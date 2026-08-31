"""Real external tool wrappers for the finance ops agent.

These are intentionally safe wrappers: they never modify source reconciliation
records, they gracefully fail when credentials or external services are unavailable,
and they return structured payloads that the agent can log and inspect.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _razorpay_auth() -> Dict[str, str]:
    key_id = _env("RAZORPAY_KEY_ID") or _env("RAZORPAY_KEY")
    key_secret = _env("RAZORPAY_KEY_SECRET") or _env("RAZORPAY_SECRET_KEY")
    return {"key_id": key_id, "key_secret": key_secret}


def lookup_razorpay_payment(payment_ref: str) -> Dict[str, Any]:
    """Look up a Razorpay payment by payment id/reference if a valid key exists."""
    creds = _razorpay_auth()
    key_id = creds.get("key_id")
    key_secret = creds.get("key_secret")
    if not key_id or not key_secret or not payment_ref:
        return {"status": "unavailable", "source": "razorpay", "error": "Razorpay API credentials missing."}

    try:
        response = requests.get(
            "https://api.razorpay.com/v1/payments",
            auth=(key_id, key_secret),
            params={"count": 10},
            timeout=15,
        )
        if response.status_code >= 400:
            return {"status": "error", "source": "razorpay", "error": response.text}

        items = response.json().get("items", [])
        for item in items:
            if item.get("id") == payment_ref or item.get("reference_id") == payment_ref:
                return {"status": "ok", "source": "razorpay", "payment": item}
        return {"status": "not_found", "source": "razorpay", "payment": None, "message": "Payment not found."}
    except Exception as exc:  # pragma: no cover - external API failure should not crash demo
        return {"status": "error", "source": "razorpay", "error": str(exc)}


def lookup_razorpay_settlement(payment_ref: str) -> Dict[str, Any]:
    """Look up the settlement linked to a payment reference when the upstream API is available."""
    creds = _razorpay_auth()
    key_id = creds.get("key_id")
    key_secret = creds.get("key_secret")
    if not key_id or not key_secret or not payment_ref:
        return {"status": "unavailable", "source": "razorpay", "error": "Razorpay API credentials missing."}

    try:
        payment = lookup_razorpay_payment(payment_ref)
        if payment.get("status") in {"ok", "not_found"} and payment.get("payment"):
            settlement_id = payment["payment"].get("settlement_id")
            if settlement_id:
                response = requests.get(
                    f"https://api.razorpay.com/v1/settlements/{settlement_id}",
                    auth=(key_id, key_secret),
                    timeout=15,
                )
                if response.status_code >= 400:
                    return {"status": "error", "source": "razorpay", "error": response.text}
                return {"status": "ok", "source": "razorpay", "settlement": response.json()}

        response = requests.get(
            "https://api.razorpay.com/v1/settlements",
            auth=(key_id, key_secret),
            params={"count": 10},
            timeout=15,
        )
        if response.status_code >= 400:
            return {"status": "error", "source": "razorpay", "error": response.text}

        items = response.json().get("items", [])
        for item in items:
            description = str(item.get("description") or "")
            if payment_ref in description:
                return {"status": "ok", "source": "razorpay", "settlement": item}

        return {"status": "not_found", "source": "razorpay", "settlement": None, "message": "Settlement not found."}
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "source": "razorpay", "error": str(exc)}


def get_github_repo() -> Dict[str, str]:
    owner = _env("GITHUB_OWNER") or "aryandhandhukiya"
    repo = _env("GITHUB_REPO") or "ReconAgent"
    return {"owner": owner, "repo": repo}


def create_github_issue(title: str, body: str, labels: Optional[List[str]] = None, repo: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Create an issue in the configured GitHub repository for finance task escalation."""
    token = _env("GITHUB_TOKEN")
    if not token:
        return {"status": "unavailable", "source": "github", "error": "GitHub token missing."}

    repo_cfg = repo or get_github_repo()
    payload = {
        "title": title,
        "body": body,
        "labels": labels or ["finance-ops", "auto-created"],
    }

    try:
        response = requests.post(
            f"https://api.github.com/repos/{repo_cfg['owner']}/{repo_cfg['repo']}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            data=json.dumps(payload),
            timeout=20,
        )
        if response.status_code >= 400:
            return {"status": "error", "source": "github", "error": response.text, "response": response.status_code}
        data = response.json()
        return {"status": "ok", "source": "github", "issue": data}
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "source": "github", "error": str(exc)}


def notify_slack(message: str, webhook_url: Optional[str] = None) -> Dict[str, Any]:
    """Optional notification tool for urgent finance escalations."""
    webhook = webhook_url or _env("SLACK_WEBHOOK_URL")
    if not webhook:
        return {"status": "unavailable", "source": "slack", "error": "Slack webhook missing."}

    try:
        response = requests.post(webhook, json={"text": message}, timeout=15)
        if response.status_code >= 400:
            return {"status": "error", "source": "slack", "error": response.text}
        return {"status": "ok", "source": "slack"}
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "source": "slack", "error": str(exc)}
