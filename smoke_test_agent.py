"""Reusable smoke test for the ReconAgent workflow.

This script exercises the local agent loop and optionally the live external tools:
- GitHub issue creation
- Slack webhook notification
- Razorpay payment lookup

How to use:
    python smoke_test_agent.py
    python smoke_test_agent.py --payment-id <razorpay_payment_id>

If you do not have a Razorpay payment ID yet, the script prints the exact sandbox
setup flow to create one in Razorpay test mode.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from agent_controller import run_agent
from external_tools import create_github_issue, lookup_razorpay_payment, notify_slack

load_dotenv()

DEFAULT_DEMO_PAYMENT_ID = "pay_TWL0UQLntTiGhC"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def razorpay_sandbox_steps() -> str:
    return """Razorpay sandbox flow:
1. Open Razorpay Dashboard.
2. Switch to test / sandbox mode if available.
3. Create a test payment or transaction in the dashboard or through a checkout flow using a Razorpay test key.
4. Wait for the payment to appear in the Payments list.
5. Copy the payment_id value.
6. Run:
   python smoke_test_agent.py --payment-id <actual_payment_id>

If you use a fake ID like 'pay_test_123', you will get a 'not_found' response because no such payment exists."""


def build_demo_exceptions(payment_id: str) -> list[dict]:
    """Return realistic demo exceptions that can be explained with a real Razorpay payment lookup."""
    return [
        {
            "order_id": "ORD-DEMO-101",
            "exception_type": "TIMING_GAP",
            "payment_ref": payment_id,
            "ledger_amount": 6060.79,
            "bank_amount": 5892.01,
            "owner": "Treasury",
            "sla_hours": 72,
            "workflow_status": "Auto-flagged",
            "notes": [
                f"The payment {payment_id} was captured in Razorpay, but the bank credit arrived after the expected settlement window.",
                "This looks like settlement timing rather than a value mismatch.",
            ],
            "justification": "Payment exists and is successful, but bank credit is delayed beyond the standard settlement timing window.",
        },
        {
            "order_id": "ORD-DEMO-102",
            "exception_type": "MISSING_SETTLEMENT",
            "payment_ref": payment_id,
            "ledger_amount": 1500.0,
            "bank_amount": 0.0,
            "owner": "Finance Ops",
            "sla_hours": 24,
            "workflow_status": "Auto-flagged",
            "notes": [
                f"Ledger shows a completed order with payment {payment_id}, but no settlement record is visible yet.",
                "This is a classic settlement-follow-up case.",
            ],
            "justification": "The payment exists in Razorpay but no settlement has been posted to the settlement feed or bank statement yet.",
        },
        {
            "order_id": "ORD-DEMO-103",
            "exception_type": "FEE_DRIFT",
            "payment_ref": payment_id,
            "ledger_amount": 2000.0,
            "bank_amount": 1958.0,
            "owner": "Finance Ops",
            "sla_hours": 24,
            "workflow_status": "Auto-flagged",
            "notes": [
                f"The payment {payment_id} was captured and settled, but the net amount differs slightly from the bank credit.",
                "This can be explained by fees, GST, or a minor settlement adjustment.",
            ],
            "justification": "The payment is valid and the settlement exists, but the bank amount is off by a small variance consistent with fee or tax drift.",
        },
    ]


def smoke_test_agent(payment_id: str | None = None) -> dict:
    """Run the agent smoke test and optionally validate live external tools."""
    payment_id = payment_id or DEFAULT_DEMO_PAYMENT_ID
    result = {
        "agent": None,
        "github": None,
        "slack": None,
        "razorpay": None,
        "demo": {"payment_id": payment_id, "scenarios": []},
        "warnings": [],
    }

    print_section("1) Agent flow smoke test")
    sample_exception = {
        "order_id": "ORD-SMOKE-001",
        "exception_type": "MISSING_SETTLEMENT",
        "ledger_amount": 1500,
        "notes": ["No settlement found for the ledger payment."],
        "owner": "Finance Ops",
        "sla_hours": 24,
        "justification": "No settlement record was seen in the ledger or settlement feed.",
    }

    agent_result = run_agent(
        sample_exception,
        action_log_path=Path("data/smoke_agent_actions.json"),
        run_log_path=Path("data/smoke_agent_runs.json"),
    )
    result["agent"] = agent_result
    print(json.dumps({
        "decision": agent_result.get("decision", {}).get("decision"),
        "action": agent_result.get("action", {}).get("status"),
        "verification": agent_result.get("verification", {}).get("verified"),
    }, indent=2))

    demo_scenarios = build_demo_exceptions(payment_id)
    print_section("2) Live Razorpay demo scenarios")
    for index, scenario in enumerate(demo_scenarios, start=1):
        run = run_agent(
            scenario,
            action_log_path=Path(f"data/demo_agent_actions_{index}.json"),
            run_log_path=Path(f"data/demo_agent_runs_{index}.json"),
        )
        result["demo"]["scenarios"].append({
            "scenario_index": index,
            "order_id": scenario["order_id"],
            "exception_type": scenario["exception_type"],
            "decision": run["decision"]["decision"],
            "action_status": run["action"]["status"],
            "reason": run["investigation"].get("reason"),
        })
        print(json.dumps({
            "scenario": index,
            "order_id": scenario["order_id"],
            "exception_type": scenario["exception_type"],
            "decision": run["decision"]["decision"],
            "reason": run["investigation"].get("reason"),
        }, indent=2))

    print_section("3) External tool checks")
    github_token = _env("GITHUB_TOKEN")
    if github_token:
        github_issue = create_github_issue(
            "ReconAgent smoke test",
            "This is a smoke test from the reusable agent flow script.",
            ["finance-ops"],
        )
        result["github"] = github_issue
        print(json.dumps(github_issue, indent=2))
    else:
        result["warnings"].append("GITHUB_TOKEN missing; GitHub API check skipped.")
        print("GitHub: skipped (missing GITHUB_TOKEN)")

    slack_webhook = _env("SLACK_WEBHOOK_URL")
    if slack_webhook:
        slack_result = notify_slack("ReconAgent smoke test message from smoke_test_agent.py")
        result["slack"] = slack_result
        print(json.dumps(slack_result, indent=2))
    else:
        result["warnings"].append("SLACK_WEBHOOK_URL missing; Slack check skipped.")
        print("Slack: skipped (missing SLACK_WEBHOOK_URL)")

    razorpay_key_id = _env("RAZORPAY_KEY_ID") or _env("RAZORPAY_KEY")
    razorpay_key_secret = _env("RAZORPAY_KEY_SECRET") or _env("RAZORPAY_SECRET_KEY")
    if razorpay_key_id and razorpay_key_secret:
        if payment_id:
            razorpay_result = lookup_razorpay_payment(payment_id)
            result["razorpay"] = razorpay_result
            print(json.dumps(razorpay_result, indent=2))
        else:
            result["warnings"].append(
                "RAZORPAY credentials configured, but no payment_id provided. Use --payment-id <id> to test a live lookup."
            )
            print("Razorpay: credentials found, but no payment_id supplied.")
            print(razorpay_sandbox_steps())
    else:
        result["warnings"].append("Razorpay credentials missing; payment lookup skipped.")
        print("Razorpay: skipped (missing RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)")
        print(razorpay_sandbox_steps())

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the ReconAgent smoke test workflow.")
    parser.add_argument(
        "--payment-id",
        dest="payment_id",
        default=DEFAULT_DEMO_PAYMENT_ID,
        help="Use a real Razorpay payment_id for the live payment lookup and demo scenarios. Defaults to the demo payment ID pay_TWL0UQLntTiGhC.",
    )
    args = parser.parse_args()

    summary = smoke_test_agent(payment_id=args.payment_id)
    print_section("4) Final summary")
    print(json.dumps({
        "agent_verified": bool(summary["agent"] and summary["agent"].get("verification", {}).get("verified")),
        "demo_payment_id": summary["demo"]["payment_id"],
        "demo_scenarios": len(summary["demo"]["scenarios"]),
        "github_status": summary["github"].get("status") if summary["github"] else "skipped",
        "slack_status": summary["slack"].get("status") if summary["slack"] else "skipped",
        "razorpay_status": summary["razorpay"].get("status") if summary["razorpay"] else "skipped",
        "warnings": summary["warnings"],
    }, indent=2))
