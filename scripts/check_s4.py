"""S4 check: drive the real UI in Edge via Playwright against an in-process server
with a fake LLM and a temp DB. Needs: pip install playwright (uses the installed Edge).
Screenshots go to $SHOT_DIR (default: temp dir)."""
import csv
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp(prefix="triage_s4_")
os.environ["TRIAGE_DB"] = str(Path(tmp) / "t.db")
SHOTS = Path(os.environ.get("SHOT_DIR") or tmp)
SHOTS.mkdir(parents=True, exist_ok=True)

import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from app import main  # noqa: E402

PORT = 8091
BASE = f"http://127.0.0.1:{PORT}"
failures = []
calls = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def fake_llm(system, user, schema):
    calls.append(user)
    out = {"categories": ["DUPLICATE_DEBIT"], "confidence": 0.9,
           "summary": "Customer reports a duplicate debit.",
           "draft_reply": "Dear customer, we are sorry for the trouble. Our team is reviewing this.",
           "issues": [{"category": "DUPLICATE_DEBIT", "detail": "charged twice"}]}
    if "recognise" in user:
        out["categories"] = ["DUPLICATE_DEBIT", "UNAUTHORISED_TXN"]
        out["issues"] = [{"category": "DUPLICATE_DEBIT", "detail": "799 charged twice"},
                         {"category": "UNAUTHORISED_TXN", "detail": "4,500 not recognised"}]
    if "IGNORE ALL" in user:
        out.update(categories=["CHARGEBACK_STATUS"], confidence=0.99, issues=[])
    if "onerror" in user:
        out.update(categories=["OTHER_NOT_A_DISPUTE"], summary="<script>window.__xss=1</script> hello",
                   issues=[{"category": "OTHER_NOT_A_DISPUTE", "detail": "<b>bold</b>"}])
    return json.dumps(out)


main.app.state.llm_call = fake_llm
server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.1)
assert server.started, "server did not start"

samples = {}
with open(ROOT / "tickets_sample.csv", newline="", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        samples[r["ticket_id"]] = r

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="msedge", headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
    page = ctx.new_page()
    js_errors = []
    page.on("pageerror", lambda e: js_errors.append(str(e)))
    # 4xx are expected (junk input -> 422); a 5xx or a real JS error is a bug.
    page.on("response", lambda r: r.status >= 500 and js_errors.append(f"HTTP {r.status} {r.url}"))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
            and js_errors.append(m.text))
    page.goto(BASE)

    # ---------- load ----------
    expect(page.locator("#sample option")).to_have_count(21)
    expect(page.locator("#f-cat option")).to_have_count(9)
    check("page loads, 20 samples + 7 categories (+All/Unassigned)", True)
    check("empty history message", "No tickets" in page.locator("#rows").inner_text())
    check("result card hidden initially", page.locator("#result").is_hidden())

    # ---------- junk: server message shown, nothing created ----------
    page.select_option("#sample", "T013")
    page.click("#analyze")
    expect(page.locator("#intake-err")).to_contain_text("empty")
    page.select_option("#sample", "T014")
    page.click("#analyze")
    expect(page.locator("#intake-err")).to_contain_text("too short")
    check("T013/T014 show rejection, no LLM call, no row", calls == [] and "No tickets" in page.locator("#rows").inner_text())

    # ---------- multi-issue + double-click ----------
    page.select_option("#sample", "T011")
    check("sample fills textarea + channel", "recognise" in page.input_value("#text") and page.input_value("#channel") == "app")
    page.dblclick("#analyze")
    expect(page.locator("#result h2")).to_contain_text("Ticket #1")
    check("double-click Analyze -> exactly one LLM call", len(calls) == 1)
    expect(page.locator("#analyze")).to_be_enabled()      # re-enabled once history has refreshed
    check("analyze button re-enabled afterwards", True)
    check("category = UNAUTHORISED_TXN", page.input_value("#cat") == "UNAUTHORISED_TXN")
    res = page.locator("#result").inner_text()
    check("CRITICAL badge + SLA 4h", "CRITICAL" in res and "SLA 4h" in res)
    check("both issues listed", "DUPLICATE_DEBIT: 799 charged twice" in res and "UNAUTHORISED_TXN: 4,500 not recognised" in res)
    check("amounts + relative date extracted", "₹799" in res and "₹4,500" in res and "yesterday" in res)
    check("history has 1 row", page.locator("#rows tr").count() == 1 and "#1" in page.locator("#rows").inner_text())
    expect(page.locator("#audit li")).to_have_count(1)
    page.screenshot(path=str(SHOTS / "s4_desktop_result.png"), full_page=True)

    # ---------- re-submit same text ----------
    page.click("#analyze")
    expect(page.locator("#result .banner.info")).to_contain_text("already triaged")
    check("re-submit shows duplicate banner, still 1 row, no new LLM call",
          page.locator("#rows tr").count() == 1 and len(calls) == 1)
    expect(page.locator("#audit li")).to_have_count(2)

    # ---------- edit category: SLA recomputed ----------
    check("save disabled until something changes", page.locator("#save").is_disabled())
    page.select_option("#cat", "DUPLICATE_DEBIT")
    expect(page.locator("#cat-note")).to_contain_text("recomputed")
    check("save enabled after change", page.locator("#save").is_enabled())
    page.click("#save")
    expect(page.locator("#result")).to_contain_text("SLA 24h")
    check("category edit -> HIGH / 24h (server recomputed)", "HIGH" in page.locator("#result .meta").inner_text())
    expect(page.locator("#audit")).to_contain_text("UNAUTHORISED_TXN → DUPLICATE_DEBIT")  # audit loads async
    check("audit shows EDITED with before/after", True)

    # ---------- approve with unsaved edit: saved first ----------
    page.fill("#summary", "Edited by staff before approval")
    page.click("#approve")
    expect(page.locator("#approve")).to_have_text("Approved ✓")
    check("approved: inputs locked", page.locator("#cat").is_disabled() and page.locator("#summary").is_disabled()
          and page.locator("#reply").is_disabled() and page.locator("#approve").is_disabled())
    check("approval included the pending edit", page.input_value("#summary") == "Edited by staff before approval")
    # CREATED, DUPLICATE_SUBMIT, EDITED (category), EDITED (summary saved before approving), APPROVED
    expect(page.locator("#audit-sum")).to_have_text("Audit trail (5)")
    audit = page.locator("#audit").text_content()   # <details> is collapsed, so inner_text() is empty
    check("audit has CREATED, DUPLICATE_SUBMIT, 2x EDITED, APPROVED",
          audit.count("EDITED") == 2 and all(k in audit for k in ("CREATED", "DUPLICATE_SUBMIT", "APPROVED")))

    # ---------- injection ticket ----------
    page.select_option("#sample", "T009")
    page.click("#analyze")
    expect(page.locator("#result h2")).to_contain_text("Ticket #2")
    banner = page.locator("#result .banner").first.inner_text()
    check("T009 banner: needs human review + injection", "Needs human review" in banner and "prompt injection" in banner)
    check("T009 category not preselected", page.input_value("#cat") == "")
    check("T009 approve disabled until a category is chosen", page.locator("#approve").is_disabled())
    check("T009 (injection) gets no one-click model suggestion", page.locator("#use-sugg").count() == 0)
    page.select_option("#cat", "CHARGEBACK_STATUS")
    check("staff choosing a category enables approve but does not save",
          page.locator("#approve").is_enabled() and page.locator("#save").is_enabled())
    check("T009 still unassigned in history until saved", "Unassigned" in page.locator("#rows").inner_text())
    page.screenshot(path=str(SHOTS / "s4_desktop_review.png"), full_page=True)

    # ---------- XSS ----------
    page.fill("#text", 'Hello <img src=x onerror="window.__xss=1"> my payment of 500 failed and I was debited today please help.')
    page.select_option("#channel", "email")
    page.click("#analyze")
    expect(page.locator("#result h2")).to_contain_text("Ticket #3")
    page.wait_for_timeout(300)
    check("no script executed from ticket/model text", page.evaluate("window.__xss") is None)
    check("markup shown as literal text", "<script>" in page.input_value("#summary")
          and page.locator("#result img").count() == 0 and page.locator("#result b").count() == 0)

    # ---------- history filters ----------
    page.select_option("#f-status", "APPROVED")
    expect(page.locator("#rows tr")).to_have_count(1)
    check("status filter APPROVED -> ticket #1 only", "#1" in page.locator("#rows").inner_text())
    page.select_option("#f-cat", "UNASSIGNED")
    expect(page.locator("#rows")).to_contain_text("No tickets match")
    page.select_option("#f-status", "")
    expect(page.locator("#rows")).to_contain_text("#2")    # wait for the refresh, not the stale message row
    expect(page.locator("#rows tr")).to_have_count(1)
    check("Unassigned filter -> only the review ticket (#2)", "#2" in page.locator("#rows").inner_text())
    page.select_option("#f-cat", "DUPLICATE_DEBIT")
    page.select_option("#f-status", "APPROVED")
    expect(page.locator("#rows")).to_contain_text("#1")   # table refreshes async
    expect(page.locator("#rows tr")).to_have_count(1)
    check("category + status combined", "#1" in page.locator("#rows").inner_text())
    page.select_option("#f-cat", "")
    page.select_option("#f-status", "")
    expect(page.locator("#rows tr")).to_have_count(3)
    check("filters cleared -> all 3, newest first", page.locator("#rows tr").first.inner_text().startswith("#3"))

    # ---------- row click + refresh persistence ----------
    page.locator("#rows tr", has_text="#1").click()
    expect(page.locator("#result h2")).to_contain_text("Ticket #1")
    check("row click opens that ticket", page.input_value("#summary") == "Edited by staff before approval")
    check("selected row highlighted", page.locator("#rows tr.sel").count() == 1)
    page.reload()
    expect(page.locator("#result h2")).to_contain_text("Ticket #1")
    check("refresh keeps the open ticket (hash deep link)", page.url.endswith("#ticket=1"))

    # ---------- layout ----------
    page.set_viewport_size({"width": 390, "height": 900})
    page.wait_for_timeout(200)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check(f"no horizontal page scroll at phone width (overflow={overflow})", overflow <= 1)
    page.screenshot(path=str(SHOTS / "s4_mobile.png"), full_page=True)
    ctx.close()
    dark = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark")
    dp = dark.new_page()
    dp.goto(BASE + "#ticket=2")
    expect(dp.locator("#result h2")).to_contain_text("Ticket #2")
    dp.screenshot(path=str(SHOTS / "s4_dark.png"), full_page=True)
    browser.close()

check(f"no JS errors / console errors ({js_errors})", not js_errors)
print("screenshots:", SHOTS)
print("\nFAILURES:" if failures else "\nALL CHECKS PASSED", failures or "")
server.should_exit = True
sys.exit(1 if failures else 0)
