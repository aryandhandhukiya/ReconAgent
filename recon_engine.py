
"""
ReconAgent — Core Deterministic Reconciliation Engine

This module performs exact-match reconciliation across three data sources:
1. Internal ledger (merchant's order records)
2. Payment gateway settlement (Razorpay-style)
3. Bank statement (actual credits)

Output: Reconciliation results with match status, variances, and exception flags.
"""

import csv
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Optional
import json
from difflib import SequenceMatcher


def load_csv(path: str) -> List[Dict[str, str]]:
    """Load a CSV file and return as list of dicts."""
    with open(path) as f:
        return list(csv.DictReader(f))


def parse_date(date_str: str) -> datetime:
    """Parse date string (YYYY-MM-DD) to datetime.date."""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def parse_amount(amount_str: str) -> float:
    """Parse amount string to float."""
    return float(amount_str)


def _find_fuzzy_settlement(
    ledger_row: Dict[str, str],
    settlements: List[Dict[str, str]],
    used_settlement_ids: set,
    amount_tolerance: float,
    date_window: int,
) -> tuple[Optional[Dict[str, str]], float, List[Dict[str, Any]]]:
    """Find a uniquely supported settlement when the payment reference differs."""
    ledger_amount = parse_amount(ledger_row["amount_inr"])
    ledger_date = parse_date(ledger_row["order_date"])
    ledger_ref = ledger_row["payment_ref"]
    candidates = []

    for settlement in settlements:
        if settlement["settlement_id"] in used_settlement_ids:
            continue

        amount_delta = abs(ledger_amount - parse_amount(settlement["gross_amount"]))
        settlement_date = parse_date(settlement["settlement_date"])
        date_delta = abs((settlement_date - ledger_date).days)
        if amount_delta > amount_tolerance or date_delta > date_window:
            continue

        reference_similarity = SequenceMatcher(
            None, ledger_ref.lower(), settlement["payment_ref"].lower()
        ).ratio()
        amount_score = max(0.0, 1.0 - amount_delta / amount_tolerance)
        date_score = max(0.0, 1.0 - date_delta / (date_window + 1))
        score = (
            reference_similarity * 0.55
            + amount_score * 0.30
            + date_score * 0.15
        )
        candidates.append({
            "settlement_id": settlement["settlement_id"],
            "payment_ref": settlement["payment_ref"],
            "reference_similarity": round(reference_similarity, 4),
            "amount_delta": round(amount_delta, 2),
            "date_delta": date_delta,
            "score": round(score, 4),
        })

    candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    if not candidates:
        return None, 0.0, []

    best = candidates[0]
    second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
    is_unique = best["score"] - second_score >= 0.12
    has_reference_evidence = best["reference_similarity"] >= 0.75
    if best["score"] < 0.78 or not is_unique or not has_reference_evidence:
        return None, best["score"], candidates[:3]

    settlement = next(
        item for item in settlements
        if item["settlement_id"] == best["settlement_id"]
    )
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
    """
    Perform 3-way reconciliation.

    Args:
        ledger_path: Path to internal_ledger.csv
        settlement_path: Path to razorpay_settlement.csv
        bank_path: Path to bank_statement.csv
        variance_threshold: Amount variance (₹) to flag as exception
        timing_gap_threshold: Days threshold to flag timing gap
        fuzzy_amount_tolerance: Maximum gross amount difference for fuzzy matching
        fuzzy_date_window: Maximum order-to-settlement date distance for fuzzy matching

    Returns:
        Dict with reconciliation results and summary
    """

    # Load data
    ledger = load_csv(ledger_path)
    settlements = load_csv(settlement_path)
    bank = load_csv(bank_path)

    # Build indexes
    settlement_by_payment_ref = {}
    for s in settlements:
        pref = s["payment_ref"]
        if pref not in settlement_by_payment_ref:
            settlement_by_payment_ref[pref] = s
        else:
            # Duplicate settlement - rare but possible
            pass

    bank_by_settlement_id = {}
    for b in bank:
        narration = b["narration"]
        if "RAZORPAY SETL" in narration:
            parts = narration.split()
            if len(parts) >= 3:
                settle_id = parts[2]
                bank_by_settlement_id[settle_id] = b

    ledger_count_by_payment_ref = defaultdict(list)
    for l in ledger:
        ledger_count_by_payment_ref[l["payment_ref"]].append(l)

    # Reconciliation
    results = []
    matched_settlement_refs = set()
    matched_settlement_ids = set()
    matched_bank_settlement_ids = set()
    fuzzy_match_log = []

    for l in ledger:
        payment_ref = l["payment_ref"]
        ledger_amount = parse_amount(l["amount_inr"])
        ledger_date = parse_date(l["order_date"])
        status = l["status"]

        result = {
            "order_id": l["order_id"],
            "payment_ref": payment_ref,
            "ledger_amount": ledger_amount,
            "ledger_date": str(ledger_date),
            "ledger_status": status,
            "match_status": None,
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
            "notes": [],
            "match_method": None,
            "confidence_score": 0.0,
            "fuzzy_match_candidates": []
        }

        # Check for duplicate in ledger
        if len(ledger_count_by_payment_ref[payment_ref]) > 1:
            result["exception_type"] = "DUPLICATE_IN_LEDGER"
            result["notes"].append(
                f"Payment ref {payment_ref} appears {len(ledger_count_by_payment_ref[payment_ref])} times in ledger"
            )

        # Match ledger to settlement
        if payment_ref in settlement_by_payment_ref:
            result["settlement_found"] = True
            s = settlement_by_payment_ref[payment_ref]
            matched_settlement_refs.add(payment_ref)
            matched_settlement_ids.add(s["settlement_id"])
            result["match_method"] = "EXACT_REFERENCE"
            result["confidence_score"] = 1.0

        else:
            s, fuzzy_score, candidates = _find_fuzzy_settlement(
                l,
                settlements,
                matched_settlement_ids,
                fuzzy_amount_tolerance,
                fuzzy_date_window,
            )
            result["fuzzy_match_candidates"] = candidates
            if s is not None:
                result["settlement_found"] = True
                matched_settlement_ids.add(s["settlement_id"])
                matched_settlement_refs.add(s["payment_ref"])
                result["match_method"] = "FUZZY_REFERENCE"
                result["confidence_score"] = fuzzy_score
                result["notes"].append(
                    f"Fuzzy settlement match: {payment_ref} -> {s['payment_ref']}"
                )
            elif candidates:
                fuzzy_match_log.append({
                    "order_id": l["order_id"],
                    "payment_ref": payment_ref,
                    "reason": "REJECTED_POSSIBLE_FALSE_POSITIVE",
                    "candidates": candidates,
                })
                result["notes"].append(
                    "Possible fuzzy match rejected: candidate was ambiguous or low confidence"
                )

        if result["settlement_found"]:
            result["settlement_id"] = s["settlement_id"]
            result["settlement_net"] = parse_amount(s["net_amount"])
            settlement_date = parse_date(s["settlement_date"])
            result["days_to_settlement"] = (settlement_date - ledger_date).days

            # Partial refund check
            if status == "partially_refunded":
                settlement_gross = parse_amount(s["gross_amount"])
                if settlement_gross < ledger_amount:
                    result["notes"].append(
                        f"Partial refund: ledger ₹{ledger_amount}, settlement gross ₹{settlement_gross}"
                    )

            # Match settlement to bank
            if s["settlement_id"] in bank_by_settlement_id:
                result["bank_found"] = True
                b = bank_by_settlement_id[s["settlement_id"]]
                matched_bank_settlement_ids.add(s["settlement_id"])

                result["bank_amount"] = parse_amount(b["credited_amount"])
                result["bank_date"] = b["value_date"]
                bank_date = parse_date(b["value_date"])
                result["days_to_bank"] = (bank_date - ledger_date).days

                # Amount variance
                if result["settlement_net"] is not None and result["bank_amount"] is not None:
                    variance = result["bank_amount"] - result["settlement_net"]
                    result["amount_variance"] = round(variance, 2)

                    if abs(variance) > variance_threshold:
                        result["notes"].append(
                            f"Amount variance: bank ₹{result['bank_amount']} vs settlement ₹{result['settlement_net']} (₹{variance:.2f})"
                        )

                # Timing gap
                if result["days_to_bank"] and result["days_to_bank"] > timing_gap_threshold:
                    result["notes"].append(
                        f"Timing gap: bank credit {result['days_to_bank']} days after order"
                    )
            else:
                result["notes"].append(f"Settlement {s['settlement_id']} not found in bank")
        else:
            result["exception_type"] = "MISSING_SETTLEMENT"
            result["notes"].append(f"No settlement found for {payment_ref}")

        # Determine match status
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
        elif result["settlement_found"] and not result["bank_found"]:
            result["match_status"] = "SETTLEMENT_ONLY"
            if result["exception_type"] is None:
                result["exception_type"] = "MISSING_BANK"
        else:
            result["match_status"] = "UNMATCHED"

        results.append(result)

    # Summary
    summary = {
        "total_ledger": len(ledger),
        "total_settlements": len(settlements),
        "total_bank": len(bank),
        "matched": sum(1 for r in results if r["match_status"] == "MATCHED"),
        "duplicates": sum(1 for r in results if r["match_status"] == "DUPLICATE"),
        "unmatched": sum(1 for r in results if r["match_status"] == "UNMATCHED"),
        "variance": sum(1 for r in results if r["match_status"] == "VARIANCE"),
        "timing_gap": sum(1 for r in results if r["match_status"] == "TIMING_GAP"),
        "settlement_only": sum(1 for r in results if r["match_status"] == "SETTLEMENT_ONLY"),
        "unmatched_settlements": len(settlement_by_payment_ref.keys() - matched_settlement_refs),
        "unmatched_bank": len(bank_by_settlement_id.keys() - matched_bank_settlement_ids)
    }

    return {
        "results": results,
        "summary": summary,
        "fuzzy_match_log": fuzzy_match_log
    }


def save_results(recon_output: Dict[str, Any], output_path: str):
    """Save reconciliation results to JSON."""
    with open(output_path, "w") as f:
        json.dump(recon_output, f, indent=2)


def print_summary(recon_output: Dict[str, Any]):
    """Print reconciliation summary to console."""
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
    # Example usage
    output = reconcile(
        "data/internal_ledger.csv",
        "data/razorpay_settlement.csv",
        "data/bank_statement.csv"
    )

    save_results(output, "data/recon_results.json")
    print_summary(output)
    print("\nResults saved to data/recon_results.json")
