"""
ReconAgent — Core Deterministic Reconciliation Engine (v2, realistic schema)

Matches across three realistically-shaped data sources:
1. Internal ledger      — payment_ref (== settlement's payment_id) is known upfront,
                           since the merchant captures it via webhook at checkout.
                           statement_no/bank_ref_no are blank until reconciled.
2. Razorpay settlement   — real Razorpay field names (payment_id, settlement_id,
                           settlement_utr, fee, tax, net_amount, settled).
3. Bank statement        — the bank does NOT know Razorpay's payment_id or
                           settlement_id at all. The only real link is
                           settlement_utr == bank's own bank_ref_no column.

Matching strategy:
  ledger.payment_ref      -> settlement.payment_id   (exact, or fuzzy fallback)
  settlement.settlement_utr -> bank.bank_ref_no        (exact — this is the real key)

Output contract (result dict per ledger row) is UNCHANGED from v1, so
run_recon.py, llm_explainer.py, and accuracy_check.py all work without
modification.
"""

import csv
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Optional
import json
from difflib import SequenceMatcher


def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path) as f:
        return list(csv.DictReader(f))


def parse_date(date_str: str) -> "datetime.date":
    # Handles both "YYYY-MM-DD" and "YYYY-MM-DD HH:MM:SS"-ish values from date()/timedelta() str()
    return datetime.strptime(date_str[:10], "%Y-%m-%d").date()


def parse_amount(amount_str: str) -> float:
    return float(amount_str)


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        return value
    return None


def get_exception_action(exception_type: Optional[str]) -> Dict[str, Any]:
    action_map = {
        "DUPLICATE_IN_LEDGER": {
            "recommended_action": "Review duplicate ledger entries and remove one",
            "owner": "Finance Ops",
            "sla_hours": 24,
            "justification": "Duplicate ledger records can artificially inflate cash position and create double-counting risk.",
        },
        "MISSING_SETTLEMENT": {
            "recommended_action": "Check Razorpay settlement status and retry reconciliation",
            "owner": "Finance Ops",
            "sla_hours": 24,
            "justification": "No settlement status record was found for the payment, which usually means the payout is pending, missing, or not yet reconciled.",
        },
        "FEE_DRIFT": {
            "recommended_action": "Validate payment gateway fee calculation / review contract changes",
            "owner": "Finance Ops",
            "sla_hours": 48,
            "justification": "The bank credit differs from the expected settlement net amount by more than the variance threshold, indicating possible fee or charge drift.",
        },
        "TIMING_GAP": {
            "recommended_action": "Escalate to treasury and monitor next settlement cycle",
            "owner": "Treasury",
            "sla_hours": 72,
            "justification": "The bank credit arrived beyond the expected timing window, indicating a likely payout delay or cash timing issue.",
        },
        "MISSING_BANK": {
            "recommended_action": "Check if bank statement is delayed or settlement is uncredited",
            "owner": "Treasury",
            "sla_hours": 24,
            "justification": "A valid settlement exists, but the bank credit was not found in the statement, suggesting a delayed or missing bank posting.",
        },
    }

    if exception_type in action_map:
        return action_map[exception_type]

    return {
        "recommended_action": "Review exception and assign manual follow-up",
        "owner": "Finance Ops",
        "sla_hours": 24,
        "justification": "The system could not confidently classify the issue and requires manual review.",
    }


def _find_fuzzy_settlement(
    ledger_row: Dict[str, str],
    settlements: List[Dict[str, str]],
    used_settlement_ids: set,
    amount_tolerance: float,
    date_window: int,
) -> tuple[Optional[Dict[str, str]], float, List[Dict[str, Any]]]:
    """Find a uniquely supported settlement when payment_ref differs from payment_id."""
    ledger_amount = parse_amount(ledger_row["amount_base_ccy"])
    ledger_date = parse_date(ledger_row["book_date"])
    ledger_ref = ledger_row["payment_ref"]
    candidates = []

    for settlement in settlements:
        if settlement["settlement_id"] in used_settlement_ids:
            continue

        amount_delta = abs(ledger_amount - parse_amount(settlement["gross_amount"]))
        settlement_date = parse_date(settlement["settled_at"])
        date_delta = abs((settlement_date - ledger_date).days)
        if amount_delta > amount_tolerance or date_delta > date_window:
            continue

        reference_similarity = SequenceMatcher(
            None, ledger_ref.lower(), settlement["payment_id"].lower()
        ).ratio()
        amount_score = max(0.0, 1.0 - amount_delta / amount_tolerance)
        date_score = max(0.0, 1.0 - date_delta / (date_window + 1))
        score = reference_similarity * 0.55 + amount_score * 0.30 + date_score * 0.15
        candidates.append({
            "settlement_id": settlement["settlement_id"],
            "payment_id": settlement["payment_id"],
            "reference_similarity": round(reference_similarity, 4),
            "amount_delta": round(amount_delta, 2),
            "date_delta": date_delta,
            "score": round(score, 4),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    if not candidates:
        return None, 0.0, []

    best = candidates[0]
    second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
    is_unique = best["score"] - second_score >= 0.12
    has_reference_evidence = best["reference_similarity"] >= 0.75
    if best["score"] < 0.78 or not is_unique or not has_reference_evidence:
        return None, best["score"], candidates[:3]

    settlement = next(s for s in settlements if s["settlement_id"] == best["settlement_id"])
    return settlement, best["score"], candidates[:3]


def reconcile(
    ledger_path: str,
    settlement_path: str,
    bank_path: str,
    variance_threshold: float = 2.0,
    timing_gap_threshold: int = 5,
    fuzzy_amount_tolerance: float = 2.0,
    fuzzy_date_window: int = 7,
) -> Dict[str, Any]:
    ledger = load_csv(ledger_path)
    settlements = load_csv(settlement_path)
    bank = load_csv(bank_path)

    def normalize_settlement_row(row: Dict[str, str]) -> Dict[str, Any]:
        normalized = dict(row)
        normalized["payment_id"] = _first_value(row.get("payment_id"), row.get("payment_ref"), "")
        normalized["settlement_id"] = _first_value(row.get("settlement_id"), "")
        normalized["gross_amount"] = _first_value(row.get("gross_amount"), row.get("amount_inr"), row.get("amount"), "0")
        normalized["net_amount"] = _first_value(row.get("net_amount"), row.get("net_amount_inr"), row.get("net"), "0")
        normalized["settlement_utr"] = _first_value(row.get("settlement_utr"), row.get("utr_number"), row.get("bank_ref_no"), "")
        normalized["settled_at"] = _first_value(row.get("settled_at"), row.get("settlement_date"), row.get("value_date"), "2026-01-01")
        return normalized

    def normalize_bank_row(row: Dict[str, str]) -> Dict[str, Any]:
        normalized = dict(row)
        normalized["bank_ref_no"] = _first_value(row.get("bank_ref_no"), row.get("utr_number"), row.get("utr"), "")
        normalized["credit"] = _first_value(row.get("credit"), row.get("credited_amount"), row.get("amount"), row.get("credited"), "0")
        normalized["value_date"] = _first_value(row.get("value_date"), row.get("txn_date"), row.get("book_date"), "2026-01-01")
        return normalized

    normalized_settlements = [normalize_settlement_row(s) for s in settlements]
    normalized_bank = [normalize_bank_row(b) for b in bank]

    settlement_by_payment_id = {}
    for s in normalized_settlements:
        pid = s["payment_id"]
        if pid and pid not in settlement_by_payment_id:
            settlement_by_payment_id[pid] = s

    bank_by_ref_no = {b["bank_ref_no"]: b for b in normalized_bank if b["bank_ref_no"]}

    ledger_count_by_payment_ref = defaultdict(list)
    for l in ledger:
        payment_ref = _first_value(l.get("payment_ref"), l.get("payment_id"), "")
        ledger_count_by_payment_ref[payment_ref].append(l)

    results = []
    matched_settlement_ids = set()
    matched_payment_ids = set()
    matched_bank_ref_nos = set()
    fuzzy_match_log = []

    def determine_workflow_status(match_status: str, exception_type: Optional[str], notes: list) -> str:
        if match_status == "MATCHED":
            if notes and any(
                "reconciled" in note.lower() or "exact reference" in note.lower() or "utr" in note.lower()
                for note in notes
            ):
                return "Closed with explanation"
            return "Resolved"
        if match_status == "DUPLICATE":
            return "Needs manual review"
        if match_status in {"VARIANCE", "TIMING_GAP", "SETTLEMENT_ONLY"}:
            return "Escalated to finance ops"
        return "Auto-flagged"

    for l in ledger:
        payment_ref = _first_value(l.get("payment_ref"), l.get("payment_id"), "")
        ledger_amount = parse_amount(_first_value(l.get("amount_base_ccy"), l.get("amount_inr"), l.get("amount"), "0"))
        ledger_date = parse_date(_first_value(l.get("book_date"), l.get("order_date"), l.get("value_date"), "2026-01-01"))
        status = _first_value(l.get("status"), "completed")

        result = {
            "order_id": l["order_id"],
            "payment_ref": payment_ref,
            "ledger_amount": ledger_amount,
            "ledger_date": str(ledger_date),
            "ledger_status": status,
            "match_status": None,
            "workflow_status": None,
            "settlement_found": False,
            "bank_found": False,
            "settlement_id": None,
            "settlement_net": None,
            "bank_amount": None,
            "bank_date": None,
            "amount_variance": None,
            "days_to_settlement": None,
            "days_to_bank": None,
            "exception_type": None,
            "recommended_action": None,
            "owner": None,
            "sla_hours": None,
            "justification": None,
            "notes": [],
            "match_method": None,
            "confidence_score": 0.0,
            "fuzzy_match_candidates": [],
        }

        if len(ledger_count_by_payment_ref[payment_ref]) > 1:
            result["exception_type"] = "DUPLICATE_IN_LEDGER"
            result["notes"].append(
                f"Payment ref {payment_ref} appears {len(ledger_count_by_payment_ref[payment_ref])} times in ledger"
            )

        if payment_ref in settlement_by_payment_id:
            result["settlement_found"] = True
            s = settlement_by_payment_id[payment_ref]
            matched_payment_ids.add(payment_ref)
            matched_settlement_ids.add(s["settlement_id"])
            result["match_method"] = "EXACT_REFERENCE"
            result["confidence_score"] = 1.0
        else:
            s, fuzzy_score, candidates = _find_fuzzy_settlement(
                {
                    "amount_base_ccy": str(ledger_amount),
                    "book_date": str(ledger_date),
                    "payment_ref": payment_ref,
                },
                normalized_settlements,
                matched_settlement_ids,
                fuzzy_amount_tolerance,
                fuzzy_date_window,
            )
            result["fuzzy_match_candidates"] = candidates
            if s is not None:
                result["settlement_found"] = True
                matched_settlement_ids.add(s["settlement_id"])
                matched_payment_ids.add(s["payment_id"])
                result["match_method"] = "FUZZY_REFERENCE"
                result["confidence_score"] = fuzzy_score
                result["notes"].append(f"Fuzzy settlement match: {payment_ref} -> {s['payment_id']}")
            elif candidates:
                fuzzy_match_log.append({
                    "order_id": l["order_id"],
                    "payment_ref": payment_ref,
                    "reason": "REJECTED_POSSIBLE_FALSE_POSITIVE",
                    "candidates": candidates,
                })
                result["notes"].append("Possible fuzzy match rejected: candidate was ambiguous or low confidence")

        if result["settlement_found"]:
            result["settlement_id"] = s["settlement_id"]
            result["settlement_net"] = parse_amount(s["net_amount"])
            settlement_date = parse_date(s["settled_at"])
            result["days_to_settlement"] = (settlement_date - ledger_date).days

            if status == "partially_refunded":
                settlement_gross = parse_amount(s["gross_amount"])
                if settlement_gross < ledger_amount:
                    result["notes"].append(
                        f"Partial refund: ledger ₹{ledger_amount}, settlement gross ₹{settlement_gross}"
                    )

            settlement_utr = s["settlement_utr"]
            if settlement_utr in bank_by_ref_no:
                result["bank_found"] = True
                b = bank_by_ref_no[settlement_utr]
                matched_bank_ref_nos.add(settlement_utr)

                result["bank_amount"] = parse_amount(b["credit"])
                result["bank_date"] = b["value_date"]
                bank_date = parse_date(b["value_date"])
                result["days_to_bank"] = (bank_date - ledger_date).days

                if result["settlement_net"] is not None and result["bank_amount"] is not None:
                    variance = result["bank_amount"] - result["settlement_net"]
                    result["amount_variance"] = round(variance, 2)
                    if abs(variance) > variance_threshold:
                        result["notes"].append(
                            f"Amount variance: bank ₹{result['bank_amount']} vs settlement ₹{result['settlement_net']} (₹{variance:.2f})"
                        )

                if result["days_to_bank"] and result["days_to_bank"] > timing_gap_threshold:
                    result["notes"].append(f"Timing gap: bank credit {result['days_to_bank']} days after order")
            else:
                result["notes"].append(f"Settlement UTR {settlement_utr} not found in bank statement")
        else:
            result["exception_type"] = "MISSING_SETTLEMENT"
            result["notes"].append(f"No settlement found for {payment_ref}")

        if result["exception_type"] == "DUPLICATE_IN_LEDGER":
            result["match_status"] = "DUPLICATE"
        elif result["exception_type"] == "MISSING_SETTLEMENT":
            result["match_status"] = "UNMATCHED"
        elif result["settlement_found"] and result["bank_found"]:
            if result["amount_variance"] is not None and abs(result["amount_variance"]) > variance_threshold:
                result["match_status"] = "VARIANCE"
                result["exception_type"] = "FEE_DRIFT"
            elif result["days_to_bank"] and result["days_to_bank"] > timing_gap_threshold:
                result["match_status"] = "TIMING_GAP"
                if result["exception_type"] is None:
                    result["exception_type"] = "TIMING_GAP"
            else:
                result["match_status"] = "MATCHED"
                result["notes"].append(
                    "Reconciled by exact reference match and bank UTR validation."
                )
        elif result["settlement_found"] and not result["bank_found"]:
            result["match_status"] = "SETTLEMENT_ONLY"
            if result["exception_type"] is None:
                result["exception_type"] = "MISSING_BANK"
        else:
            result["match_status"] = "UNMATCHED"

        result["workflow_status"] = determine_workflow_status(result["match_status"], result["exception_type"], result["notes"])

        if result["exception_type"]:
            action = get_exception_action(result["exception_type"])
            result["recommended_action"] = action["recommended_action"]
            result["owner"] = action["owner"]
            result["sla_hours"] = action["sla_hours"]
            result["justification"] = action["justification"]

        results.append(result)

    workflow_counts = {}
    for state in ["Closed with explanation", "Resolved", "Auto-flagged", "Escalated to finance ops", "Needs manual review"]:
        workflow_counts[state] = sum(1 for r in results if r["workflow_status"] == state)

    cash_matched = sum(float(r.get("bank_amount") or 0) for r in results if r.get("match_status") == "MATCHED")
    cash_unresolved = sum(float(r.get("ledger_amount") or 0) for r in results if r.get("match_status") != "MATCHED")
    open_exceptions = sum(1 for r in results if r.get("match_status") != "MATCHED")
    settlement_pending = sum(float(r.get("ledger_amount") or 0) for r in results if not r.get("settlement_found"))
    potential_cash_exposure = sum(
        float(r.get("ledger_amount") or 0)
        for r in results
        if r.get("match_status") in {"UNMATCHED", "VARIANCE", "TIMING_GAP", "SETTLEMENT_ONLY"}
        or r.get("exception_type") in {"MISSING_SETTLEMENT", "MISSING_BANK"}
        or (r.get("amount_variance") is not None and float(r.get("amount_variance") or 0) < 0)
    )

    risk_levels = []
    for r in results:
        if r.get("exception_type") in {"MISSING_SETTLEMENT"}:
            risk_levels.append("High")
        elif r.get("match_status") in {"VARIANCE", "UNMATCHED", "SETTLEMENT_ONLY"}:
            risk_levels.append("High" if (abs(float(r.get("amount_variance") or 0)) > 50 or float(r.get("ledger_amount") or 0) > 50000) else "Medium")
        elif r.get("match_status") in {"DUPLICATE", "TIMING_GAP", "MISSING_BANK"}:
            risk_levels.append("Medium")
        elif r.get("exception_type") == "FEE_DRIFT":
            risk_levels.append("Low")
        elif r.get("match_status") != "MATCHED":
            risk_levels.append("Medium")
        else:
            risk_levels.append("Low")

    if "High" in risk_levels:
        aggregated_risk = "High"
    elif "Medium" in risk_levels:
        aggregated_risk = "Medium"
    else:
        aggregated_risk = "Low"

    summary = {
        "total_ledger": len(ledger),
        "total_settlements": len(normalized_settlements),
        "total_bank": len(normalized_bank),
        "matched": sum(1 for r in results if r["match_status"] == "MATCHED"),
        "duplicates": sum(1 for r in results if r["match_status"] == "DUPLICATE"),
        "unmatched": sum(1 for r in results if r["match_status"] == "UNMATCHED"),
        "variance": sum(1 for r in results if r["match_status"] == "VARIANCE"),
        "timing_gap": sum(1 for r in results if r["match_status"] == "TIMING_GAP"),
        "settlement_only": sum(1 for r in results if r["match_status"] == "SETTLEMENT_ONLY"),
        "workflow_counts": workflow_counts,
        "cash_position": {
            "cash_matched": round(cash_matched, 2),
            "cash_unresolved": round(cash_unresolved, 2),
            "potential_cash_exposure": round(potential_cash_exposure, 2),
            "open_exceptions": open_exceptions,
            "settlement_pending": round(settlement_pending, 2),
            "risk_exposure": aggregated_risk,
        },
        "unmatched_settlements": len(settlement_by_payment_id.keys() - matched_payment_ids),
        "unmatched_bank": len(bank_by_ref_no.keys() - matched_bank_ref_nos),
    }

    return {"results": results, "summary": summary, "fuzzy_match_log": fuzzy_match_log}


def save_results(recon_output: Dict[str, Any], output_path: str):
    with open(output_path, "w") as f:
        json.dump(recon_output, f, indent=2)


def print_summary(recon_output: Dict[str, Any]):
    s = recon_output["summary"]
    print("\n=== RECONCILIATION SUMMARY ===")
    print(f"Ledger records: {s['total_ledger']}")
    print(f"Settlement records: {s['total_settlements']}")
    print(f"Bank records: {s['total_bank']}")
    print(f"\nMatched (all 3 sources): {s['matched']}")
    print(f"Duplicates: {s['duplicates']}")
    print(f"Unmatched: {s['unmatched']}")
    print(f"Amount variance: {s['variance']}")
    print(f"Timing gaps: {s['timing_gap']}")
    print(f"Settlement only: {s['settlement_only']}")
    if s["unmatched_settlements"] > 0:
        print(f"\n⚠️  {s['unmatched_settlements']} settlements not linked to ledger")
    if s["unmatched_bank"] > 0:
        print(f"⚠️  {s['unmatched_bank']} bank transactions unmatched")


if __name__ == "__main__":
    output = reconcile("data/internal_ledger.csv", "data/razorpay_settlement.csv", "data/bank_statement.csv")
    save_results(output, "data/recon_results.json")
    print_summary(output)
    print("\nResults saved to data/recon_results.json")