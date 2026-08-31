import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_actions import (
    ToolExecutionResult,
    append_workflow_action,
    create_action_plan,
    execute_action,
    get_order_memory,
    summarize_workflow_counts,
    validate_action,
)
from agent_controller import run_agent
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
            ["payment_id", "payment_ref", "settlement_id", "settlement_utr", "gross_amount", "razorpay_fee", "gst_on_fee", "net_amount", "settlement_date", "currency"],
        )
        bank_path = self.write_csv(
            directory,
            "bank.csv",
            bank,
            ["txn_id", "credited_amount", "value_date", "narration", "utr_number", "bank_ref_no"],
        )
        return reconcile(ledger_path, settlement_path, bank_path)

    def settlement(self, payment_ref, settlement_id):
        return {
            "payment_id": payment_ref,
            "payment_ref": payment_ref,
            "settlement_id": settlement_id,
            "settlement_utr": f"UTR-{settlement_id}",
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

    def test_workflow_status_assignment_and_summary_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_reconciliation(
                directory,
                [
                    {
                        "order_id": "ORD-3", "customer_name": "Test User", "order_date": "2026-08-01",
                        "amount_inr": "1000.00", "currency": "INR", "payment_ref": "pay_ABC12345ZZ", "status": "completed",
                    },
                    {
                        "order_id": "ORD-4", "customer_name": "Test User", "order_date": "2026-08-01",
                        "amount_inr": "1000.00", "currency": "INR", "payment_ref": "pay_ABC12345AA", "status": "completed",
                    },
                ],
                [
                    self.settlement("pay_ABC12345ZZ", "setl_10"),
                    self.settlement("pay_ABC12345AA", "setl_11"),
                ],
                [
                    {"txn_id": "BANK-1", "credited_amount": "976.40", "value_date": "2026-08-03",
                     "narration": "RAZORPAY SETL setl_10", "utr_number": "UTR-setl_10"},
                    {"txn_id": "BANK-2", "credited_amount": "976.40", "value_date": "2026-08-03",
                     "narration": "RAZORPAY SETL setl_11", "utr_number": "UTR-setl_11"},
                ],
            )

        result_one = output["results"][0]
        result_two = output["results"][1]

        self.assertIn(result_one["workflow_status"], {"Resolved", "Closed with explanation"})
        self.assertIn(result_two["workflow_status"], {"Resolved", "Closed with explanation"})
        self.assertIn("workflow_counts", output["summary"])
        self.assertGreaterEqual(output["summary"]["workflow_counts"].get("Closed with explanation", 0), 1)
        self.assertGreaterEqual(output["summary"]["workflow_counts"].get("Resolved", 0), 0)

    def test_exception_action_metadata_is_present(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_reconciliation(
                directory,
                [{
                    "order_id": "ORD-5", "customer_name": "Test User", "order_date": "2026-08-01",
                    "amount_inr": "1000.00", "currency": "INR", "payment_ref": "pay_MISSING_SETTLEMENT", "status": "completed",
                }],
                [],
                [],
            )

        result = output["results"][0]
        self.assertEqual(result["exception_type"], "MISSING_SETTLEMENT")
        self.assertEqual(result["recommended_action"], "Check Razorpay settlement status and retry reconciliation")
        self.assertEqual(result["owner"], "Finance Ops")
        self.assertEqual(result["sla_hours"], 24)
        self.assertIn("settlement status", result["justification"].lower())

    def test_cash_position_summary_includes_exposure_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_reconciliation(
                directory,
                [
                    {
                        "order_id": "ORD-6", "customer_name": "Test User", "order_date": "2026-08-01",
                        "amount_inr": "1000.00", "currency": "INR", "payment_ref": "pay_MATCHED", "status": "completed",
                    },
                    {
                        "order_id": "ORD-7", "customer_name": "Test User", "order_date": "2026-08-01",
                        "amount_inr": "2000.00", "currency": "INR", "payment_ref": "pay_MISSING_SETTLEMENT", "status": "completed",
                    },
                    {
                        "order_id": "ORD-8", "customer_name": "Test User", "order_date": "2026-08-01",
                        "amount_inr": "1500.00", "currency": "INR", "payment_ref": "pay_VARIANCE", "status": "completed",
                    },
                ],
                [
                    self.settlement("pay_MATCHED", "setl_20"),
                    self.settlement("pay_VARIANCE", "setl_21"),
                ],
                [
                    {"txn_id": "BANK-20", "credited_amount": "976.40", "value_date": "2026-08-03",
                     "narration": "RAZORPAY SETL setl_20", "utr_number": "UTR-setl_20"},
                    {"txn_id": "BANK-21", "credited_amount": "800.00", "value_date": "2026-08-03",
                     "narration": "RAZORPAY SETL setl_21", "utr_number": "UTR-setl_21"},
                ],
            )

        cash_position = output["summary"]["cash_position"]
        self.assertAlmostEqual(cash_position["cash_matched"], 976.4)
        self.assertAlmostEqual(cash_position["cash_unresolved"], 3500.0)
        self.assertGreaterEqual(cash_position["potential_cash_exposure"], 0)
        self.assertGreaterEqual(cash_position["open_exceptions"], 2)
        self.assertGreaterEqual(cash_position["settlement_pending"], 0)
        self.assertIn("High", cash_position["risk_exposure"])


class AgentActionTests(unittest.TestCase):
    def test_missing_settlement_creates_high_priority_finance_task(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "agent_action_log.json"
            plan = create_action_plan(
                {
                    "order_id": "ORD-AGENT-1",
                    "exception_type": "MISSING_SETTLEMENT",
                    "owner": "Finance Ops",
                    "sla_hours": 24,
                    "justification": "No settlement record was found.",
                    "notes": ["Ledger payment exists"],
                    "ledger_amount": 1200,
                    "workflow_status": "Auto-flagged",
                },
                log_path,
            )

        self.assertEqual(plan["decision"], "create_finance_task")
        self.assertEqual(plan["priority"], "HIGH")
        self.assertEqual(plan["status"], "Proposed")

    def test_approval_is_required_for_sensitive_action(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "agent_action_log.json"
            plan = create_action_plan(
                {"order_id": "ORD-AGENT-2", "exception_type": "FEE_DRIFT"},
                log_path,
            )
            plan["decision"] = "mark_exception_resolved"
            plan["requires_approval"] = True

            with self.assertRaises(PermissionError):
                execute_action(plan, log_path)

            executed = execute_action(plan, log_path, approved=True)

        self.assertEqual(executed["status"], "Executed")
        self.assertEqual(executed["approval"], "Approved by operator")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_action("delete_ledger_record")

    def test_workflow_actions_are_logged_separately_and_status_overrides_are_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow_path = Path(directory) / "workflow_actions.json"
            exception = {
                "order_id": "ORD-WF-1",
                "exception_type": "MISSING_SETTLEMENT",
                "workflow_status": "Auto-flagged",
                "owner": "Finance Ops",
                "sla_hours": 24,
                "justification": "No settlement record was seen.",
                "notes": ["Ledger exists but settlement is missing"],
                "ledger_amount": 1500,
            }

            entry = append_workflow_action(
                exception,
                action="escalate_exception",
                note="Escalated to finance ops after review.",
                log_path=workflow_path,
            )

            self.assertTrue(workflow_path.exists())
            self.assertEqual(entry["workflow_status"], "Escalated to finance ops")
            self.assertEqual(entry["action"], "escalate_exception")

            counts = summarize_workflow_counts(
                [{"order_id": "ORD-WF-1", "workflow_status": "Auto-flagged"}],
                workflow_log_path=workflow_path,
            )
            self.assertEqual(counts["Escalated to finance ops"], 1)

    def test_structured_memory_and_tool_execution_result_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow_path = Path(directory) / "workflow_actions.json"
            exception = {
                "order_id": "ORD-MEM-1",
                "exception_type": "MISSING_SETTLEMENT",
                "owner": "Finance Ops",
                "sla_hours": 24,
                "justification": "No settlement record was found.",
                "notes": ["Ledger exists"],
                "ledger_amount": 2000,
            }
            append_workflow_action(
                exception,
                action="create_finance_task",
                note="Task created for review.",
                log_path=workflow_path,
            )
            memory = get_order_memory("ORD-MEM-1", log_path=workflow_path)

            self.assertEqual(memory["order_id"], "ORD-MEM-1")
            self.assertEqual(memory["latest_status"], "Task created")
            self.assertEqual(memory["history"][0]["action"], "create_finance_task")

            result = ToolExecutionResult(
                order_id="ORD-MEM-1",
                action="create_finance_task",
                status="Executed",
                approved=True,
                workflow_status="Task created",
                reason="No settlement record was found.",
                note="Task created for review.",
            )
            payload = result.to_dict()
            self.assertEqual(payload["action"], "create_finance_task")
            self.assertTrue(payload["approved"])

    def test_agent_runs_investigate_act_verify_loop_without_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
                run = run_agent(
                    {
                        "order_id": "ORD-AGENT-3",
                        "exception_type": "MISSING_SETTLEMENT",
                        "ledger_amount": 1500,
                        "notes": ["No settlement found"],
                    },
                    action_log_path=directory_path / "actions.json",
                    run_log_path=directory_path / "runs.json",
                )

        self.assertEqual(run["investigation"]["source"], "policy fallback")
        self.assertEqual(run["decision"]["decision"], "create_finance_task")
        self.assertTrue(run["verification"]["verified"])
        self.assertEqual(run["action"]["status"], "Executed")


if __name__ == "__main__":
    unittest.main()
