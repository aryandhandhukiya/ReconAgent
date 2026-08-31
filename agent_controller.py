"""Agent loop for synthetic reconciliation exceptions.

Gemini may investigate and select an action, but Python validates the decision
and executes only controlled local prototype tools. No source financial CSV is
modified by this module.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import agent_actions
import external_tools
from dotenv import load_dotenv


RUN_LOG_PATH = Path("data/agent_run_log.json")
ALLOWED_AGENT_ACTIONS = sorted(agent_actions.ALLOWED_ACTIONS)
load_dotenv()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fallback_investigation(exception: Dict[str, Any]) -> Dict[str, Any]:
    """Provide a deterministic result when Gemini is unavailable."""
    decision = agent_actions.choose_action(exception)
    return {
        "investigation": (
            f"The exception is {exception.get('exception_type', 'unknown')}. "
            f"Evidence: {exception.get('notes') or 'no additional notes'}"
        ),
        "action": decision["action"],
        "reason": decision["reason"],
        "confidence": 0.75,
        "source": "policy fallback",
    }


def investigate_with_gemini(exception: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Gemini for a structured action proposal, with a safe fallback."""
    if not os.environ.get("GEMINI_API_KEY"):
        return _fallback_investigation(exception)

    try:
        import google.generativeai as genai

        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        model = genai.GenerativeModel(
            model_name="gemini-3.6-flash",
            system_instruction=(
                "You are a finance operations agent. Analyze only the supplied "
                "reconciliation evidence. Return JSON with keys investigation, "
                "action, reason, confidence. action must be one of: "
                f"{', '.join(ALLOWED_AGENT_ACTIONS)}. Never invent amounts or dates."
            ),
        )
        prompt = (
            "Choose the safest next workflow action for this exception. "
            "Do not modify financial records. Evidence:\n"
            f"{json.dumps(exception, default=str, indent=2)}"
        )
        response = model.generate_content(prompt)
        raw = (response.text or "").strip().replace("```json", "").replace("```", "").strip()
        decision = json.loads(raw)
        action = decision.get("action")
        agent_actions.validate_action(action)
        return {
            "investigation": str(decision.get("investigation", "")),
            "action": action,
            "reason": str(decision.get("reason", "")),
            "confidence": float(decision.get("confidence", 0.0)),
            "source": "Gemini",
        }
    except Exception as error:  # noqa: BLE001 - prototype must retain a usable fallback
        fallback = _fallback_investigation(exception)
        fallback["fallback_reason"] = str(error)
        return fallback


def _execute_prototype_tool(
    plan: Dict[str, Any],
    exception: Dict[str, Any],
    log_path: Path,
) -> Dict[str, Any]:
    """Execute a local, non-financial simulation of the selected tool."""
    action = plan["decision"]
    if action == "mark_exception_resolved":
        return {
            "tool": action,
            "status": "Awaiting human approval",
            "message": "Resolution requires operator approval before closure.",
        }

    executed = agent_actions.execute_action(plan, log_path)
    return {
        "tool": action,
        "status": executed["status"],
        "message": f"Simulated {action} completed; source records were not modified.",
    }


def verify_action(action_result: Dict[str, Any]) -> Dict[str, Any]:
    """Verify the local prototype tool produced an auditable outcome."""
    verified = action_result.get("status") in {"Executed", "Awaiting human approval"}
    return {
        "verified": verified,
        "next_status": "Pending approval" if action_result["status"] == "Awaiting human approval" else "Action completed",
        "verification": "Action result was recorded in the agent audit log." if verified else "Action did not complete.",
    }


class FinanceOpsAgent:
    """Minimal agentic loop for finance reconciliation exceptions."""

    def __init__(
        self,
        action_log_path: Path = agent_actions.DEFAULT_LOG_PATH,
        workflow_log_path: Path = agent_actions.WORKFLOW_LOG_PATH,
        run_log_path: Path = RUN_LOG_PATH,
    ) -> None:
        self.action_log_path = Path(action_log_path)
        self.workflow_log_path = Path(workflow_log_path)
        self.run_log_path = Path(run_log_path)
        self.tool_registry = {
            "investigate_exception": self._tool_investigate,
            "create_finance_task": self._tool_create_task,
            "approve_action": self._tool_approve_action,
            "escalate_exception": self._tool_escalate,
            "retry_reconciliation": self._tool_retry,
            "add_exception_note": self._tool_note,
            "mark_exception_reviewed": self._tool_review,
            "mark_exception_resolved": self._tool_resolve,
        }

    def observe(self, exception: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "order_id": exception.get("order_id"),
            "exception_type": exception.get("exception_type"),
            "workflow_status": agent_actions.get_latest_workflow_state(
                exception.get("order_id", ""),
                default=exception.get("workflow_status", "Auto-flagged"),
                log_path=self.workflow_log_path,
            ),
            "owner": exception.get("owner") or "Finance Ops",
            "sla_hours": exception.get("sla_hours") or 24,
            "notes": exception.get("notes") or [],
            "history": agent_actions.get_order_history(
                exception.get("order_id", ""),
                log_path=self.workflow_log_path,
            ),
        }

    def investigate(self, exception: Dict[str, Any]) -> Dict[str, Any]:
        return investigate_with_gemini(exception)

    def decide(self, exception: Dict[str, Any]) -> Dict[str, Any]:
        return agent_actions.choose_action(exception)

    def _tool_investigate(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        result = agent_actions.append_workflow_action(
            exception,
            action="investigate_exception",
            note=note,
            log_path=self.workflow_log_path,
        )
        payment_ref = exception.get("payment_ref")
        if payment_ref:
            settlement = external_tools.lookup_razorpay_settlement(str(payment_ref))
            result["external_context"] = {"razorpay_settlement": settlement}
        return result

    def _tool_create_task(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        result = agent_actions.append_workflow_action(
            exception,
            action="create_finance_task",
            note=note,
            log_path=self.workflow_log_path,
        )
        issue_title = f"Finance task: {exception.get('order_id') or 'Unknown order'}"
        issue_body = (
            f"Order: {exception.get('order_id')}\n"
            f"Exception: {exception.get('exception_type')}\n"
            f"Reason: {exception.get('justification') or result.get('reason')}\n"
            f"Note: {note or ''}"
        )
        issue = external_tools.create_github_issue(issue_title, issue_body, ["finance-ops", "auto-created"])
        result["external_context"] = {"github_issue": issue}
        return result

    def _tool_approve_action(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        result = agent_actions.append_workflow_action(
            exception,
            action="approve_action",
            note=note,
            log_path=self.workflow_log_path,
        )
        result["external_context"] = {"approval": "local_operator_approval"}
        return result

    def _tool_escalate(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        result = agent_actions.append_workflow_action(
            exception,
            action="escalate_exception",
            note=note,
            log_path=self.workflow_log_path,
        )
        issue_title = f"Escalation: {exception.get('order_id') or 'Unknown order'}"
        issue_body = (
            f"Order: {exception.get('order_id')}\n"
            f"Exception: {exception.get('exception_type')}\n"
            f"Reason: {exception.get('justification') or result.get('reason')}\n"
            f"Note: {note or ''}"
        )
        issue = external_tools.create_github_issue(issue_title, issue_body, ["severity:high", "finance-ops"]) 

        payment_ref = exception.get("payment_ref") or exception.get("payment_id") or "unknown"
        slack_message = (
            f"Recon exception detected for payment {payment_ref}. "
            "Payment captured but settlement/bank credit is delayed."
        )
        slack = external_tools.notify_slack(slack_message)
        result["external_context"] = {"github_issue": issue, "slack": slack}
        return result

    def _tool_retry(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        return agent_actions.append_workflow_action(
            exception,
            action="retry_reconciliation",
            note=note,
            log_path=self.workflow_log_path,
        )

    def _tool_note(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        return agent_actions.append_workflow_action(
            exception,
            action="add_exception_note",
            note=note,
            log_path=self.workflow_log_path,
        )

    def _tool_review(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        return agent_actions.append_workflow_action(
            exception,
            action="mark_exception_reviewed",
            note=note,
            log_path=self.workflow_log_path,
        )

    def _tool_resolve(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        return agent_actions.append_workflow_action(
            exception,
            action="mark_exception_resolved",
            note=note,
            log_path=self.workflow_log_path,
        )

    def act(self, exception: Dict[str, Any], action: str, note: Optional[str] = None) -> Dict[str, Any]:
        tool = self.tool_registry.get(action)
        if tool is None:
            raise ValueError(f"Tool not registered for action: {action}")
        workflow_entry = tool(exception, note=note)
        plan = agent_actions.create_action_plan(
            exception,
            self.action_log_path,
            requested_action=action,
            note=note,
        )
        execution = agent_actions.execute_action(
            plan,
            self.action_log_path,
            approved=action in {"mark_exception_resolved", "approve_action"},
        )
        return {
            "workflow": workflow_entry,
            "plan": plan,
            "execution": execution,
            "tool": action,
        }

    def verify(self, action_result: Dict[str, Any]) -> Dict[str, Any]:
        return verify_action(action_result["execution"])

    def run(self, exception: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        observed = self.observe(exception)
        investigation = self.investigate(exception)
        default_decision = self.decide(exception)
        chosen_action = investigation.get("action") or default_decision["action"]
        action_result = self.act(exception, chosen_action, note=note)
        verification = self.verify(action_result)
        run = {
            "run_id": f"RUN-{_timestamp().replace(':', '').replace('-', '')}",
            "order_id": exception.get("order_id"),
            "observed_exception": observed,
            "investigation": investigation,
            "decision": action_result["plan"],
            "action": action_result["execution"],
            "verification": verification,
            "created_at": _timestamp(),
        }

        existing: List[Dict[str, Any]] = []
        if self.run_log_path.exists():
            with self.run_log_path.open(encoding="utf-8") as file:
                existing = json.load(file)
        existing.append(run)
        self.run_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.run_log_path.open("w", encoding="utf-8") as file:
            json.dump(existing, file, indent=2)
        return run


def run_agent(
    exception: Dict[str, Any],
    action_log_path: Path = agent_actions.DEFAULT_LOG_PATH,
    run_log_path: Path = RUN_LOG_PATH,
) -> Dict[str, Any]:
    """Run observe -> investigate -> decide -> act -> verify for one exception."""
    agent = FinanceOpsAgent(
        action_log_path=action_log_path,
        run_log_path=run_log_path,
    )
    return agent.run(exception)
