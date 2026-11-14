"""S1 check: deterministic core against the 20 samples (no LLM, no network)."""
import csv
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, triage  # noqa: E402

conn = db.init_db(":memory:")
cats = db.get_categories(conn)
assert len(cats) == 7, cats

samples = {}
with open(ROOT / "tickets_sample.csv", newline="", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        samples[r["ticket_id"]] = r["text"]

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# --- input gate
for tid in ("T013", "T014"):
    try:
        triage.validate_input(samples[tid])
        check(f"{tid} rejected", False)
    except triage.InputRejected as e:
        check(f"{tid} rejected ({e.code})", True)
for junk in (None, "", "   \n\t ", "abc", "!!!!!!!!!!!!!!!!!!!!", "refund"):
    try:
        triage.validate_input(junk)
        check(f"junk {junk!r} rejected", False)
    except triage.InputRejected:
        check(f"junk {junk!r} rejected", True)

# --- PII
red, pii = triage.mask_pii(samples["T015"])
check("T015 account masked", "50100234567891" not in red and "XXXXXXXXXX7891" in red)
check("T015 mobile masked", "9876543210" not in red and "XXXXXX3210" in red)
check("T015 cheque no kept", "55102" in red)
red5, pii5 = triage.mask_pii(samples["T005"])
check("T005 ref 7745100293 NOT masked as mobile", "7745100293" in red5 and not pii5["mobiles"])
red1, _ = triage.mask_pii(samples["T001"])
check("T001 cheque 482913 and 45,000 untouched", "482913" in red1 and "45,000" in red1)
check("raw PII absent from pii dict", all("9876543210" not in v for v in pii["mobiles"]))

# --- extraction
d1 = triage.extract_details(samples["T001"])
check("T001 cheque + amount + date", d1["cheque_numbers"] == ["482913"]
      and [a["value"] for a in d1["amounts"]] == [45000.0] and d1["dates"] == ["3 Nov"])
d2 = triage.extract_details(samples["T002"])
check("T002 amount 1250, clock times ignored", [a["value"] for a in d2["amounts"]] == [1250.0])
d5 = triage.extract_details(samples["T005"])
check("T005 ref + amount 15000", d5["references"] == ["7745100293"]
      and [a["value"] for a in d5["amounts"]] == [15000.0])
d6 = triage.extract_details(samples["T006"])
check("T006 ref DSP-20931", d6["references"] == ["DSP-20931"])
d8 = triage.extract_details(samples["T008"])
check("T008 Hinglish amount 3200", [a["value"] for a in d8["amounts"]] == [3200.0])
d9 = triage.extract_details(samples["T009"])
check("T009 amount 5,00,000", [a["value"] for a in d9["amounts"]] == [500000.0])
d15 = triage.extract_details(samples["T015"])
check("T015 cheque 55102, no account/mobile as amount", d15["cheque_numbers"] == ["55102"]
      and not d15["amounts"])

# --- injection
check("T009 flagged", triage.flag_injection(samples["T009"]) == ["POSSIBLE_PROMPT_INJECTION"])
clean = [t for t, s in samples.items() if t != "T009" and s and triage.flag_injection(s)]
check(f"no false positives on other samples {clean}", not clean)

# --- severity + SLA
check("T011 primary = UNAUTHORISED_TXN",
      triage.pick_primary(["DUPLICATE_DEBIT", "UNAUTHORISED_TXN"], cats) == "UNAUTHORISED_TXN")
check("tie broken by sla_hours", triage.pick_primary(["FAILED_TXN_DEBITED", "WRONG_BENEFICIARY"], cats)
      in ("FAILED_TXN_DEBITED", "WRONG_BENEFICIARY"))
check("unknown category ignored", triage.pick_primary(["MADE_UP"], cats) is None
      and triage.pick_primary(["MADE_UP", "CHEQUE_RETURN_QUERY"], cats) == "CHEQUE_RETURN_QUERY")
t0 = triage.utcnow()
sla = triage.compute_sla("UNAUTHORISED_TXN", t0, cats)
check("UNAUTHORISED_TXN = CRITICAL/4h, deadline = ingest + 4h exactly",
      sla["priority"] == "CRITICAL" and sla["sla_hours"] == 4
      and sla["sla_deadline"] == (t0 + timedelta(hours=4)).isoformat())
sla_none = triage.compute_sla(None, t0, cats)
check("no category -> review fallback HIGH/24h", (sla_none["priority"], sla_none["sla_hours"]) == ("HIGH", 24))

# --- idempotency key
check("hash ignores whitespace only",
      triage.content_hash("a  b\n c") == triage.content_hash("a b c")
      and triage.content_hash("a b c") != triage.content_hash("A b c"))

print("\n--- all 20 samples ---")
for tid, text in samples.items():
    try:
        t = triage.validate_input(text)
    except triage.InputRejected as e:
        print(f"{tid}: REJECTED {e.code}")
        continue
    red, pii = triage.mask_pii(t)
    d = triage.extract_details(t)
    print(f"{tid}: amounts={[a['value'] for a in d['amounts']]} cheques={d['cheque_numbers']} "
          f"refs={d['references']} dates={d['dates'] + d['relative_dates']} "
          f"pii={pii['mobiles'] + pii['accounts']} flags={triage.flag_injection(t)}")

print("\nFAILURES:" if failures else "\nALL CHECKS PASSED", failures or "")
sys.exit(1 if failures else 0)
