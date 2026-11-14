"""Deterministic triage core: input gate, hashing, PII masking, regex extraction,
injection flagging, severity ranking and SLA maths. No LLM, no I/O.

Priority and SLA come ONLY from the categories table via compute_sla()."""
import hashlib
import re
from datetime import datetime, timedelta, timezone

MIN_CHARS = 15
MIN_WORDS = 3
MIN_LETTERS = 5
MAX_CHARS = 10_000

PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

# Used when no valid category exists (needs human review). App constant, never LLM-chosen.
REVIEW_PRIORITY = "HIGH"
REVIEW_SLA_HOURS = 24


class InputRejected(ValueError):
    """Raised before any LLM call or DB write."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------- 1. input gate ----------

def validate_input(text) -> str:
    """Return stripped text or raise InputRejected."""
    if text is None or not str(text).strip():
        raise InputRejected("EMPTY_INPUT", "Ticket text is empty.")
    t = str(text).strip()
    if len(t) > MAX_CHARS:
        raise InputRejected("INPUT_TOO_LONG", f"Ticket text exceeds {MAX_CHARS} characters.")
    letters = sum(c.isalpha() for c in t)
    if len(t) < MIN_CHARS or len(t.split()) < MIN_WORDS or letters < MIN_LETTERS:
        raise InputRejected(
            "INPUT_TOO_SHORT",
            "Ticket is too short to triage. Paste the full message.",
        )
    return t


# ---------- 2. idempotency key ----------

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    """sha256 of whitespace-normalised text (case and punctuation are preserved)."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


# ---------- 3. PII masking ----------

# A long digit run right after one of these words is a reference, not an account/phone.
_REF_CONTEXT = re.compile(
    r"(?:ref(?:erence)?|utr|txn|transaction|rrn|cheque|check|chq|id|no|number|#)\W{0,4}$",
    re.I,
)
_MOBILE_CONTEXT = re.compile(r"(?:mobile|phone|contact|call|whatsapp|mob|cell)\W{0,6}$", re.I)
# Not part of a larger number: no digit (or digit+comma/dot, as in 1,250 / 12.50) on either side.
_LONG_DIGITS = re.compile(r"(?<!\d)(?<!\d,)(?<!\d\.)(?:\+?91[\s-]?)?\d{9,18}(?!\d|,\d|\.\d)")


def _mask(digits: str) -> str:
    return "X" * max(len(digits) - 4, 4) + digits[-4:]


def _classify_digits(raw_match: str, before: str):
    """Return ('mobile'|'account'|None, digits)."""
    digits = re.sub(r"\D", "", raw_match)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    ctx = before[-25:]
    if _MOBILE_CONTEXT.search(ctx):
        return ("mobile" if len(digits) == 10 else "account"), digits
    if _REF_CONTEXT.search(ctx):
        # "account no 5010..." must stay an account: check for the word account just before
        if not re.search(r"(?:account|a/c|acct)\W{0,6}(?:no|number|#)?\W{0,4}$", ctx, re.I):
            return None, digits
    if len(digits) == 10 and digits[0] in "6789":
        return "mobile", digits
    if 9 <= len(digits) <= 18:
        return "account", digits
    return None, digits


def mask_pii(text: str):
    """Return (redacted_text, pii) where pii = {'mobiles': [masked], 'accounts': [masked]}.
    Raw numbers are never returned."""
    pii = {"mobiles": [], "accounts": []}

    def repl(m):
        kind, digits = _classify_digits(m.group(0), text[: m.start()])
        if kind is None:
            return m.group(0)
        masked = _mask(digits)
        pii["mobiles" if kind == "mobile" else "accounts"].append(masked)
        return masked

    return _LONG_DIGITS.sub(repl, text), pii


# ---------- 4. regex extraction (run on raw text, output contains no raw PII) ----------

_CHEQUE = re.compile(
    r"(?:cheque|check|chq)\s*(?:no\.?|number|num|#)?\s*:?\s*#?(\d{4,9})\b", re.I
)
_REF = re.compile(
    r"\b(?:ref(?:erence)?|utr|txn|rrn|transaction)\s*(?:no\.?|id|number|#)?\s*[:#-]?\s*"
    r"([A-Za-z0-9-]*\d[A-Za-z0-9-]{3,})",
    re.I,
)
_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATES = [
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\b(?:,?\s+\d{{4}})?", re.I),
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?", re.I),
]
_RELATIVE = re.compile(
    r"\b(?:today|yesterday|last week|last month|day before yesterday|"
    r"aaj|kal|\d+\s+days?(?:\s+ago)?)\b",
    re.I,
)
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d{1,2})?(?![\w])")
_CURRENCY_BEFORE = re.compile(r"(?:rs\.?|inr|₹|rupees?)\s*$", re.I)
_CURRENCY_AFTER = re.compile(r"^\s*(?:rs\.?|rupees?|rupaye|rupaiye|inr|₹)\b", re.I)
_AMOUNT_VERB = re.compile(
    r"(?:paid|pay|debit(?:ed)?|credit(?:ed)?|charged?|sent|send|transfer(?:red)?|"
    r"deducted|refund(?:ed)?|withdrawn|of|for|amount)\W{0,3}(?:of\W{0,3})?$",
    re.I,
)


def _spans_overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def extract_details(raw: str) -> dict:
    """Regex-extract amounts, dates, cheque numbers and references.
    A hint for the UI/LLM merge - never used for priority or SLA."""
    cheques = [m.group(1) for m in _CHEQUE.finditer(raw)]
    refs = []
    for m in _REF.finditer(raw):
        val = m.group(1).strip("-")
        if val not in refs:
            refs.append(val)

    date_spans, dates = [], []
    for rx in _DATES:
        for m in rx.finditer(raw):
            if not any(_spans_overlap(m.span(), s) for s in date_spans):
                date_spans.append(m.span())
                dates.append(m.group(0).strip())
    relative = [m.group(0).lower() for m in _RELATIVE.finditer(raw)]

    # spans to exclude from amount detection
    skip = list(date_spans)
    for rx in (_CHEQUE, _REF):
        for m in rx.finditer(raw):
            skip.append(m.span(1))
    for m in _LONG_DIGITS.finditer(raw):
        skip.append(m.span())
    for m in re.finditer(r"\d{1,2}:\d{2}", raw):  # clock times like 10:14
        skip.append(m.span())

    amounts = []
    for m in _NUM.finditer(raw):
        if any(_spans_overlap(m.span(), s) for s in skip):
            continue
        txt = m.group(0).rstrip(",.")
        try:
            value = float(txt.replace(",", ""))
        except ValueError:
            continue
        if value <= 0:
            continue
        marked = (
            _CURRENCY_BEFORE.search(raw[max(0, m.start() - 8): m.start()])
            or _CURRENCY_AFTER.search(raw[m.end(): m.end() + 10])
            or _AMOUNT_VERB.search(raw[max(0, m.start() - 20): m.start()])
        )
        if marked and value not in [a["value"] for a in amounts]:
            amounts.append({"value": value, "text": txt})

    return {
        "amounts": amounts,
        "dates": dates,
        "relative_dates": list(dict.fromkeys(relative)),
        "cheque_numbers": cheques,
        "references": refs,
    }


# ---------- 5. prompt-injection flagging ----------

_INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)",
    r"disregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)",
    r"forget\s+(?:all\s+|everything|your)\s*(?:previous|prior|above|instructions?)?",
    r"(?:you\s+are|act\s+as|pretend\s+to\s+be)\s+(?:now\s+)?(?:a|an|the)?\s*\w*\s*(?:admin|system|developer|dan)\b",
    r"\bsystem\s*prompt\b",
    r"mark\s+(?:this\s+)?(?:as\s+)?(?:resolved|approved|closed|low|critical)",
    r"classify\s+(?:this\s+)?as\s+\w+",
    r"approve\s+(?:a\s+|the\s+)?(?:full\s+)?refund",
    r"\bsla\b.{0,20}\b(?:0|zero)\b",
    r"(?:set|change)\s+(?:the\s+)?(?:priority|sla|status|category)\b",
]
_INJECTION_RX = [re.compile(p, re.I) for p in _INJECTION_PATTERNS]


def flag_injection(text: str) -> list:
    """Return a list of flags; empty if clean. Flags force human review."""
    if any(rx.search(text) for rx in _INJECTION_RX):
        return ["POSSIBLE_PROMPT_INJECTION"]
    return []


# ---------- 6. severity + SLA (the only place priority/SLA are decided) ----------

def pick_primary(candidates, categories: dict):
    """Most severe valid category: highest priority, then lowest sla_hours.
    Unknown names are ignored. Returns None if nothing valid."""
    valid = [c for c in dict.fromkeys(candidates or []) if c in categories]
    if not valid:
        return None
    return min(
        valid,
        key=lambda c: (
            PRIORITY_RANK[categories[c]["default_priority"]],
            categories[c]["sla_hours"],
        ),
    )


def compute_sla(category, ingested_at: datetime, categories: dict) -> dict:
    """Priority + SLA from the table. category=None -> review fallback constants."""
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=timezone.utc)
    if category is None:
        priority, hours = REVIEW_PRIORITY, REVIEW_SLA_HOURS
    else:
        row = categories[category]  # KeyError on unknown category is deliberate
        priority, hours = row["default_priority"], row["sla_hours"]
    deadline = ingested_at + timedelta(hours=hours)
    return {
        "priority": priority,
        "sla_hours": hours,
        "sla_deadline": deadline.astimezone(timezone.utc).isoformat(),
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
