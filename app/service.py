"""Pipeline + ticket persistence rules. HTTP-free so it can be tested directly.

Guarantees:
- junk input is rejected before hashing, the LLM and the DB
- one row per distinct text (UNIQUE content_hash); resubmits return the existing row
- priority / sla_hours / sla_deadline are written only from compute_sla(); callers cannot set them
- raw_text is stored for audit and never returned by any read function here
- every create / duplicate / edit / approve appends an audit_events row
"""
import json
from datetime import datetime

from . import db, llm, triage

PENDING = "PENDING_REVIEW"
NEEDS_REVIEW = "NEEDS_HUMAN_REVIEW"
APPROVED = "APPROVED"
STATUSES = (PENDING, NEEDS_REVIEW, APPROVED)
UNASSIGNED = "UNASSIGNED"  # filter value for tickets with no category

GENERIC_REPLY = (
    "Dear Customer, thank you for contacting us. We have received your message and our team "
    "is reviewing it. We will update you once we have more information. "
    "Please do not share your OTP, PIN or password with anyone."
)


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


# ---------- helpers ----------

def _now() -> str:
    return triage.utcnow().isoformat()


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _audit(conn, ticket_id, event_type, actor, before=None, after=None):
    conn.execute(
        "INSERT INTO audit_events(ticket_id, event_type, actor, before_json, after_json, ts) "
        "VALUES (?,?,?,?,?,?)",
        (ticket_id, event_type, actor,
         None if before is None else _j(before), None if after is None else _j(after), _now()),
    )


def _row_by_hash(conn, h):
    return conn.execute("SELECT * FROM tickets WHERE content_hash=?", (h,)).fetchone()


def _row_by_id(conn, ticket_id):
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None:
        raise ServiceError("NOT_FOUND", f"Ticket {ticket_id} not found.", 404)
    return row


def ticket_to_dict(row, detail: bool = False) -> dict:
    """Public shape. Deliberately omits raw_text and content_hash."""
    d = {
        "id": row["id"],
        "external_id": row["external_id"],
        "channel": row["channel"],
        "redacted_text": row["redacted_text"],
        "ingested_at": row["ingested_at"],
        "updated_at": row["updated_at"],
        "category": row["category"],
        "priority": row["priority"],
        "sla_hours": row["sla_hours"],
        "sla_deadline": row["sla_deadline"],
        "confidence": row["confidence"],
        "summary": row["summary"],
        "draft_reply": row["draft_reply"],
        "status": row["status"],
        "flags": json.loads(row["flags_json"]),
        "extracted": json.loads(row["extracted_json"]),
    }
    if detail and row["model_output_json"]:
        d["model_output"] = json.loads(row["model_output_json"])
    return d


# ---------- triage (create or return existing) ----------

def _register_duplicate(conn, row, channel, external_id, actor) -> dict:
    _audit(conn, row["id"], "DUPLICATE_SUBMIT", actor,
           after={"channel": channel, "external_id": external_id})
    conn.commit()
    return ticket_to_dict(row, detail=True)


def triage_ticket(conn, text, channel="other", external_id=None, actor="staff", call=None):
    """Return (ticket_dict, duplicate: bool). Raises triage.InputRejected for junk."""
    clean = triage.validate_input(text)                      # 1. gate: before anything costly
    h = triage.content_hash(clean)                           # 2. idempotency key
    existing = _row_by_hash(conn, h)
    if existing is not None:
        return _register_duplicate(conn, existing, channel, external_id, actor), True

    cats = db.get_categories(conn)
    redacted, pii = triage.mask_pii(clean)                   # 3. LLM and API only see this
    details = triage.extract_details(clean)
    flags = triage.flag_injection(clean)
    result = llm.classify(redacted, cats, call=call)         # 4. never raises

    reasons = list(result["review_reasons"]) + list(flags)
    primary = None if reasons else triage.pick_primary(result["categories"], cats)
    if primary is None and not reasons:                      # defensive: no valid category at all
        reasons.append("NO_VALID_CATEGORY")
    needs_review = bool(reasons)
    category = None if needs_review else primary

    suggested = result["suggested_categories"] or result["categories"]
    issues = result["issues"] or [{"category": c, "detail": ""} for c in result["categories"]]
    extracted = {
        **details,
        "pii": pii,                                          # masked values only
        "issues": issues,
        "all_categories": result["categories"] or suggested,
        "suggested_categories": suggested,
        "suggested_primary": triage.pick_primary(suggested, cats),
        "review_reasons": reasons,
    }
    summary = result["summary"] or f"Needs human review: {redacted[:140].strip()}"
    reply = result["draft_reply"] or GENERIC_REPLY

    ingested = triage.utcnow()                               # 5. SLA clock starts at ingestion
    sla = triage.compute_sla(category, ingested, cats)
    status = NEEDS_REVIEW if needs_review else PENDING
    now = ingested.isoformat()

    cur = conn.execute(
        """INSERT INTO tickets(external_id, channel, raw_text, redacted_text, content_hash, ingested_at,
               category, priority, sla_hours, sla_deadline, confidence, extracted_json, summary,
               draft_reply, flags_json, model_output_json, status, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(content_hash) DO NOTHING""",
        (external_id, channel, clean, redacted, h, now, category, sla["priority"], sla["sla_hours"],
         sla["sla_deadline"], result["confidence"], _j(extracted), summary, reply, _j(flags),
         _j(result), status, now),
    )
    if cur.rowcount == 0:                                    # lost a race with an identical submit
        conn.rollback()
        return _register_duplicate(conn, _row_by_hash(conn, h), channel, external_id, actor), True

    tid = cur.lastrowid
    _audit(conn, tid, "CREATED", actor, after={
        "category": category, "priority": sla["priority"], "sla_deadline": sla["sla_deadline"],
        "status": status, "flags": flags, "review_reasons": reasons, "source": result["source"],
    })
    conn.commit()
    return ticket_to_dict(_row_by_id(conn, tid), detail=True), False


# ---------- reads ----------

def list_tickets(conn, category=None, status=None, limit=100, offset=0):
    cats = db.get_categories(conn)
    where, args = [], []
    if category:
        if category == UNASSIGNED:
            where.append("category IS NULL")
        elif category in cats:
            where.append("category = ?")
            args.append(category)
        else:
            raise ServiceError("BAD_FILTER", f"Unknown category {category!r}.", 422)
    if status:
        if status not in STATUSES:
            raise ServiceError("BAD_FILTER", f"Unknown status {status!r}.", 422)
        where.append("status = ?")
        args.append(status)
    sql = "SELECT * FROM tickets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ingested_at DESC, id DESC LIMIT ? OFFSET ?"
    limit = max(1, min(int(limit), 500))
    rows = conn.execute(sql, (*args, limit, max(0, int(offset)))).fetchall()
    return [ticket_to_dict(r) for r in rows]


def get_ticket(conn, ticket_id):
    return ticket_to_dict(_row_by_id(conn, ticket_id), detail=True)


def get_audit(conn, ticket_id):
    _row_by_id(conn, ticket_id)
    rows = conn.execute("SELECT * FROM audit_events WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()
    return [
        {"id": r["id"], "event_type": r["event_type"], "actor": r["actor"], "ts": r["ts"],
         "before": json.loads(r["before_json"]) if r["before_json"] else None,
         "after": json.loads(r["after_json"]) if r["after_json"] else None}
        for r in rows
    ]


# ---------- human decisions ----------

def edit_ticket(conn, ticket_id, changes: dict, actor="staff"):
    """changes may hold category / summary / draft_reply. Priority + SLA are recomputed here."""
    row = _row_by_id(conn, ticket_id)
    if row["status"] == APPROVED:
        raise ServiceError("ALREADY_APPROVED", "Approved tickets cannot be edited.", 409)
    cats = db.get_categories(conn)

    new = {}
    if changes.get("category") is not None:
        if changes["category"] not in cats:
            raise ServiceError("UNKNOWN_CATEGORY", "Category must be one of the fixed list.", 422)
        new["category"] = changes["category"]
    for field, cap in (("summary", llm.MAX_SUMMARY), ("draft_reply", llm.MAX_REPLY)):
        if changes.get(field) is not None:
            val = changes[field].strip()
            if not val:
                raise ServiceError("EMPTY_FIELD", f"{field} cannot be blank.", 422)
            new[field] = val[:cap]

    new = {k: v for k, v in new.items() if v != row[k]}      # drop no-ops
    if not new:
        return ticket_to_dict(row, detail=True)

    sets = dict(new)
    if "category" in new:
        sla = triage.compute_sla(new["category"], datetime.fromisoformat(row["ingested_at"]), cats)
        sets.update(priority=sla["priority"], sla_hours=sla["sla_hours"], sla_deadline=sla["sla_deadline"])
        if row["status"] == NEEDS_REVIEW:
            sets["status"] = PENDING                          # a human has now assigned a category
    sets["updated_at"] = _now()

    cols = ", ".join(f"{k}=?" for k in sets)
    cur = conn.execute(f"UPDATE tickets SET {cols} WHERE id=? AND status != ?",
                       (*sets.values(), ticket_id, APPROVED))
    if cur.rowcount == 0:                                     # approved by someone else meanwhile
        conn.rollback()
        raise ServiceError("ALREADY_APPROVED", "Approved tickets cannot be edited.", 409)
    _audit(conn, ticket_id, "EDITED", actor,
           before={k: row[k] for k in sets if k != "updated_at"},
           after={k: v for k, v in sets.items() if k != "updated_at"})
    conn.commit()
    return ticket_to_dict(_row_by_id(conn, ticket_id), detail=True)


def approve_ticket(conn, ticket_id, actor="staff"):
    """Returns (ticket_dict, newly_approved: bool). Approving twice is a harmless no-op."""
    row = _row_by_id(conn, ticket_id)
    if row["status"] == APPROVED:
        return ticket_to_dict(row, detail=True), False
    if row["category"] is None:
        raise ServiceError("CATEGORY_REQUIRED", "Assign a category before approving.", 409)
    cur = conn.execute("UPDATE tickets SET status=?, updated_at=? WHERE id=? AND status != ?",
                       (APPROVED, _now(), ticket_id, APPROVED))
    if cur.rowcount == 0:                                     # double-click race: someone else won
        conn.rollback()
        return ticket_to_dict(_row_by_id(conn, ticket_id), detail=True), False
    _audit(conn, ticket_id, "APPROVED", actor,
           before={"status": row["status"]},
           after={"status": APPROVED, "category": row["category"], "priority": row["priority"],
                  "sla_deadline": row["sla_deadline"], "summary": row["summary"],
                  "draft_reply": row["draft_reply"]})
    conn.commit()
    return ticket_to_dict(_row_by_id(conn, ticket_id), detail=True), True
