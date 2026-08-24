"""
ReconAgent — Accuracy Checker

Compares the reconciliation engine's actual output against the KNOWN ground
truth (the generator knows exactly what category it planted for every
transaction, since it planted it deliberately).

This turns "43/64 matched" from a vanity number into a real accuracy metric:
did the engine correctly identify WHY each transaction did or didn't match?

Usage:
    python run_recon.py          # produces data/recon_results.json
    python accuracy_check.py     # compares it against data/ground_truth.json
"""

import json
from collections import defaultdict


def load_json(path):
    with open(path) as f:
        return json.load(f)


def check_accuracy(results_path="data/recon_results.json", ground_truth_path="data/ground_truth.json"):
    recon_output = load_json(results_path)
    ground_truth = load_json(ground_truth_path)

    results_by_order_id = {r["order_id"]: r for r in recon_output["results"]}
    gt_by_order_id = {g["order_id"]: g for g in ground_truth}

    if set(results_by_order_id) != set(gt_by_order_id):
        missing_in_results = set(gt_by_order_id) - set(results_by_order_id)
        missing_in_gt = set(results_by_order_id) - set(gt_by_order_id)
        print("⚠️  WARNING: order_id sets don't match between results and ground truth.")
        if missing_in_results:
            print(f"   In ground truth but not in results: {sorted(missing_in_results)}")
        if missing_in_gt:
            print(f"   In results but not in ground truth: {sorted(missing_in_gt)}")
        print("   (Did you regenerate data without re-running reconciliation, or vice versa?)\n")

    rows = []
    for order_id, gt in gt_by_order_id.items():
        result = results_by_order_id.get(order_id)
        if result is None:
            continue
        rows.append({
            "order_id": order_id,
            "true_category": gt["true_category"],
            "expected_match_status": gt["expected_match_status"],
            "actual_match_status": result["match_status"],
            "correct": gt["expected_match_status"] == result["match_status"],
        })

    total = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    overall_accuracy = correct / total if total else 0.0

    # Per-category breakdown (precision/recall style, using each true_category as its own class)
    by_category = defaultdict(lambda: {"total": 0, "correct": 0, "wrong_examples": []})
    for r in rows:
        cat = r["true_category"]
        by_category[cat]["total"] += 1
        if r["correct"]:
            by_category[cat]["correct"] += 1
        else:
            by_category[cat]["wrong_examples"].append(r)

    # Confusion matrix: expected_match_status -> actual_match_status -> count
    confusion = defaultdict(lambda: defaultdict(int))
    for r in rows:
        confusion[r["expected_match_status"]][r["actual_match_status"]] += 1

    # Print report
    print("=" * 60)
    print("ACCURACY REPORT (engine output vs. known ground truth)")
    print("=" * 60)
    print(f"\nOverall accuracy: {correct}/{total} = {overall_accuracy*100:.1f}%\n")

    print("By true category:")
    for cat, stats in sorted(by_category.items()):
        acc = stats["correct"] / stats["total"] if stats["total"] else 0.0
        print(f"  {cat:16s}  {stats['correct']}/{stats['total']}  ({acc*100:.1f}%)")

    print("\nConfusion matrix (rows = expected, columns = actual):")
    all_statuses = sorted({s for row in confusion.values() for s in row} | set(confusion.keys()))
    header = "  " + " ".join(f"{s[:10]:>10s}" for s in all_statuses)
    print(header)
    for expected in all_statuses:
        line = f"{expected[:14]:14s}"
        for actual in all_statuses:
            line += f"{confusion[expected][actual]:>11d}"
        print(line)

    misclassified = [r for r in rows if not r["correct"]]
    if misclassified:
        print(f"\n{len(misclassified)} misclassified transaction(s):")
        for r in misclassified:
            print(f"  {r['order_id']}: true={r['true_category']} "
                  f"expected={r['expected_match_status']} got={r['actual_match_status']}")
    else:
        print("\nNo misclassifications — every transaction resolved exactly as expected.")

    report = {
        "overall_accuracy": overall_accuracy,
        "total": total,
        "correct": correct,
        "by_category": {
            cat: {"total": s["total"], "correct": s["correct"],
                  "accuracy": s["correct"] / s["total"] if s["total"] else 0.0}
            for cat, s in by_category.items()
        },
        "misclassified": misclassified,
    }
    with open("data/accuracy_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("\nFull report saved to data/accuracy_report.json")

    return report


if __name__ == "__main__":
    check_accuracy()