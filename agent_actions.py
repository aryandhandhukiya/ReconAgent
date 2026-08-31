"""Controlled agent actions for reconciliation exceptions.

The reconciliation engine remains the financial source of truth. This module
turns its exception output into safe, auditable workflow actions.
"""

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_LOG_PATH = Path("data/agent_action_log.json")
WORKFLOW_LOG_PATH = Path("data/workflow_actions.json")
MEMORY_DB_PATH = Path("data/agent_memory.db")
ALLOWED_ACTIONS = {
    "investigate_exception",
    "create_finance_task",
    "approve_action",
    "escalate_exception",
    "retry_reconciliation",
    "add_exception_note",
    "mark_exception_reviewed",
    "mark_exception_resolved",
}
APPROVAL_REQUIRED_ACTIONS = {
    "mark_exception_resolved",
}

WORKFLOW_STATUS_BY_ACTION = {
    "investigate_exception": "Under investigation",
    "approve_action": "Action approved",
    "create_finance_task": "Task created",
    "escalate_exception": "Escalated to finance ops",
    "retry_reconciliation": "Retry requested",
    "add_exception_note": "Note added",
    "mark_exception_reviewed": "Reviewed",
    "mark_exception_resolved": "Resolved",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _priority(exception: Dict[str, Any]) -> str:
    exception_type = exception.get("exception_type")
    amount = abs(float(exception.get("ledger_amount") or 0))

    if exception_type == "MISSING_SETTLEMENT" or amount >= 10000:
        return "HIGH"
    if exception_type in {"DUPLICATE_IN_LEDGER", "MISSING_BANK", "TIMING_GAP"}:
        return "MEDIUM"
    return "LOW"


def decision_policy(exception: Dict[str, Any]) -> Dict[str, Any]:
    """Clear agentic decision layer that maps evidence to a safe workflow action."""
    exception_type = exception.get("exception_type")
    amount = abs(float(exception.get("ledger_amount") or 0))
    action_by_type = {
        "MISSING_SETTLEMENT": "create_finance_task",
        "MISSING_BANK": "escalate_exception",
        "FEE_DRIFT": "create_finance_task",
        "TIMING_GAP": "escalate_exception",
        "DUPLICATE_IN_LEDGER": "create_finance_task",
    }
    action = action_by_type.get(exception_type, "create_finance_task")
    if amount >= 10000 and exception_type not in {"MISSING_BANK"}:
        action = "escalate_exception"
    return {
        "action": action,
        "priority": _priority(exception),
        "owner": exception.get("owner") or "Finance Ops",
        "sla_hours": exception.get("sla_hours") or 24,
        "requires_approval": action in APPROVAL_REQUIRED_ACTIONS,
        "reason": exception.get("justification") or "Exception requires finance follow-up.",
    }


def choose_action(exception: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compatible wrapper around the decision policy."""
    return decision_policy(exception)


def _load_log(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    return value if isinstance(value, list) else []


def _save_log(path: Path, entries: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(entries, file, indent=2)


def _should_use_sqlite(log_path: Path) -> bool:
    resolved = Path(log_path).resolve()
    workspace_root = Path.cwd().resolve()
    try:
        resolved.relative_to(workspace_root)
        return True
    except ValueError:
        return False


def _memory_db_path(log_path: Path = WORKFLOW_LOG_PATH) -> Path:
    return Path(log_path).parent / "agent_memory.db"


def _init_memory_db(db_path: Path) -> None:
    if not _should_use_sqlite(db_path):
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT,
                order_id TEXT,
                exception_type TEXT,
                action TEXT,
                workflow_status TEXT,
                priority TEXT,
                owner TEXT,
                sla_hours INTEGER,
                reason TEXT,
                note TEXT,
                created_at TEXT,
                raw_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                action TEXT,
                status TEXT,
                approved INTEGER,
                workflow_status TEXT,
                reason TEXT,
                note TEXT,
                created_at TEXT,
                raw_json TEXT
            )
            """
        )
        connection.commit()


def _save_workflow_memory(entry: Dict[str, Any], log_path: Path = WORKFLOW_LOG_PATH) -> None:
    db_path = _memory_db_path(log_path)
    if not _should_use_sqlite(log_path):
        return
    _init_memory_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO workflow_memory (
                workflow_id, order_id, exception_type, action, workflow_status,
                priority, owner, sla_hours, reason, note, created_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.get("workflow_id"),
                entry.get("order_id"),
                entry.get("exception_type"),
                entry.get("action"),
                entry.get("workflow_status"),
                entry.get("priority"),
                entry.get("owner"),
                entry.get("sla_hours"),
                entry.get("reason"),
                entry.get("note"),
                entry.get("created_at"),
                json.dumps(entry, ensure_ascii=False),
            ),
        )
        connection.commit()


def _save_tool_execution_memory(result: Dict[str, Any], log_path: Path = WORKFLOW_LOG_PATH) -> None:
    db_path = _memory_db_path(log_path)
    if not _should_use_sqlite(log_path):
        return
    _init_memory_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO tool_execution_log (
                order_id, action, status, approved, workflow_status, reason, note, created_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.get("order_id"),
                result.get("action"),
                result.get("status"),
                1 if result.get("approved") else 0,
                result.get("workflow_status"),
                result.get("reason"),
                result.get("note"),
                result.get("created_at") or _timestamp(),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        connection.commit()


def create_action_plan(
    exception: Dict[str, Any],
    log_path: Path = DEFAULT_LOG_PATH,
    requested_action: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Create and persist a proposed action without executing a financial change."""
    decision = choose_action(exception)
    if requested_action is not None:
        validate_action(requested_action)
        decision["action"] = requested_action
        decision["requires_approval"] = requested_action in APPROVAL_REQUIRED_ACTIONS
    existing = _load_log(log_path)
    action_id = f"ACT-{len(existing) + 1:04d}"
    entry = {
        "action_id": action_id,
        "order_id": exception.get("order_id"),
        "exception_type": exception.get("exception_type"),
        "workflow_status": exception.get("workflow_status", "Auto-flagged"),
        "decision": decision["action"],
        "priority": decision["priority"],
        "owner": decision["owner"],
        "sla_hours": decision["sla_hours"],
        "reason": decision["reason"],
        "evidence": exception.get("notes", []),
        "requires_approval": decision["requires_approval"],
        "note": note,
        "status": "Pending approval" if decision["requires_approval"] else "Proposed",
        "created_at": _timestamp(),
    }
    existing.append(entry)
    _save_log(log_path, existing)
    return entry


def validate_action(action: str) -> None:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported agent action: {action}")


def append_workflow_action(
    exception: Dict[str, Any],
    action: str,
    note: Optional[str] = None,
    log_path: Path = WORKFLOW_LOG_PATH,
) -> Dict[str, Any]:
    """Persist an operator action separately from the original reconciliation result."""
    validate_action(action)
    entries = _load_log(log_path)
    workflow_status = WORKFLOW_STATUS_BY_ACTION.get(action, "Queued")
    entry = {
        "workflow_id": f"WF-{len(entries) + 1:04d}",
        "order_id": exception.get("order_id"),
        "exception_type": exception.get("exception_type"),
        "action": action,
        "workflow_status": workflow_status,
        "priority": _priority(exception),
        "owner": exception.get("owner") or "Finance Ops",
        "sla_hours": exception.get("sla_hours") or 24,
        "reason": exception.get("justification") or "Operator workflow update.",
        "note": note or "",
        "created_at": _timestamp(),
    }
    entries.append(entry)
    _save_log(log_path, entries)
    _save_workflow_memory(entry, log_path)
    return entry


def summarize_workflow_counts(
    rows: List[Dict[str, Any]],
    workflow_log_path: Path = WORKFLOW_LOG_PATH,
) -> Dict[str, int]:
    """Return a merged workflow summary using the live workflow log and base results."""
    workflow_order = [
        "Closed with explanation",
        "Resolved",
        "Auto-flagged",
        "Escalated to finance ops",
        "Needs manual review",
        "Under investigation",
        "Action approved",
        "Task created",
        "Retry requested",
        "Note added",
        "Reviewed",
    ]
    counts = {state: 0 for state in workflow_order}
    for row in rows:
        status = row.get("workflow_status") or "Auto-flagged"
        counts[status] = counts.get(status, 0) + 1

    for entry in _load_log(workflow_log_path):
        status = entry.get("workflow_status")
        if status:
            counts[status] = counts.get(status, 0) + 1

    return counts


def execute_action(
    action_entry: Dict[str, Any],
    log_path: Path = DEFAULT_LOG_PATH,
    approved: bool = False,
) -> Dict[str, Any]:
    """Execute a workflow-only action and append the result to the audit log."""
    action = action_entry.get("decision")
    validate_action(action)
    if action_entry.get("requires_approval") and not approved:
        raise PermissionError(f"Action {action} requires human approval")

    entries = _load_log(log_path)
    updated = dict(action_entry)
    updated["status"] = "Executed"
    updated["executed_at"] = _timestamp()
    updated["approval"] = "Approved by operator" if approved else "Not required"
    entries.append({
        "action_id": updated["action_id"],
        "order_id": updated.get("order_id"),
        "decision": action,
        "status": updated["status"],
        "approval": updated["approval"],
        "note": updated.get("note"),
        "executed_at": updated["executed_at"],
    })
    _save_log(log_path, entries)
    _save_tool_execution_memory({
        "order_id": updated.get("order_id"),
        "action": action,
        "status": updated["status"],
        "approved": bool(approved),
        "workflow_status": WORKFLOW_STATUS_BY_ACTION.get(action, "Queued"),
        "reason": updated.get("reason") or "Action executed via agent workflow.",
        "note": updated.get("note") or "",
        "created_at": updated["executed_at"],
    }, log_path)
    return updated


def create_action_plans(
    exceptions: List[Dict[str, Any]],
    log_path: Path = DEFAULT_LOG_PATH,
) -> List[Dict[str, Any]]:
    """Create proposals for all exceptions in a reconciliation batch."""
    return [create_action_plan(exception, log_path) for exception in exceptions]


@dataclass
class ToolExecutionResult:
    """Typed result for a workflow tool execution, suitable for agent memory and audit logs."""
    order_id: str
    action: str
    status: str
    approved: bool = False
    workflow_status: str = "Auto-flagged"
    reason: str = ""
    note: str = ""
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = self.created_at or _timestamp()
        return payload


def get_order_memory(
    order_id: str,
    log_path: Path = WORKFLOW_LOG_PATH,
) -> Dict[str, Any]:
    """Return structured memory for one order: latest state, history, and summary."""
    if not order_id:
        return {"order_id": order_id, "latest_status": "Auto-flagged", "history": []}

    if not _should_use_sqlite(log_path):
        entries = _load_log(log_path)
        history = [entry for entry in entries if entry.get("order_id") == order_id]
        latest_status = history[-1].get("workflow_status") if history else "Auto-flagged"
        return {
            "order_id": order_id,
            "latest_status": latest_status,
            "history": history,
            "action_count": len(history),
            "last_action": history[-1].get("action") if history else None,
        }

    db_path = _memory_db_path(log_path)
    _init_memory_db(db_path)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT workflow_id, order_id, exception_type, action, workflow_status,
                   priority, owner, sla_hours, reason, note, created_at, raw_json
            FROM workflow_memory
            WHERE order_id = ?
            ORDER BY id ASC
            """,
            (order_id,),
        ).fetchall()

    if rows:
        history = []
        for row in rows:
            workflow_id, order_id_value, exception_type, action, workflow_status, priority, owner, sla_hours, reason, note, created_at, raw_json = row
            history.append({
                "workflow_id": workflow_id,
                "order_id": order_id_value,
                "exception_type": exception_type,
                "action": action,
                "workflow_status": workflow_status,
                "priority": priority,
                "owner": owner,
                "sla_hours": sla_hours,
                "reason": reason,
                "note": note,
                "created_at": created_at,
                "raw_json": raw_json,
            })
        latest_status = history[-1].get("workflow_status") if history else "Auto-flagged"
        return {
            "order_id": order_id,
            "latest_status": latest_status,
            "history": history,
            "action_count": len(history),
            "last_action": history[-1].get("action") if history else None,
        }

    entries = _load_log(log_path)
    history = [entry for entry in entries if entry.get("order_id") == order_id]
    latest_status = history[-1].get("workflow_status") if history else "Auto-flagged"
    return {
        "order_id": order_id,
        "latest_status": latest_status,
        "history": history,
        "action_count": len(history),
        "last_action": history[-1].get("action") if history else None,
    }


def get_order_history(
    order_id: str,
    log_path: Path = WORKFLOW_LOG_PATH,
) -> List[Dict[str, Any]]:
    """Backward-compatible wrapper returning the per-order workflow history."""
    return get_order_memory(order_id, log_path).get("history", [])


def get_latest_workflow_state(
    order_id: str,
    default: str = "Auto-flagged",
    log_path: Path = WORKFLOW_LOG_PATH,
) -> str:
    """Return the latest known workflow state for the order from memory."""
    return get_order_memory(order_id, log_path).get("latest_status") or default
