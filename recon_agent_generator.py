"""
ReconAgent — Synthetic Dataset Generator (v2, realistic schema)

Models three data sources the way a real Indian e-commerce company using
Razorpay would actually maintain them:

1. internal_ledger.csv     — company's own ERP/accounting export (Tally/Zoho-style)
                              knows payment_id at checkout time (captured via webhook),
                              but does NOT know bank_ref_no/statement_no until AFTER
                              reconciliation — those are left blank, which is exactly
                              what this project's agent is meant to fill in.

2. razorpay_settlement.csv — modeled on Razorpay's real settlement report fields
                              (payment_id, settlement_id, settlement_utr, fee, tax,
                              settled, method, order_receipt) as documented in
                              Razorpay's own API/report docs.

3. bank_statement.csv      — modeled on a real Indian current-account statement
                              export (NEFT/IMPS/RTGS codes, Chq/Ref No column,
                              running balance, messy narration text). Critically,
                              the bank has NEVER heard of Razorpay's payment_id —
                              the only real link is settlement_utr, which appears
                              as the bank's own reference number (bank_ref_no).

Ground truth (which category was planted for each transaction) is saved to
data/ground_truth.json so accuracy_check.py can grade the engine honestly.
"""

import argparse
import csv
import json
import os
import random
import time
from datetime import date, timedelta
from faker import Faker

fake = Faker("en_IN")


def configure_seed(seed: int | None = None) -> int:
    if seed is None:
        seed = int(time.time_ns() % (2**32 - 1))
    random.seed(seed)
    Faker.seed(seed)
    return seed


SEED = configure_seed()

COMPANY_ACC_NO = "XXXXXXXX4521"
RAZORPAY_FEE_RATE = 0.0236  # ~2% + GST approximation, matches Razorpay's typical blended rate
START_DATE = date(2026, 8, 1)

ledger_rows = []
settlement_rows = []
bank_rows = []
ground_truth_rows = []

order_counter = 10000
settlement_counter = 50000
bank_sr_counter = 0
running_balance = 500000.00  # opening balance for the whole statement period


def next_order_id():
    global order_counter
    order_counter += 1
    return f"ORD-{order_counter}"


def next_payment_id():
    return "pay_" + fake.bothify(text="??########??").upper()


def next_settlement_id():
    global settlement_counter
    settlement_counter += 1
    return f"setl_{settlement_counter}"


def next_utr():
    return fake.numerify(text="##################")[:16]


def next_bank_sr():
    global bank_sr_counter
    bank_sr_counter += 1
    return bank_sr_counter


def compute_settlement(gross):
    fee = round(gross * RAZORPAY_FEE_RATE, 2)
    gst = round(fee * 0.18, 2)
    net = round(gross - fee - gst, 2)
    return fee, gst, net


def make_narration(utr, method_hint="NEFT"):
    templates = [
        f"{method_hint} CR-HDFC0001234-RAZORPAY SOFTWARE PVT LTD-{utr}",
        f"{method_hint}/RAZORPAY SOFTWARE PRIVATE LIMITED/{utr}",
        f"BY {method_hint} RAZORPAY-{utr}",
    ]
    return random.choice(templates)


def add_ledger_row(order_id, order_date, client_id, invoice_no, payment_id,
                    category, amount, currency="INR", status="completed"):
    gst_amount = round(amount * 18 / 118, 2)
    ledger_rows.append({
        "sr_no": len(ledger_rows) + 1,
        "order_id": order_id,
        "book_date": order_date,
        "value_date": order_date,
        "client_id": client_id,
        "invoice_no": invoice_no,
        "payment_ref": payment_id,
        "category": category,
        "transaction_currency": currency,
        "base_currency": "INR",
        "amount_transaction_ccy": amount,
        "amount_base_ccy": amount,
        "gst_amount": gst_amount,
        "acc_no": COMPANY_ACC_NO,
        "statement_no": "",
        "bank_ref_no": "",
        "status": status,
    })


def add_settlement_row(payment_id, order_id, invoice_no, gross, settle_date,
                        method="card", entity_type="payment"):
    fee, tax, net = compute_settlement(gross)
    settle_id = next_settlement_id()
    utr = next_utr()
    settlement_rows.append({
        "payment_id": payment_id,
        "order_id": order_id,
        "invoice_no": invoice_no,
        "entity_type": entity_type,
        "method": method,
        "gross_amount": gross,
        "fee": fee,
        "tax": tax,
        "net_amount": net,
        "currency": "INR",
        "settled": "true",
        "settlement_id": settle_id,
        "settlement_utr": utr,
        "created_at": settle_date - timedelta(days=2),
        "settled_at": settle_date,
    })
    return settle_id, utr, net


def add_bank_row(utr, credit_amount, value_date, method="NEFT"):
    global running_balance
    balance_before = running_balance
    running_balance = round(running_balance + credit_amount, 2)
    bank_rows.append({
        "sr_no": next_bank_sr(),
        "acc_no": COMPANY_ACC_NO,
        "statement_no": f"STMT-{value_date.strftime('%Y-%m')}",
        "statement_period": value_date.strftime("%B %Y"),
        "txn_date": value_date,
        "value_date": value_date,
        "transaction_code": method,
        "narration": make_narration(utr, method),
        "bank_ref_no": utr,
        "debit": "",
        "credit": credit_amount,
        "balance_before": balance_before,
        "balance_after": running_balance,
    })


def make_clean_transaction(order_date):
    order_id = next_order_id()
    payment_id = next_payment_id()
    invoice_no = f"INV-{order_id.split('-')[1]}"
    client_id = f"CUST-{random.randint(10000, 99999)}"
    amount = round(random.uniform(299, 9999), 2)
    settle_date = order_date + timedelta(days=2)

    add_ledger_row(order_id, order_date, client_id, invoice_no, payment_id, "SALES", amount)
    _, utr, net = add_settlement_row(payment_id, order_id, invoice_no, amount, settle_date)
    add_bank_row(utr, net, settle_date)

    ground_truth_rows.append({"order_id": order_id, "true_category": "CLEAN", "expected_match_status": "MATCHED"})


def make_partial_refund(order_date):
    order_id = next_order_id()
    payment_id = next_payment_id()
    invoice_no = f"INV-{order_id.split('-')[1]}"
    client_id = f"CUST-{random.randint(10000, 99999)}"
    full_amount = round(random.uniform(999, 7999), 2)
    refund_amount = round(full_amount * random.uniform(0.2, 0.6), 2)
    net_after_refund = round(full_amount - refund_amount, 2)
    settle_date = order_date + timedelta(days=3)

    add_ledger_row(order_id, order_date, client_id, invoice_no, payment_id, "SALES", full_amount,
                   status="partially_refunded")
    _, utr, net = add_settlement_row(payment_id, order_id, invoice_no, net_after_refund, settle_date)
    add_bank_row(utr, net, settle_date)

    ground_truth_rows.append({"order_id": order_id, "true_category": "PARTIAL_REFUND", "expected_match_status": "MATCHED"})


def make_duplicate_entry(order_date):
    order_id1 = next_order_id()
    order_id2 = next_order_id()
    payment_id = next_payment_id()
    invoice_no = f"INV-{order_id1.split('-')[1]}"
    client_id = f"CUST-{random.randint(10000, 99999)}"
    amount = round(random.uniform(499, 4999), 2)
    settle_date = order_date + timedelta(days=2)

    add_ledger_row(order_id1, order_date, client_id, invoice_no, payment_id, "SALES", amount)
    add_ledger_row(order_id2, order_date, client_id, invoice_no, payment_id, "SALES", amount)
    _, utr, net = add_settlement_row(payment_id, order_id1, invoice_no, amount, settle_date)
    add_bank_row(utr, net, settle_date)

    ground_truth_rows.append({"order_id": order_id1, "true_category": "DUPLICATE", "expected_match_status": "DUPLICATE"})
    ground_truth_rows.append({"order_id": order_id2, "true_category": "DUPLICATE", "expected_match_status": "DUPLICATE"})


def make_timing_gap(order_date):
    order_id = next_order_id()
    payment_id = next_payment_id()
    invoice_no = f"INV-{order_id.split('-')[1]}"
    client_id = f"CUST-{random.randint(10000, 99999)}"
    amount = round(random.uniform(299, 6999), 2)
    settle_date = order_date + timedelta(days=2)
    bank_date = settle_date + timedelta(days=random.randint(6, 10))

    add_ledger_row(order_id, order_date, client_id, invoice_no, payment_id, "SALES", amount)
    _, utr, net = add_settlement_row(payment_id, order_id, invoice_no, amount, settle_date)
    add_bank_row(utr, net, bank_date)

    ground_truth_rows.append({"order_id": order_id, "true_category": "TIMING_GAP", "expected_match_status": "TIMING_GAP"})


def make_fee_drift(order_date):
    order_id = next_order_id()
    payment_id = next_payment_id()
    invoice_no = f"INV-{order_id.split('-')[1]}"
    client_id = f"CUST-{random.randint(10000, 99999)}"
    amount = round(random.uniform(999, 8999), 2)
    settle_date = order_date + timedelta(days=2)

    add_ledger_row(order_id, order_date, client_id, invoice_no, payment_id, "SALES", amount)
    _, utr, net = add_settlement_row(payment_id, order_id, invoice_no, amount, settle_date)
    drift = round(random.uniform(3, 18), 2) * random.choice([-1, 1])
    add_bank_row(utr, round(net + drift, 2), settle_date)

    ground_truth_rows.append({"order_id": order_id, "true_category": "FEE_DRIFT", "expected_match_status": "VARIANCE"})


def make_pure_exception(order_date):
    order_id = next_order_id()
    payment_id = next_payment_id()
    invoice_no = f"INV-{order_id.split('-')[1]}"
    client_id = f"CUST-{random.randint(10000, 99999)}"
    amount = round(random.uniform(499, 5999), 2)

    add_ledger_row(order_id, order_date, client_id, invoice_no, payment_id, "SALES", amount)
    # deliberately: no settlement row, no bank row

    ground_truth_rows.append({"order_id": order_id, "true_category": "PURE_EXCEPTION", "expected_match_status": "UNMATCHED"})


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(seed: int | None = None):
    global SEED
    if seed is not None:
        SEED = configure_seed(seed)
    else:
        SEED = configure_seed()

    os.makedirs("data", exist_ok=True)

    plan = (
        [make_clean_transaction] * 35 +
        [make_partial_refund] * 8 +
        [make_duplicate_entry] * 4 +
        [make_timing_gap] * 5 +
        [make_fee_drift] * 4 +
        [make_pure_exception] * 4
    )
    random.shuffle(plan)

    current_date = START_DATE
    for fn in plan:
        current_date += timedelta(days=random.choice([0, 0, 1]))
        fn(current_date)

    bank_rows.sort(key=lambda r: r["value_date"])

    write_csv("data/internal_ledger.csv", ledger_rows, [
        "sr_no", "order_id", "book_date", "value_date", "client_id", "invoice_no",
        "payment_ref", "category", "transaction_currency", "base_currency",
        "amount_transaction_ccy", "amount_base_ccy", "gst_amount",
        "acc_no", "statement_no", "bank_ref_no", "status"
    ])
    write_csv("data/razorpay_settlement.csv", settlement_rows, [
        "payment_id", "order_id", "invoice_no", "entity_type", "method",
        "gross_amount", "fee", "tax", "net_amount", "currency", "settled",
        "settlement_id", "settlement_utr", "created_at", "settled_at"
    ])
    write_csv("data/bank_statement.csv", bank_rows, [
        "sr_no", "acc_no", "statement_no", "statement_period", "txn_date", "value_date",
        "transaction_code", "narration", "bank_ref_no", "debit", "credit",
        "balance_before", "balance_after"
    ])

    with open("data/ground_truth.json", "w", encoding="utf-8") as f:
        json.dump(ground_truth_rows, f, indent=2)

    print(f"Ledger rows: {len(ledger_rows)}")
    print(f"Settlement rows: {len(settlement_rows)}")
    print(f"Bank rows: {len(bank_rows)}")
    print(f"Ground truth rows: {len(ground_truth_rows)}")
    print("\nDone. Files written to data/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic reconciliation CSVs.")
    parser.add_argument("--seed", type=int, default=None, help="Optional deterministic seed for repeatable output.")
    args = parser.parse_args()
    main(seed=args.seed)