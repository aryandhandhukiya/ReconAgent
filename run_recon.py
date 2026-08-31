#!/usr/bin/env python3
"""
ReconAgent — Run Entire Reconciliation Pipeline

Usage:
    python run_recon.py

This will:
1. Generate synthetic data
2. Run reconciliation
3. Classify exceptions
4. Print summary
"""

import subprocess
import sys
import json
import recon_engine
import agent_actions


def run_pipeline():
    print("=" * 60)
    print("ReconAgent — Payment Reconciliation Pipeline")
    print("=" * 60)
    
    # Step 1: Generate data
    print("\n[1/3] Generating synthetic data...")
    result = subprocess.run([sys.executable, "recon_agent_generator.py"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Data generation failed:\n{result.stderr}")
        return
    print("✅ Data generated (data/*.csv)")
    
    # Step 2: Run reconciliation
    print("\n[2/3] Running reconciliation engine...")
    output = recon_engine.reconcile(
        "data/internal_ledger.csv",
        "data/razorpay_settlement.csv",
        "data/bank_statement.csv"
    )
    
    # Save results
    with open("data/recon_results.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    
    # Save summary table
    import csv
    results = output["results"]
    with open("data/recon_summary_table.csv", "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "order_id", "payment_ref", "match_status", "exception_type",
            "ledger_amount", "settlement_net", "bank_amount", "amount_variance",
            "days_to_settlement", "days_to_bank", "match_method", "confidence_score",
            "fuzzy_match_candidates", "notes"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = {k: r.get(k, "") for k in fieldnames}
            if isinstance(row["notes"], list):
                row["notes"] = "; ".join(row["notes"]) if row["notes"] else ""
            if isinstance(row["fuzzy_match_candidates"], list):
                row["fuzzy_match_candidates"] = json.dumps(row["fuzzy_match_candidates"])
            writer.writerow(row)
    
    print("✅ Reconciliation complete")
    
    # Step 3: Classify exceptions
    print("\n[3/3] Classifying exceptions...")
    
    def classify_exception(result):
        classification = {
            "order_id": result["order_id"],
            "payment_ref": result["payment_ref"],
            "exception_type": result["exception_type"],
            "workflow_status": result.get("workflow_status", "Auto-flagged"),
            "severity": None,
            "likely_cause": None,
            "suggested_action": result.get("recommended_action"),
            "owner": result.get("owner"),
            "sla_hours": result.get("sla_hours"),
            "justification": result.get("justification"),
            "notes": result["notes"]
        }
        
        if result["exception_type"] == "DUPLICATE_IN_LEDGER":
            classification["severity"] = "MEDIUM"
            classification["likely_cause"] = "Duplicate entry in ledger"
            classification["suggested_action"] = "Review and remove duplicate"
        elif result["exception_type"] == "MISSING_SETTLEMENT":
            classification["severity"] = "HIGH"
            classification["likely_cause"] = "Payment not settled by gateway"
            classification["suggested_action"] = "Check payment gateway dashboard"
        elif result["exception_type"] == "FEE_DRIFT":
            classification["severity"] = "LOW"
            classification["likely_cause"] = f"Amount variance: ₹{result['amount_variance']}"
            classification["suggested_action"] = "Review bank charges or rounding"
        elif result["exception_type"] == "TIMING_GAP":
            classification["severity"] = "MEDIUM"
            classification["likely_cause"] = f"Bank credit delayed by {result['days_to_bank']} days"
            classification["suggested_action"] = "Monitor next settlement cycle"
        else:
            classification["severity"] = "UNKNOWN"
            classification["likely_cause"] = "Unknown"
            classification["suggested_action"] = "Manual review"
        
        return classification
    
    classifications = []
    for r in results:
        if r["exception_type"]:
            classifications.append(classify_exception(r))
    
    # Save classifications
    with open("data/exception_classifications.json", "w", encoding="utf-8") as f:
        json.dump(classifications, f, indent=2)
    
    import csv
    with open("data/exception_classifications.csv", "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "order_id", "payment_ref", "exception_type", "severity", "likely_cause",
            "suggested_action", "owner", "sla_hours", "justification", "notes"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for c in classifications:
            row = {k: c.get(k, "") for k in fieldnames}
            if isinstance(row["notes"], list):
                row["notes"] = "; ".join(row["notes"]) if row["notes"] else ""
            writer.writerow(row)
    
    print("✅ Exceptions classified")

    # Step 3b: Create controlled agent action proposals
    print("\n[agent] Creating action proposals...")
    action_plans = agent_actions.create_action_plans(classifications)
    print(f"✅ {len(action_plans)} action proposal(s) saved to data/agent_action_log.json")

    # Step 4: LLM-powered plain-English explanations (Gemini)
    print("\n[4/4] Generating LLM explanations for exceptions...")
    llm_explanations_by_order_id = {}
    try:
        import llm_explainer
        llm_results = llm_explainer.explain_all_exceptions(
            results_path="data/recon_results.json",
            output_path="data/llm_exception_explanations.json",
        )
        llm_explanations_by_order_id = {
            item["order_id"]: item["llm_explanation"] for item in llm_results
        }
        print("✅ LLM explanations generated")
    except RuntimeError as e:
        print(f"⚠️  Skipping LLM step: {e}")
    except Exception as e:  # noqa: BLE001 — surface any Gemini/network error without killing the pipeline
        print(f"⚠️  LLM step failed, continuing without it: {e}")

    # Merge LLM explanations into the classification files
    if llm_explanations_by_order_id:
        for c in classifications:
            c["llm_explanation"] = llm_explanations_by_order_id.get(c["order_id"], "")

        with open("data/exception_classifications.json", "w", encoding="utf-8") as f:
            json.dump(classifications, f, indent=2)

        with open("data/exception_classifications.csv", "w", encoding="utf-8", newline="") as f:
            fieldnames = [
                "order_id", "payment_ref", "exception_type", "severity",
                "likely_cause", "suggested_action", "owner", "sla_hours",
                "justification", "llm_explanation", "notes"
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for c in classifications:
                row = {k: c.get(k, "") for k in fieldnames}
                if isinstance(row["notes"], list):
                    row["notes"] = "; ".join(row["notes"]) if row["notes"] else ""
                writer.writerow(row)

    # Print summary
    summary = output["summary"]
    print("\n" + "=" * 60)
    print("RECONCILIATION SUMMARY")
    print("=" * 60)
    print(f"Ledger records: {summary['total_ledger']}")
    print(f"Settlements: {summary['total_settlements']}")
    print(f"Bank transactions: {summary['total_bank']}")
    print(f"\n✅ Matched: {summary['matched']} ({summary['matched']/summary['total_ledger']*100:.1f}%)")
    print(f"⚠️  Duplicates: {summary['duplicates']}")
    print(f"❌ Unmatched: {summary['unmatched']}")
    print(f"💰 Amount variance: {summary['variance']}")
    print(f"⏰ Timing gaps: {summary['timing_gap']}")
    
    # Exception breakdown
    print("\n" + "=" * 60)
    print("EXCEPTION BREAKDOWN")
    print("=" * 60)
    exception_counts = {}
    for c in classifications:
        et = c["exception_type"]
        exception_counts[et] = exception_counts.get(et, 0) + 1
    
    for et, count in sorted(exception_counts.items(), key=lambda x: -x[1]):
        print(f"{et}: {count}")
    
    # Severity breakdown
    severity_counts = {}
    for c in classifications:
        sev = c["severity"]
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    
    print("\nBy Severity:")
    for sev in ["HIGH", "MEDIUM", "LOW"]:
        count = severity_counts.get(sev, 0)
        print(f"  {sev}: {count}")
    
    print("\n" + "=" * 60)
    print("OUTPUT FILES")
    print("=" * 60)
    print("📄 data/internal_ledger.csv")
    print("📄 data/razorpay_settlement.csv")
    print("📄 data/bank_statement.csv")
    print("📄 data/recon_results.json")
    print("📄 data/recon_summary_table.csv")
    print("📄 data/exception_classifications.json")
    print("📄 data/exception_classifications.csv")
    if llm_explanations_by_order_id:
        print("📄 data/llm_exception_explanations.json")
    print("\n✅ Pipeline complete!")


if __name__ == "__main__":
    run_pipeline()