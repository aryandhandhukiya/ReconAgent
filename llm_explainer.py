"""
ReconAgent — LLM Exception Explainer (Gemini)

Replaces the hardcoded if/else classification in run_recon.py with
actual LLM reasoning, grounded strictly in the row's real numbers.

Setup:
    pip install google-generativeai --break-system-packages
    export GEMINI_API_KEY="your-key-here"

Usage:
    python llm_explainer.py
    (run AFTER run_recon.py, since it reads data/recon_results.json)
"""

import json
import os
import time
from typing import Any, Dict, List

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

MODEL_NAME = "gemini-2.0-flash"

SYSTEM_INSTRUCTION = """You are a finance-ops assistant explaining payment reconciliation
exceptions to a non-technical merchant. You will be given the exact structured data for
ONE transaction that failed to reconcile cleanly across ledger, settlement, and bank records.

Rules:
- Base your explanation ONLY on the numbers and fields provided. Never invent facts,
  amounts, or dates not present in the input.
- If the data doesn't clearly explain the exception, say so honestly instead of guessing.
- Write ONE short paragraph (2-3 sentences), plain English, no jargon.
- End with a one-line suggested next action.
- Do not use markdown formatting, just plain text.
"""


def configure_gemini():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable not set. "
            "Run: export GEMINI_API_KEY='your-key-here'"
        )
    genai.configure(api_key=api_key)


def build_prompt(result: Dict[str, Any]) -> str:
    """Build a prompt grounded strictly in this row's actual fields."""
    fields = {
        "order_id": result.get("order_id"),
        "payment_ref": result.get("payment_ref"),
        "match_status": result.get("match_status"),
        "exception_type": result.get("exception_type"),
        "ledger_amount": result.get("ledger_amount"),
        "ledger_status": result.get("ledger_status"),
        "settlement_found": result.get("settlement_found"),
        "settlement_net": result.get("settlement_net"),
        "bank_found": result.get("bank_found"),
        "bank_amount": result.get("bank_amount"),
        "amount_variance": result.get("amount_variance"),
        "days_to_settlement": result.get("days_to_settlement"),
        "days_to_bank": result.get("days_to_bank"),
        "match_method": result.get("match_method"),
        "confidence_score": result.get("confidence_score"),
        "notes": result.get("notes"),
    }
    return (
        "Here is the transaction data:\n\n"
        f"{json.dumps(fields, indent=2)}\n\n"
        "Explain what happened with this transaction and why it did not reconcile cleanly."
    )


def explain_exception(model, result: Dict[str, Any], retries: int = 3) -> str:
    """Call Gemini for one exception row, with retry on transient failure."""
    prompt = build_prompt(result)
    last_error = None
    for attempt in range(retries):
        try:
            response = model.generate_content(prompt)
            text = (response.text or "").strip()
            if text:
                return text
            last_error = "empty response"
        except Exception as e:  # noqa: BLE001 — we deliberately want to catch and retry any API error
            last_error = str(e)
            time.sleep(1.5 * (attempt + 1))
    return f"[LLM explanation unavailable after {retries} attempts: {last_error}]"


def explain_all_exceptions(results_path: str, output_path: str) -> List[Dict[str, Any]]:
    configure_gemini()
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    with open(results_path) as f:
        recon_output = json.load(f)

    results = recon_output["results"]
    exceptions = [r for r in results if r.get("match_status") != "MATCHED"]

    print(f"Found {len(exceptions)} non-matched transactions to explain...")

    explanations = []
    for i, result in enumerate(exceptions, start=1):
        print(f"  [{i}/{len(exceptions)}] {result['order_id']} ({result['match_status']})...")
        explanation_text = explain_exception(model, result)
        explanations.append({
            "order_id": result["order_id"],
            "payment_ref": result["payment_ref"],
            "match_status": result["match_status"],
            "exception_type": result.get("exception_type"),
            "llm_explanation": explanation_text,
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(explanations, f, indent=2)

    print(f"\nDone. Explanations saved to {output_path}")
    return explanations


if __name__ == "__main__":
    explain_all_exceptions(
        results_path="data/recon_results.json",
        output_path="data/llm_exception_explanations.json",
    )
