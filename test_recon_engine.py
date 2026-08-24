import csv
import tempfile
import unittest
from pathlib import Path

from recon_engine import reconcile


class FuzzyMatchingTests(unittest.TestCase):
    def write_csv(self, directory, name, rows, fieldnames):
        path = Path(directory) / name
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return str(path)

    def run_reconciliation(self, directory, ledger, settlements, bank):
        ledger_path = self.write_csv(
            directory,
            "ledger.csv",
            ledger,
            ["order_id", "customer_name", "order_date", "amount_inr", "currency", "payment_ref", "status"],
        )
        settlement_path = self.write_csv(
            directory,
            "settlements.csv",
            settlements,
            ["payment_ref", "settlement_id", "gross_amount", "razorpay_fee", "gst_on_fee", "net_amount", "settlement_date", "currency"],
        )
        bank_path = self.write_csv(
            directory,
            "bank.csv",
            bank,
            ["txn_id", "credited_amount", "value_date", "narration", "utr_number"],
        )
        return reconcile(ledger_path, settlement_path, bank_path)

    def settlement(self, payment_ref, settlement_id):
        return {
            "payment_ref": payment_ref,
            "settlement_id": settlement_id,
            "gross_amount": "1000.00",
            "razorpay_fee": "20.00",
            "gst_on_fee": "3.60",
            "net_amount": "976.40",
            "settlement_date": "2026-08-03",
            "currency": "INR",
        }

    def test_fuzzy_reference_match_has_confidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_reconciliation(
                directory,
                [{
                    "order_id": "ORD-1", "customer_name": "Test User", "order_date": "2026-08-01",
                    "amount_inr": "1000.00", "currency": "INR", "payment_ref": "pay_ABC12345XY", "status": "completed",
                }],
                [self.settlement("pay_ABC12345XZ", "setl_1")],
                [{
                    "txn_id": "BANK-1", "credited_amount": "976.40", "value_date": "2026-08-03",
                    "narration": "RAZORPAY SETL setl_1", "utr_number": "UTR1",
                }],
            )

        result = output["results"][0]
        self.assertEqual(result["match_method"], "FUZZY_REFERENCE")
        self.assertGreaterEqual(result["confidence_score"], 0.78)
        self.assertEqual(output["fuzzy_match_log"], [])

    def test_ambiguous_fuzzy_candidate_is_rejected_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_reconciliation(
                directory,
                [{
                    "order_id": "ORD-2", "customer_name": "Test User", "order_date": "2026-08-01",
                    "amount_inr": "1000.00", "currency": "INR", "payment_ref": "pay_ABC12345XY", "status": "completed",
                }],
                [
                    self.settlement("pay_ABC12345XZ", "setl_2"),
                    self.settlement("pay_ABC12345XW", "setl_3"),
                ],
                [],
            )

        result = output["results"][0]
        self.assertEqual(result["match_status"], "UNMATCHED")
        self.assertEqual(result["match_method"], None)
        self.assertEqual(output["fuzzy_match_log"][0]["reason"], "REJECTED_POSSIBLE_FALSE_POSITIVE")
        self.assertEqual(len(output["fuzzy_match_log"][0]["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
