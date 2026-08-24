"""
ReconAgent — Synthetic Dataset Generator (Fixed Version)
"""

import csv
import json
import random
from datetime import date, timedelta
from faker import Faker
import os

fake = Faker("en_IN")
random.seed(42)
Faker.seed(42)

MDR_RATE = 0.02
GST_RATE = 0.18
START_DATE = date(2026, 8, 1)

ledger_rows = []
settlement_rows = []
bank_rows = []
ground_truth_rows = []  # order_id -> true category + expected engine outcome

order_counter = 10000
settlement_counter = 50000
bank_counter = 80000

def next_order_id():
    global order_counter
    order_counter += 1
    return f"ORD-{order_counter}"

def next_payment_ref():
    return "pay_" + fake.bothify(text="??########??").upper()

def next_settlement_id():
    global settlement_counter
    settlement_counter += 1
    return f"setl_{settlement_counter}"

def next_bank_txn():
    global bank_counter
    bank_counter += 1
    return f"BANKTXN-{bank_counter}"

def next_utr():
    return "UTR" + fake.numerify(text="##########")

def compute_settlement(gross):
    fee = round(gross * MDR_RATE, 2)
    gst = round(fee * GST_RATE, 2)
    net = round(gross - fee - gst, 2)
    return fee, gst, net

def make_clean_transaction(order_date):
    order_id = next_order_id()
    pay_ref = next_payment_ref()
    amount = round(random.uniform(299, 9999), 2)
    fee, gst, net = compute_settlement(amount)
    settle_id = next_settlement_id()
    settle_date = order_date + timedelta(days=2)

    ledger_rows.append({
        "order_id": order_id, "customer_name": fake.name(), "order_date": order_date,
        "amount_inr": amount, "currency": "INR", "payment_ref": pay_ref, "status": "completed"
    })
    settlement_rows.append({
        "payment_ref": pay_ref, "settlement_id": settle_id, "gross_amount": amount,
        "razorpay_fee": fee, "gst_on_fee": gst, "net_amount": net,
        "settlement_date": settle_date, "currency": "INR"
    })
    bank_rows.append({
        "txn_id": next_bank_txn(), "credited_amount": net, "value_date": settle_date,
        "narration": f"RAZORPAY SETL {settle_id}", "utr_number": next_utr()
    })
    ground_truth_rows.append({
        "order_id": order_id, "true_category": "CLEAN", "expected_match_status": "MATCHED"
    })

def make_partial_refund(order_date):
    order_id = next_order_id()
    pay_ref = next_payment_ref()
    full_amount = round(random.uniform(999, 7999), 2)
    refund_amount = round(full_amount * random.uniform(0.2, 0.6), 2)
    net_after_refund = round(full_amount - refund_amount, 2)
    fee, gst, net = compute_settlement(net_after_refund)
    settle_id = next_settlement_id()
    settle_date = order_date + timedelta(days=3)

    ledger_rows.append({
        "order_id": order_id, "customer_name": fake.name(), "order_date": order_date,
        "amount_inr": full_amount, "currency": "INR", "payment_ref": pay_ref,
        "status": "partially_refunded"
    })
    settlement_rows.append({
        "payment_ref": pay_ref, "settlement_id": settle_id, "gross_amount": net_after_refund,
        "razorpay_fee": fee, "gst_on_fee": gst, "net_amount": net,
        "settlement_date": settle_date, "currency": "INR"
    })
    bank_rows.append({
        "txn_id": next_bank_txn(), "credited_amount": net, "value_date": settle_date,
        "narration": f"RAZORPAY SETL {settle_id}", "utr_number": next_utr()
    })
    ground_truth_rows.append({
        "order_id": order_id, "true_category": "PARTIAL_REFUND", "expected_match_status": "MATCHED"
    })

def make_duplicate_entry(order_date):
    order_id1 = next_order_id()
    order_id2 = next_order_id()
    pay_ref = next_payment_ref()
    amount = round(random.uniform(499, 4999), 2)
    fee, gst, net = compute_settlement(amount)
    settle_id = next_settlement_id()
    settle_date = order_date + timedelta(days=2)
    customer = fake.name()

    ledger_rows.append({
        "order_id": order_id1, "customer_name": customer, "order_date": order_date,
        "amount_inr": amount, "currency": "INR", "payment_ref": pay_ref, "status": "completed"
    })
    ledger_rows.append({
        "order_id": order_id2, "customer_name": customer, "order_date": order_date,
        "amount_inr": amount, "currency": "INR", "payment_ref": pay_ref, "status": "completed"
    })
    settlement_rows.append({
        "payment_ref": pay_ref, "settlement_id": settle_id, "gross_amount": amount,
        "razorpay_fee": fee, "gst_on_fee": gst, "net_amount": net,
        "settlement_date": settle_date, "currency": "INR"
    })
    bank_rows.append({
        "txn_id": next_bank_txn(), "credited_amount": net, "value_date": settle_date,
        "narration": f"RAZORPAY SETL {settle_id}", "utr_number": next_utr()
    })
    ground_truth_rows.append({
        "order_id": order_id1, "true_category": "DUPLICATE", "expected_match_status": "DUPLICATE"
    })
    ground_truth_rows.append({
        "order_id": order_id2, "true_category": "DUPLICATE", "expected_match_status": "DUPLICATE"
    })

def make_timing_gap(order_date):
    order_id = next_order_id()
    pay_ref = next_payment_ref()
    amount = round(random.uniform(299, 6999), 2)
    fee, gst, net = compute_settlement(amount)
    settle_id = next_settlement_id()
    settle_date = order_date + timedelta(days=2)
    bank_date = settle_date + timedelta(days=random.randint(6, 10))

    ledger_rows.append({
        "order_id": order_id, "customer_name": fake.name(), "order_date": order_date,
        "amount_inr": amount, "currency": "INR", "payment_ref": pay_ref, "status": "completed"
    })
    settlement_rows.append({
        "payment_ref": pay_ref, "settlement_id": settle_id, "gross_amount": amount,
        "razorpay_fee": fee, "gst_on_fee": gst, "net_amount": net,
        "settlement_date": settle_date, "currency": "INR"
    })
    bank_rows.append({
        "txn_id": next_bank_txn(), "credited_amount": net, "value_date": bank_date,
        "narration": f"RAZORPAY SETL {settle_id}", "utr_number": next_utr()
    })
    ground_truth_rows.append({
        "order_id": order_id, "true_category": "TIMING_GAP", "expected_match_status": "TIMING_GAP"
    })

def make_fee_drift(order_date):
    order_id = next_order_id()
    pay_ref = next_payment_ref()
    amount = round(random.uniform(999, 8999), 2)
    fee, gst, net = compute_settlement(amount)
    drift = round(random.uniform(3, 18), 2) * random.choice([-1, 1])
    net_bank = round(net + drift, 2)
    settle_id = next_settlement_id()
    settle_date = order_date + timedelta(days=2)

    ledger_rows.append({
        "order_id": order_id, "customer_name": fake.name(), "order_date": order_date,
        "amount_inr": amount, "currency": "INR", "payment_ref": pay_ref, "status": "completed"
    })
    settlement_rows.append({
        "payment_ref": pay_ref, "settlement_id": settle_id, "gross_amount": amount,
        "razorpay_fee": fee, "gst_on_fee": gst, "net_amount": net,
        "settlement_date": settle_date, "currency": "INR"
    })
    bank_rows.append({
        "txn_id": next_bank_txn(), "credited_amount": net_bank, "value_date": settle_date,
        "narration": f"RAZORPAY SETL {settle_id}", "utr_number": next_utr()
    })
    ground_truth_rows.append({
        "order_id": order_id, "true_category": "FEE_DRIFT", "expected_match_status": "VARIANCE"
    })

def make_pure_exception(order_date):
    order_id = next_order_id()
    pay_ref = next_payment_ref()
    amount = round(random.uniform(499, 5999), 2)

    ledger_rows.append({
        "order_id": order_id, "customer_name": fake.name(), "order_date": order_date,
        "amount_inr": amount, "currency": "INR", "payment_ref": pay_ref, "status": "completed"
    })
    # deliberately no settlement / bank row
    ground_truth_rows.append({
        "order_id": order_id, "true_category": "PURE_EXCEPTION", "expected_match_status": "UNMATCHED"
    })

def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

def main():
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

    write_csv(
        "data/internal_ledger.csv", ledger_rows,
        ["order_id", "customer_name", "order_date", "amount_inr", "currency", "payment_ref", "status"]
    )

    write_csv(
        "data/razorpay_settlement.csv", settlement_rows,
        ["payment_ref", "settlement_id", "gross_amount", "razorpay_fee", "gst_on_fee",
         "net_amount", "settlement_date", "currency"]
    )

    write_csv(
        "data/bank_statement.csv", bank_rows,
        ["txn_id", "credited_amount", "value_date", "narration", "utr_number"]
    )

    with open("data/ground_truth.json", "w", encoding="utf-8") as f:
        json.dump(ground_truth_rows, f, indent=2)

    print(f"Ledger rows: {len(ledger_rows)}")
    print(f"Settlement rows: {len(settlement_rows)}")
    print(f"Bank rows: {len(bank_rows)}")
    print(f"Ground truth rows: {len(ground_truth_rows)}")
    print("\nDone. Files written to data/")

if __name__ == "__main__":
    main()