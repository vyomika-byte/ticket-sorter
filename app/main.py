import csv
import re
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, Query, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import db, service, triage

SAMPLES_CSV = db.ROOT / "tickets_sample.csv"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db().close()          # create schema + load categories.csv once at startup
    yield


app = FastAPI(title="Dispute Triage", lifespan=lifespan)
app.state.llm_call = None         # tests inject a fake model here; None = real Gemini / fallback


def get_db():
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


def get_actor(x_actor: str = Header(default="staff", max_length=64)) -> str:
    # No auth in the prototype: the actor is whatever the client says (v2: real staff identity).
    return re.sub(r"[^\w .@-]", "", x_actor).strip() or "staff"


@app.exception_handler(triage.InputRejected)
async def _rejected(_, exc: triage.InputRejected):
    return JSONResponse(status_code=422, content={"error": exc.code, "message": exc.message})


@app.exception_handler(service.ServiceError)
async def _service_error(_, exc: service.ServiceError):
    return JSONResponse(status_code=exc.http_status, content={"error": exc.code, "message": exc.message})


class TriageIn(BaseModel):
    text: Optional[str] = None    # None/blank reaches the gate and gets a clean 422
    channel: Literal["email", "app", "call", "other"] = "other"
    external_id: Optional[str] = Field(default=None, max_length=64)


class EditIn(BaseModel):
    """Priority / SLA / status are not accepted: extra keys are a 422."""
    model_config = ConfigDict(extra="forbid")
    category: Optional[str] = None
    summary: Optional[str] = None
    draft_reply: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/categories")
def categories(conn=Depends(get_db)):
    return [{"category": c, **v} for c, v in db.get_categories(conn).items()]


@app.get("/api/samples")
def samples():
    with open(SAMPLES_CSV, newline="", encoding="utf-8") as f:
        return [
            {"ticket_id": r["ticket_id"], "channel": r["channel"], "text": r["text"] or ""}
            for r in csv.DictReader(f)
        ]


@app.post("/api/triage")
def triage_endpoint(body: TriageIn, response: Response, conn=Depends(get_db), actor=Depends(get_actor)):
    ticket, duplicate = service.triage_ticket(
        conn, body.text, body.channel, body.external_id, actor, call=app.state.llm_call
    )
    response.status_code = 200 if duplicate else 201
    return {"ticket": ticket, "duplicate": duplicate}


@app.get("/api/tickets")
def tickets(
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn=Depends(get_db),
):
    return service.list_tickets(conn, category, status, limit, offset)


@app.get("/api/tickets/{ticket_id}")
def ticket(ticket_id: int, conn=Depends(get_db)):
    return service.get_ticket(conn, ticket_id)


@app.get("/api/tickets/{ticket_id}/audit")
def audit(ticket_id: int, conn=Depends(get_db)):
    return service.get_audit(conn, ticket_id)


@app.patch("/api/tickets/{ticket_id}")
def edit(ticket_id: int, body: EditIn, conn=Depends(get_db), actor=Depends(get_actor)):
    return service.edit_ticket(conn, ticket_id, body.model_dump(exclude_unset=True), actor)


@app.post("/api/tickets/{ticket_id}/approve")
def approve(ticket_id: int, conn=Depends(get_db), actor=Depends(get_actor)):
    t, newly = service.approve_ticket(conn, ticket_id, actor)
    return {"ticket": t, "already_approved": not newly}


# Mounted last so /api/* and /health keep precedence over the static catch-all.
app.mount("/", StaticFiles(directory=db.ROOT / "static", html=True), name="static")
