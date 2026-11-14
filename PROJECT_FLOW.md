# PROJECT_FLOW

## 1. Architecture Map

```
Browser (static/index.html) -> FastAPI (app/main.py) -> service.py -> triage.py + llm.py -> SQLite (app/db.py)
```

| Concern | File |
|---|---|
| HTTP API, request models, static UI mount | app/main.py |
| Pipeline, idempotent insert, edit/approve, audit | app/service.py |
| Gate, PII, regex, injection, SLA maths | app/triage.py |
| Gemini call + output validation + fallback | app/llm.py |
| UI (single file, no build) | static/index.html |

Run: `.venv\Scripts\python -m uvicorn app.main:app` then open http://127.0.0.1:8000
Checks: scripts/check_s1..s4.py (offline); s2 `--live` hits Gemini; s4 needs Edge + playwright.
Smoke: `scripts/smoke.py` (live, temp DB; `--seed` loads real DB) compares all 20 samples to expected.

## 2. Core Write/Read Flows

1. Analyze click: button locks, `POST /api/triage` {text, channel}.
2. `validate_input` rejects junk (422, no LLM, no row); UI shows the message.
3. `content_hash` hit returns existing row + DUPLICATE_SUBMIT audit; UI shows banner.
4. Miss: mask PII, extract details, flag injection, `llm.classify` on redacted text only.
5. Review reason (bad output, low confidence, injection, no key) -> category NULL, NEEDS_HUMAN_REVIEW.
6. `pick_primary` + `compute_sla` read the categories table; INSERT ... ON CONFLICT + CREATED audit.
7. UI renders card; `#ticket=ID` in URL; history and audit trail reload.
8. Save: `PATCH` changed fields only; category change recomputes SLA from original ingest time.
9. Approve: UI saves pending edits first, then `POST .../approve`; repeat is a no-op.
10. History: `GET /api/tickets?category=&status=`; row click opens ticket via `GET /api/tickets/{id}`.

## 3. Key Trade-offs & Guards

- Priority/SLA written only by `compute_sla`; PATCH rejects priority/sla/status keys (422).
- Closed list: invalid, unknown, low-confidence or errored model output never assigns a category.
- Injection: text is data in tags; flag forces review; UI offers no one-click suggestion.
- PII: raw text kept for audit only; API, LLM, UI show masked values.
- Idempotency: UNIQUE hash + conditional writes; button lock; concurrent submits give one row.
- UI builds DOM with textContent; ticket and model text never reach innerHTML.
- Approved tickets immutable (409); no auth, actor from Staff ID box via `X-Actor`.
- Identical simultaneous submits may each call the LLM once before the insert resolves.
- Model outage/quota (429, 5xx) stores the ticket as NEEDS_HUMAN_REVIEW; re-submit returns it, no re-analysis (v2).
- Gemini free tier: 20 requests/day per model; use a model with quota via `GEMINI_MODEL`.

## 4. Slice Changelog

- S5: `scripts/smoke.py` runs all 20 samples live: 18 match plan, T013/T014 rejected, T009 to review; re-submit made 0 rows. 429s now labelled `LLM_RATE_LIMITED`. Limitation: first run hit daily quota (7 tickets stuck in review); no auto re-analysis.
- S4: Single-page UI: samples, analyze, editable card, approve, filtered history, audit; 37 browser checks pass. Fixed SQLite cross-thread 500 found by UI test. Limitation: no pagination, polling or login.
- S3: API, idempotent insert, audit trail, edit/approve, filters; 68 offline checks pass.
- S2: `llm.py` Gemini call, strict validation, keyword fallback; live run matches expectations. No retry.
- S1: Input gate, hash, PII mask, extraction, injection flag, severity, SLA, schema. Regex PII/amounts heuristic.
- S0.1: Switched LLM provider Claude to Gemini; key in git-ignored `.env`.
- S0: Scaffolded venv, deps, PLAN.md, `/health` endpoint.
