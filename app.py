"""
ReconAgent Dashboard — Streamlit frontend

Run:
    streamlit run app.py

Reads the JSON/CSV artifacts already produced by:
    python recon_agent_generator.py
    python run_recon.py (or recon_engine.py directly)
    python accuracy_check.py
"""

import io
import json
import os
from datetime import datetime
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import recon_engine
import agent_actions
import agent_controller

try:
    from smoke_test_agent import DEFAULT_DEMO_PAYMENT_ID, build_demo_exceptions
except Exception:  # pragma: no cover - optional demo helper
    DEFAULT_DEMO_PAYMENT_ID = "pay_TWL0UQLntTiGhC"
    def build_demo_exceptions(payment_id: str):
        return [
            {
                "order_id": "ORD-DEMO-101",
                "payment_ref": payment_id,
                "exception_type": "TIMING_GAP",
                "match_status": "TIMING_GAP",
                "workflow_status": "Escalated to finance ops",
                "owner": "Treasury",
                "sla_hours": 72,
                "priority": "LOW",
                "ledger_amount": 6060.79,
                "bank_amount": 5892.01,
                "amount_variance": 0.00,
                "justification": "The payment exists and is successful, but bank credit is delayed beyond the standard settlement window.",
                "notes": ["The bank credit arrived after the expected timing window."]
            }
        ]

st.set_page_config(
    page_title="ReconAgent — AI Finance Controller",
    page_icon="🧾",
    layout="wide",
)

DATA_DIR = "data"
ACTION_LOG_PATH = Path(DATA_DIR) / "agent_action_log.json"
WORKFLOW_LOG_PATH = Path(DATA_DIR) / "workflow_actions.json"
UPLOADED_DATA_DIR = Path(DATA_DIR) / "uploads"

COLUMN_ALIASES = {
    "ledger": {
        "order_id": {"order_id"},
        "payment_ref": {"payment_ref", "payment_id"},
        "amount": {"amount_inr", "amount_base_ccy", "amount"},
    },
    "settlement": {
        "payment_id": {"payment_id", "payment_ref"},
        "settlement_id": {"settlement_id"},
        "net_amount": {"net_amount", "net_amount_inr"},
    },
    "bank": {
        "bank_ref_no": {"bank_ref_no", "utr_number", "utr"},
        "credit": {"credited_amount", "credit", "amount", "credited"},
    },
}

STATUS_COLORS = {
    "MATCHED": "#1B7A43",
    "DUPLICATE": "#C77800",
    "UNMATCHED": "#B3261E",
    "VARIANCE": "#B3261E",
    "TIMING_GAP": "#C77800",
    "SETTLEMENT_ONLY": "#8A6D00",
}


@st.cache_data
def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


@st.cache_data
def load_csv(path):
    if not os.path.exists(path):
        return None
    return pd.read_csv(path)


def status_badge(status):
    color = STATUS_COLORS.get(status, "#666")
    return f'<span style="background:{color}20; color:{color}; padding:2px 10px; border-radius:12px; font-size:0.82rem; font-weight:600;">{status}</span>'


def load_action_log():
    if not os.path.exists(ACTION_LOG_PATH):
        return []
    with open(ACTION_LOG_PATH, encoding="utf-8") as file:
        value = json.load(file)
    return value if isinstance(value, list) else []


def load_workflow_log():
    if not os.path.exists(WORKFLOW_LOG_PATH):
        return []
    with open(WORKFLOW_LOG_PATH, encoding="utf-8") as file:
        value = json.load(file)
    return value if isinstance(value, list) else []


def submit_agent_action(row, action, note=None):
    exception = row.to_dict()
    exception["notes"] = exception.get("notes") or []

    if action == "escalate_exception":
        agent = agent_controller.FinanceOpsAgent(
            action_log_path=ACTION_LOG_PATH,
            workflow_log_path=WORKFLOW_LOG_PATH,
        )
        result = agent._tool_escalate(exception, note=note)
        st.cache_data.clear()
        return {"workflow": result, "execution": result}

    workflow_entry = agent_actions.append_workflow_action(
        exception,
        action=action,
        note=note,
        log_path=WORKFLOW_LOG_PATH,
    )
    plan = agent_actions.create_action_plan(
        exception,
        ACTION_LOG_PATH,
        requested_action=action,
        note=note,
    )
    executed = agent_actions.execute_action(
        plan,
        ACTION_LOG_PATH,
        approved=action in {"mark_exception_resolved", "approve_action"},
    )
    st.cache_data.clear()
    return {"workflow": workflow_entry, "execution": executed}


def validate_uploaded_csv(uploaded_file, required_aliases, label):
    if uploaded_file is None:
        raise ValueError(f"{label} is required.")

    try:
        buffer = io.BytesIO(uploaded_file.getvalue())
        df = pd.read_csv(buffer)
    except Exception as exc:  # pragma: no cover - surfaced to UI
        raise ValueError(f"{label} could not be parsed as CSV: {exc}") from exc

    missing = []
    for field_name, accepted_names in required_aliases.items():
        if not any(name in df.columns for name in accepted_names):
            missing.append(field_name)

    if missing:
        expected = ", ".join(sorted({name for aliases in required_aliases.values() for name in aliases}))
        raise ValueError(
            f"{label} is missing required columns for {', '.join(missing)}. "
            f"Expected one of: {expected}."
        )

    return df


def save_uploaded_csv(uploaded_file, folder_path, name):
    if uploaded_file is None:
        return None
    folder_path.mkdir(parents=True, exist_ok=True)
    target_path = folder_path / f"{name}.csv"
    target_path.write_bytes(uploaded_file.getvalue())
    return target_path


def run_uploaded_reconciliation(ledger_file, settlement_file, bank_file):
    if ledger_file is None or settlement_file is None or bank_file is None:
        return None

    output = recon_engine.reconcile(str(ledger_file), str(settlement_file), str(bank_file))
    recon_path = Path(DATA_DIR) / "recon_results.json"
    with recon_path.open("w", encoding="utf-8") as file:
        json.dump(output, file, indent=2)
    load_json.clear()
    return output


def add_demo_razorpay_exceptions(results_df):
    """Inject a few realistic Razorpay-based exception cards for live demos."""
    if results_df is None or results_df.empty:
        return pd.DataFrame([])

    module_demo_rows = build_demo_exceptions(DEFAULT_DEMO_PAYMENT_ID)
    demo_rows = []
    for row in module_demo_rows:
        demo_rows.append({
            "order_id": row["order_id"],
            "payment_ref": row["payment_ref"],
            "ledger_amount": row.get("ledger_amount", 0.0),
            "bank_amount": row.get("bank_amount", 0.0),
            "amount_variance": row.get("amount_variance", 0.0),
            "match_status": row.get("match_status", "TIMING_GAP"),
            "workflow_status": row.get("workflow_status", "Escalated to finance ops"),
            "exception_type": row.get("exception_type", "TIMING_GAP"),
            "match_method": "LIVE_DEMO",
            "confidence_score": 0.93,
            "recommended_action": "Escalate to treasury and monitor next settlement cycle",
            "owner": row.get("owner", "Treasury"),
            "sla_hours": row.get("sla_hours", 72),
            "priority": row.get("priority", "LOW"),
            "justification": row.get("justification", "Live Razorpay verification suggests a settlement timing issue."),
            "notes": row.get("notes", []),
        })

    demo_df = pd.DataFrame(demo_rows)
    appended = pd.concat([results_df, demo_df], ignore_index=True)
    return appended


# ---------- Upload controls ----------
st.subheader("Upload reconciliation files")
with st.form("upload_form"):
    ledger_upload = st.file_uploader("Ledger CSV", type=["csv"], key="ledger_upload")
    settlement_upload = st.file_uploader("Settlement CSV", type=["csv"], key="settlement_upload")
    bank_upload = st.file_uploader("Bank statement CSV", type=["csv"], key="bank_upload")
    submitted = st.form_submit_button("Start reconciliation")

recon = st.session_state.get("uploaded_reconciliation")
if submitted:
    if not (ledger_upload and settlement_upload and bank_upload):
        st.error("Please upload all three CSV files before starting reconciliation.")
    else:
        try:
            validate_uploaded_csv(ledger_upload, COLUMN_ALIASES["ledger"], "Ledger CSV")
            validate_uploaded_csv(settlement_upload, COLUMN_ALIASES["settlement"], "Settlement CSV")
            validate_uploaded_csv(bank_upload, COLUMN_ALIASES["bank"], "Bank statement CSV")

            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            upload_folder = UPLOADED_DATA_DIR / timestamp
            ledger_path = save_uploaded_csv(ledger_upload, upload_folder, "ledger")
            settlement_path = save_uploaded_csv(settlement_upload, upload_folder, "settlement")
            bank_path = save_uploaded_csv(bank_upload, upload_folder, "bank")

            with st.spinner("Running reconciliation on uploaded files..."):
                recon = run_uploaded_reconciliation(ledger_path, settlement_path, bank_path)

            if recon is not None:
                st.session_state["uploaded_reconciliation"] = recon
                st.success(f"Reconciliation complete using uploaded files from {upload_folder}.")
        except ValueError as exc:
            st.error(f"CSV validation failed: {exc}")
        except Exception as exc:  # pragma: no cover - surfaced to UI
            st.error(f"Reconciliation failed: {exc}")

if recon is None:
    recon = load_json(f"{DATA_DIR}/recon_results.json")

accuracy = load_json(f"{DATA_DIR}/accuracy_report.json")
exceptions_df = load_csv(f"{DATA_DIR}/exception_classifications.csv")

if recon is None:
    st.error(
        "No reconciliation results found. Run the pipeline first:\n\n"
        "```\npython recon_agent_generator.py\npython run_recon.py\npython accuracy_check.py\n```"
    )
    st.stop()

results = recon["results"]
summary = recon["summary"]
results_df = pd.DataFrame(results)
results_df = add_demo_razorpay_exceptions(results_df)

if "workflow_status" not in results_df.columns:
    workflow_map = {
        "MATCHED": "Resolved",
        "DUPLICATE": "Needs manual review",
        "VARIANCE": "Escalated to finance ops",
        "TIMING_GAP": "Escalated to finance ops",
        "SETTLEMENT_ONLY": "Escalated to finance ops",
        "UNMATCHED": "Auto-flagged",
    }
    results_df["workflow_status"] = results_df["match_status"].map(workflow_map).fillna("Auto-flagged")

if "workflow_counts" not in summary:
    workflow_order = [
        "Closed with explanation",
        "Resolved",
        "Auto-flagged",
        "Escalated to finance ops",
        "Needs manual review",
    ]
    workflow_counts = {state: 0 for state in workflow_order}
    for value in results_df["workflow_status"]:
        if value in workflow_counts:
            workflow_counts[value] += 1
    summary["workflow_counts"] = workflow_counts

# ---------- Header ----------
st.title("🧾 ReconAgent")
st.caption("Multi-source payment reconciliation — Ledger × Razorpay Settlements × Bank Statement")

# ---------- Top metrics ----------
col1, col2, col3, col4, col5 = st.columns(5)
total = summary["total_ledger"]
match_rate = summary["matched"] / total * 100 if total else 0
workflow_log = load_workflow_log()
workflow_status_by_order = {}
for entry in workflow_log:
    workflow_status_by_order[entry.get("order_id")] = entry.get("workflow_status")

workflow_counts = agent_actions.summarize_workflow_counts(results_df.to_dict("records"), workflow_log_path=WORKFLOW_LOG_PATH)
summary["workflow_counts"] = workflow_counts

col1.metric("Ledger records", total)
col2.metric("Match rate", f"{match_rate:.1f}%", f"{summary['matched']}/{total}")
col3.metric("Exceptions", total - summary["matched"])
if accuracy:
    col4.metric("Engine accuracy", f"{accuracy['overall_accuracy']*100:.1f}%", help="Validated against known ground truth")
else:
    col4.metric("Engine accuracy", "—", help="Run accuracy_check.py to populate")
col5.metric("Avg. confidence", f"{results_df['confidence_score'].mean():.2f}")

st.subheader("Cash Position View")
if "cash_position" in summary:
    cash_position = summary["cash_position"]
else:
    cash_position = {
        "cash_matched": float(results_df.loc[results_df["match_status"] == "MATCHED", "bank_amount"].fillna(0).sum()) if "bank_amount" in results_df.columns else 0.0,
        "cash_unresolved": float(results_df.loc[results_df["match_status"] != "MATCHED", "ledger_amount"].fillna(0).sum()) if "ledger_amount" in results_df.columns else 0.0,
        "potential_cash_exposure": float(results_df.loc[results_df["match_status"].isin(["UNMATCHED", "VARIANCE", "TIMING_GAP", "SETTLEMENT_ONLY"]) | results_df["exception_type"].isin(["MISSING_SETTLEMENT", "MISSING_BANK"]) | (results_df["amount_variance"].fillna(0) < 0), "ledger_amount"].fillna(0).sum()) if {"ledger_amount", "exception_type", "amount_variance"}.issubset(results_df.columns) else 0.0,
        "open_exceptions": int((results_df["match_status"] != "MATCHED").sum()),
        "settlement_pending": float(results_df.loc[~results_df.get("settlement_found", pd.Series(True, index=results_df.index)).fillna(True).astype(bool), "ledger_amount"].fillna(0).sum()) if "ledger_amount" in results_df.columns else 0.0,
        "risk_exposure": "Medium",
    }

cash_cols = st.columns(6)
with cash_cols[0]:
    st.metric("Cash matched", f"₹{cash_position.get('cash_matched', 0):,.2f}")
with cash_cols[1]:
    st.metric("Cash unresolved", f"₹{cash_position.get('cash_unresolved', 0):,.2f}")
with cash_cols[2]:
    st.metric("Potential cash exposure", f"₹{cash_position.get('potential_cash_exposure', 0):,.2f}")
with cash_cols[3]:
    st.metric("Open exceptions", cash_position.get('open_exceptions', 0))
with cash_cols[4]:
    st.metric("Settlement pending", f"₹{cash_position.get('settlement_pending', 0):,.2f}")
with cash_cols[5]:
    st.metric("Risk exposure", cash_position.get('risk_exposure', 'Low'))

st.subheader("Workflow status")
workflow_cols = st.columns(5)
workflow_order = [
    "Closed with explanation",
    "Resolved",
    "Auto-flagged",
    "Escalated to finance ops",
    "Needs manual review",
]
for idx, state in enumerate(workflow_order):
    with workflow_cols[idx]:
        st.metric(state, workflow_counts.get(state, 0))

st.divider()

# ---------- Tabs ----------
tab_overview, tab_exceptions, tab_audit, tab_accuracy = st.tabs(
    ["📊 Overview", "⚠️ Exceptions", "🔍 Audit Trail", "🎯 Accuracy Validation"]
)

# ===== TAB 1: Overview =====
with tab_overview:
    left, right = st.columns([1, 1])

    with left:
        st.subheader("Match status breakdown")
        status_counts = results_df["match_status"].value_counts().reset_index()
        status_counts.columns = ["status", "count"]
        fig = px.pie(
            status_counts, names="status", values="count", hole=0.55,
            color="status", color_discrete_map=STATUS_COLORS,
        )
        fig.update_traces(textinfo="value+percent")
        fig.update_layout(showlegend=True, margin=dict(t=10, b=10, l=10, r=10), height=340)
        st.plotly_chart(fig, width="stretch")

    with right:
        st.subheader("Exception types")
        exc_types = results_df["exception_type"].dropna().value_counts().reset_index()
        exc_types.columns = ["exception_type", "count"]
        if not exc_types.empty:
            fig2 = px.bar(exc_types, x="count", y="exception_type", orientation="h", color="count",
                           color_continuous_scale="Reds")
            fig2.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=340, showlegend=False,
                                coloraxis_showscale=False, yaxis_title="", xaxis_title="Count")
            st.plotly_chart(fig2, width="stretch")
        else:
            st.info("No exceptions found.")

    st.subheader("All transactions")
    display_cols = ["order_id", "payment_ref", "ledger_amount", "match_status", "workflow_status",
                     "exception_type", "match_method", "confidence_score"]
    view_df = results_df[display_cols].copy()
    view_df["match_status"] = view_df["match_status"].apply(lambda s: status_badge(s))
    st.write(view_df.to_html(escape=False, index=False), unsafe_allow_html=True)

# ===== TAB 2: Exceptions =====
with tab_exceptions:
    st.subheader("Exception report — with plain-English explanations")
    non_matched = results_df[results_df["match_status"] != "MATCHED"].copy()

    if non_matched.empty:
        st.success("No exceptions in this batch.")
    else:
        status_filter = st.multiselect(
            "Filter by status", options=sorted(non_matched["match_status"].unique()),
            default=sorted(non_matched["match_status"].unique())
        )
        filtered = non_matched[non_matched["match_status"].isin(status_filter)]

        if "ORD-DEMO-101" in filtered["order_id"].astype(str).values:
            st.info("Live Razorpay demo exception is included below to showcase the escalation flow using payment ID pay_TWL0UQLntTiGhC.")

        explanations_by_order = {}
        if exceptions_df is not None and "llm_explanation" in exceptions_df.columns:
            explanations_by_order = dict(zip(exceptions_df["order_id"], exceptions_df["llm_explanation"]))

        action_log = load_action_log()
        action_by_order = {}
        for entry in action_log:
            action_by_order.setdefault(entry.get("order_id"), []).append(entry)

        workflow_log = load_workflow_log()
        workflow_by_order = {}
        for entry in workflow_log:
            workflow_by_order.setdefault(entry.get("order_id"), []).append(entry)

        for _, row in filtered.iterrows():
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.markdown(f"**{row['order_id']}** &nbsp; `{row['payment_ref']}`", unsafe_allow_html=True)
                with c2:
                    st.markdown(status_badge(row["match_status"]), unsafe_allow_html=True)

                m1, m2, m3 = st.columns(3)
                m1.caption(f"Ledger amount: ₹{row['ledger_amount']:,.2f}")
                m2.caption(f"Bank amount: ₹{row['bank_amount']:,.2f}" if pd.notna(row.get("bank_amount")) else "Bank amount: —")
                m3.caption(f"Variance: ₹{row['amount_variance']:,.2f}" if pd.notna(row.get("amount_variance")) else "Variance: —")

                latest_workflow = workflow_by_order.get(row["order_id"], [])
                current_workflow = (latest_workflow[-1].get("workflow_status") if latest_workflow else row.get("workflow_status") or "Auto-flagged")
                if row.get("recommended_action"):
                    st.markdown(
                        f"**Recommended action:** {row['recommended_action']}<br>"
                        f"**Owner:** {row['owner']}<br>"
                        f"**SLA:** {row['sla_hours']} hours<br>"
                        f"**Priority:** {row.get('priority', 'LOW')}<br>"
                        f"**Current workflow status:** {current_workflow}<br>"
                        f"**Why:** {row['justification']}",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"**Recommended action:** {row.get('exception_type', 'Review needed')}<br>"
                        f"**Owner:** {row.get('owner', 'Finance Ops')}<br>"
                        f"**SLA:** {row.get('sla_hours', 24)} hours<br>"
                        f"**Priority:** {row.get('priority', 'LOW')}<br>"
                        f"**Current workflow status:** {current_workflow}",
                        unsafe_allow_html=True,
                    )

                latest_action = action_by_order.get(row["order_id"], [])
                if latest_action:
                    last_entry = latest_action[-1]
                    st.caption(
                        f"Agent action: {last_entry.get('decision', '—')} | "
                        f"{last_entry.get('status', '—')} | "
                        f"Priority: {last_entry.get('priority', '—')}"
                    )

                note = st.text_input(
                    "Operator note",
                    key=f"note_{row['order_id']}",
                    placeholder="Add context for the finance team",
                )
                action_cols = st.columns(6)
                if st.button("Run AI agent", key=f"agent_{row['order_id']}"):
                    run = agent_controller.run_agent(
                        row.to_dict(),
                        action_log_path=ACTION_LOG_PATH,
                    )
                    st.session_state[f"agent_run_{row['order_id']}"] = run
                    st.rerun()
                if action_cols[0].button("Investigate", key=f"investigate_{row['order_id']}"):
                    submit_agent_action(row, "investigate_exception", note or None)
                    st.success("Investigation recorded and logged.")
                    st.rerun()
                if action_cols[1].button("Approve action", key=f"approve_{row['order_id']}"):
                    submit_agent_action(row, "approve_action", note or None)
                    st.success("Action approved and logged.")
                    st.rerun()
                if action_cols[2].button("Escalate", key=f"escalate_{row['order_id']}"):
                    submit_agent_action(row, "escalate_exception", note or None)
                    st.success("Exception escalated and logged.")
                    st.rerun()
                if action_cols[3].button("Add note", key=f"add_note_{row['order_id']}"):
                    submit_agent_action(row, "add_exception_note", note or None)
                    st.success("Operator note saved to the workflow log.")
                    st.rerun()
                if action_cols[4].button("Mark reviewed", key=f"review_{row['order_id']}"):
                    submit_agent_action(row, "mark_exception_reviewed", note or None)
                    st.success("Exception marked reviewed and logged.")
                    st.rerun()
                if action_cols[5].button("Resolve", key=f"resolve_{row['order_id']}"):
                    submit_agent_action(row, "mark_exception_resolved", note or None)
                    st.success("Resolution approved and logged.")
                    st.rerun()

                latest_run = st.session_state.get(f"agent_run_{row['order_id']}")
                if latest_run:
                    investigation = latest_run["investigation"]
                    st.info(
                        f"Agent investigation ({investigation['source']}): "
                        f"{investigation['investigation']}\n\n"
                        f"Decision: {investigation['action']} | "
                        f"Confidence: {investigation['confidence']:.0%}\n\n"
                        f"Verification: {latest_run['verification']['verification']}"
                    )

                explanation = explanations_by_order.get(row["order_id"])
                if explanation and isinstance(explanation, str) and not explanation.startswith("[LLM"):
                    st.info(f"🤖 {explanation}")
                elif row.get("notes"):
                    notes = row["notes"] if isinstance(row["notes"], list) else [row["notes"]]
                    st.caption("Rule-based note: " + "; ".join(notes))
                else:
                    st.caption("No explanation available yet — run llm_explainer.py to generate one.")

# ===== TAB 3: Audit Trail =====
with tab_audit:
    st.subheader("Full decision log")
    st.caption("Every transaction's match decision, method, and confidence — matched or not.")

    audit_cols = ["order_id", "payment_ref", "match_status", "match_method",
                  "confidence_score", "settlement_found", "bank_found",
                  "days_to_settlement", "days_to_bank"]
    audit_df = results_df[audit_cols].sort_values("confidence_score", ascending=True)
    st.dataframe(audit_df, width="stretch", height=500)

    st.subheader("Fuzzy-match false-positive guard")
    st.caption("Cases where a fuzzy match was *considered* but rejected as ambiguous, instead of guessed at.")
    fuzzy_log = recon.get("fuzzy_match_log", [])
    if fuzzy_log:
        for entry in fuzzy_log:
            with st.container(border=True):
                st.markdown(f"**{entry['order_id']}** — `{entry['payment_ref']}`")
                st.caption(f"Reason: {entry['reason']}")
                cand_df = pd.DataFrame(entry["candidates"])
                st.dataframe(cand_df, width="stretch", hide_index=True)
    else:
        st.info("No ambiguous fuzzy matches were rejected in this batch — every match was either exact or uniquely confident.")

    st.subheader("Agent action history")
    st.caption("Workflow actions performed from the Exceptions tab, including operator approvals.")
    action_log = load_action_log()
    if action_log:
        st.dataframe(pd.DataFrame(action_log), width="stretch", hide_index=True)
    else:
        st.info("No agent actions have been recorded yet.")

# ===== TAB 4: Accuracy Validation =====
with tab_accuracy:
    st.subheader("Engine accuracy vs. known ground truth")
    st.caption(
        "The dataset generator plants known categories (clean, refund, duplicate, timing gap, "
        "fee drift, missing settlement). This checks whether the engine correctly identified each one — "
        "not just whether the match rate looks good."
    )

    if accuracy is None:
        st.warning("Run `python accuracy_check.py` to generate this validation.")
    else:
        c1, c2 = st.columns([1, 2])
        with c1:
            st.metric("Overall accuracy", f"{accuracy['overall_accuracy']*100:.1f}%",
                       f"{accuracy['correct']}/{accuracy['total']} correct")

        with c2:
            cat_data = []
            for cat, stats in accuracy["by_category"].items():
                cat_data.append({"category": cat, "accuracy": stats["accuracy"] * 100,
                                  "correct": stats["correct"], "total": stats["total"]})
            cat_df = pd.DataFrame(cat_data)
            fig3 = px.bar(cat_df, x="category", y="accuracy", color="accuracy",
                           color_continuous_scale="RdYlGn", range_color=[0, 100],
                           text=cat_df.apply(lambda r: f"{r['correct']}/{r['total']}", axis=1))
            fig3.update_layout(height=300, margin=dict(t=10, b=10, l=10, r=10),
                                coloraxis_showscale=False, yaxis_title="Accuracy %", xaxis_title="")
            st.plotly_chart(fig3, width="stretch")

        if accuracy["misclassified"]:
            st.subheader(f"⚠️ {len(accuracy['misclassified'])} misclassified transaction(s)")
            st.dataframe(pd.DataFrame(accuracy["misclassified"]), width="stretch", hide_index=True)
        else:
            st.success("No misclassifications — every transaction resolved exactly as the generator intended.")

st.divider()
st.caption("ReconAgent — built for the Razorpay AI Buildathon, AI Finance Controller track.")
