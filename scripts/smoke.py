"""S5 smoke test: all 20 sample tickets through the LIVE pipeline (real Gemini) into a temp DB,
compared with the plan's expected outcomes. Add --seed to also load them into the real triage.db.

Model output can vary run to run, so category mismatches are reported, not hidden.
Hard invariants (SLA from table, PII masking, idempotency, junk rejection) must always hold."""
import csv
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEED = "--seed" in sys.argv
if not SEED:
    os.environ["TRIAGE_DB"] = str(Path(tempfile.mkdtemp(prefix="triage_smoke_")) / "t.db")

from app import db, llm, service, triage  # noqa: E402

if llm.get_api_key() is None:
    sys.exit("No real GEMINI_API_KEY in .env - smoke test needs the live model.")

# expected primary category; None = must be rejected; "REVIEW" = must need human review
EXPECT = {
    "T001": "CHEQUE_RETURN_QUERY", "T002": "DUPLICATE_DEBIT", "T003": "FAILED_TXN_DEBITED",
    "T004": "UNAUTHORISED_TXN", "T005": "WRONG_BENEFICIARY", "T006": "CHARGEBACK_STATUS",
    "T007": "OTHER_NOT_A_DISPUTE", "T008": "FAILED_TXN_DEBITED", "T009": "REVIEW",
    "T010": "CHEQUE_RETURN_QUERY", "T011": "UNAUTHORISED_TXN", "T012": "FAILED_TXN_DEBITED",
    "T013": None, "T014": None, "T015": "CHEQUE_RETURN_QUERY", "T016": "WRONG_BENEFICIARY",
    "T017": "OTHER_NOT_A_DISPUTE", "T018": "DUPLICATE_DEBIT", "T019": "CHEQUE_RETURN_QUERY",
    "T020": "UNAUTHORISED_TXN",
}
SECONDARY = {"T011": {"DUPLICATE_DEBIT"}}  # must also appear in the identified issues

rows = []
with open(ROOT / "tickets_sample.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

conn = db.init_db(db.DB_PATH)
cats = db.get_categories(conn)
hard_fail, mismatch = [], []
results = {}
print(f"DB: {db.DB_PATH}\nModel: {llm.get_model()}\n")
hdr = f"{'ID':5} {'expected':20} {'got':20} {'prio':8} {'sla':4} {'conf':5} {'status':19} notes"
print(hdr + "\n" + "-" * len(hdr))

t_start = time.time()
for r in rows:
    tid, text = r["ticket_id"], r["text"]
    exp = EXPECT[tid]
    try:
        ticket, dup = service.triage_ticket(conn, text, r["channel"], tid, actor="smoke")
    except triage.InputRejected as e:
        results[tid] = None
        ok = exp is None
        print(f"{tid:5} {'REJECTED':20} {e.code:20} {'-':8} {'-':4} {'-':5} {'-':19} {'' if ok else 'UNEXPECTED REJECT'}")
        if not ok:
            hard_fail.append(f"{tid} rejected but expected {exp}")
        continue
    results[tid] = ticket
    ex = ticket["extracted"]
    got = ticket["category"] or "REVIEW"
    notes = []
    if exp is None:
        hard_fail.append(f"{tid} should have been rejected")
        notes.append("SHOULD BE REJECTED")
    elif got != exp:
        mismatch.append(f"{tid}: expected {exp}, got {got} ({ex['review_reasons'] or ticket['confidence']})")
        notes.append("MISMATCH")
    if ticket["flags"]:
        notes.append("flags=" + ",".join(ticket["flags"]))
    if ex["review_reasons"]:
        notes.append("reasons=" + ",".join(ex["review_reasons"]))
    extra = [i["category"] for i in ex["issues"] if i["category"] != ticket["category"]]
    if extra:
        notes.append("also=" + ",".join(dict.fromkeys(extra)))
    conf = f"{ticket['confidence']:.2f}" if ticket["confidence"] is not None else "-"
    print(f"{tid:5} {str(exp):20} {got:20} {ticket['priority']:8} {ticket['sla_hours']:<4} {conf:5} {ticket['status']:19} {' '.join(notes)}")
print(f"\n{len(rows)} tickets in {time.time() - t_start:.0f}s")

# ---------- hard invariants (must hold regardless of model wording) ----------
for tid, t in results.items():
    if t is None:
        continue
    if t["category"]:
        row = cats[t["category"]]
        if (t["priority"], t["sla_hours"]) != (row["default_priority"], row["sla_hours"]):
            hard_fail.append(f"{tid}: priority/SLA do not match the categories table")
    else:
        if (t["priority"], t["sla_hours"]) != (triage.REVIEW_PRIORITY, triage.REVIEW_SLA_HOURS):
            hard_fail.append(f"{tid}: review fallback priority/SLA wrong")
    dl = datetime.fromisoformat(t["sla_deadline"]) - datetime.fromisoformat(t["ingested_at"])
    if dl != timedelta(hours=t["sla_hours"]):
        hard_fail.append(f"{tid}: deadline != ingested_at + sla_hours")
    if t["status"] == "APPROVED":
        hard_fail.append(f"{tid}: auto-approved")

t9 = results["T009"]
if not (t9["category"] is None and t9["status"] == "NEEDS_HUMAN_REVIEW" and "POSSIBLE_PROMPT_INJECTION" in t9["flags"]):
    hard_fail.append("T009 injection not flagged into human review")
t11 = results["T011"]
if t11 and "DUPLICATE_DEBIT" not in {i["category"] for i in t11["extracted"]["issues"]}:
    mismatch.append("T011: secondary issue DUPLICATE_DEBIT not listed")
t19 = results["T019"]
if t19 and not any("address" in (i["detail"] or "").lower() for i in t19["extracted"]["issues"]):
    mismatch.append("T019: address-change request not noted in issues")

t15 = results["T015"]
blob = json.dumps(t15)
if "50100234567891" in blob or "9876543210" in blob:
    hard_fail.append("T015 raw account/mobile leaked in API shape")
if not t15["extracted"]["pii"]["mobiles"] or not t15["extracted"]["pii"]["accounts"]:
    hard_fail.append("T015 PII not detected")
if results["T005"]["extracted"]["pii"]["mobiles"]:
    hard_fail.append("T005 reference misread as a mobile number")

# ---------- idempotency: second pass must create nothing ----------
before = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
dups = 0
for r in rows:
    try:
        _, dup = service.triage_ticket(conn, r["text"], r["channel"], r["ticket_id"], actor="smoke")
        dups += dup
    except triage.InputRejected:
        pass
after = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
accepted = sum(1 for t in results.values() if t)
print(f"idempotency: {before} rows -> {after} rows after re-submitting all 20; {dups}/{accepted} flagged duplicate")
if not (before == after == accepted and dups == accepted):
    hard_fail.append("re-submit created rows or was not flagged duplicate")
dup_events = conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='DUPLICATE_SUBMIT'").fetchone()[0]
created_events = conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='CREATED'").fetchone()[0]
if (created_events, dup_events) != (accepted, accepted):
    hard_fail.append(f"audit counts wrong: CREATED={created_events}, DUPLICATE_SUBMIT={dup_events}")

# ---------- summary ----------
reviews = [k for k, t in results.items() if t and t["category"] is None]
print(f"needs human review: {reviews}")
print(f"\nCATEGORY/DETAIL MISMATCHES ({len(mismatch)}):" if mismatch else "\nno category mismatches")
for m in mismatch:
    print("  -", m)
print(f"HARD FAILURES ({len(hard_fail)}):" if hard_fail else "no hard failures")
for m in hard_fail:
    print("  -", m)
conn.close()
sys.exit(1 if hard_fail else 0)
