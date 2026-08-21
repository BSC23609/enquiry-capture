"""Turn an email into a structured prospect record.

Two stages, deliberately:

  1. A cheap local pre-filter that throws out the obvious junk. Newsletters,
     OTPs, statements, internal mail, delivery receipts. This runs for free and
     kills roughly 70% of a real inbox before we spend anything.

  2. A Claude call on whatever survives. This is the part Power Automate
     couldn't do without a Premium licence, and it's the whole reason the
     accuracy is different here.

Regex still handles GSTIN, mobile and email afterwards, as a cross-check
against what the model returned. Where they disagree, regex wins for those
three fields — they have strict formats and the model has no business
overriding a structural match.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

import httpx

from .config import settings
from .known_customers import KNOWN_DOMAINS, KNOWN_EMAILS, KNOWN_MOBILES

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ patterns
RE_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
# Indian mobiles are written every which way: "9840412345", "98404 12345",
# "98404-12345", "+91 98404 12345", "0091-9840412345". Allow internal
# separators, then normalise down to 10 digits afterwards.
_MOBILE_CORE = r"[6-9]\d{4}[\s.\-]?\d{5}"
RE_MOBILE_LABELLED = re.compile(
    r"(?:mob(?:ile)?|cell|phone|ph|contact|call|whatsapp|tel)\.?\s*[:\-]?\s*"
    r"((?:(?:\+|00)?91[\s.\-]?)?" + _MOBILE_CORE + r")", re.I,
)
RE_MOBILE_ANY = re.compile(
    r"(?<![\d])(?:(?:\+|00)?91[\s.\-]?)?(" + _MOBILE_CORE + r")(?![\d])"
)
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

QUOTE_MARKERS = [
    "-----Original Message-----",
    "________________________________",
    "\nFrom:",
    "\r\nFrom:",
    "Sent from my iPhone",
    "Get Outlook for",
]
RE_ON_WROTE = re.compile(r"\n\s*On .{5,120}wrote:", re.S)

JUNK_MARKERS = [
    "unsubscribe", "view this email in your browser", "manage preferences",
    "this is an automated", "do not reply to this",
    "one time password", "verification code", "your otp",
    "statement of account", "e-statement", "salary slip", "payslip",
    "delivery status notification", "out of office", "automatic reply",
    "undeliverable", "webinar", "job application", "curriculum vitae",
    "seeking a position", "resume attached",
]

KW_PRODUCT = [
    "tmt", "hr coil", "cr coil", "ms plate", "angle", "channel", "steel",
    "coil", "plate", "rebar", "ismb", "ismc", "billet",
    "roofing", "roof", "colour coated", "color coated", "profile sheet",
    "trapezoidal", "corrugated", "sandwich panel", "polycarbonate",
    "peb", "pre-engineered", "pre engineered", "shed", "warehouse", "godown",
    "purlin", "girt", "fabrication", "structural steel", "mezzanine", "crane",
]
KW_INTENT = [
    "quote", "quotation", "enquiry", "inquiry", "requirement", "price",
    "pricing", "rate", "rates", "budget", "tender", "rfq", "supply",
    "proposal", "interested", "looking for", "need", "project", "kindly send",
]


# ------------------------------------------------------------------ cleaning
def html_to_text(raw: str) -> str:
    if "<" not in raw:
        return raw
    s = re.sub(r"<(style|script)[\s\S]*?</\1>", " ", raw, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|tr|td|li|h\d)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html_lib.unescape(s)


def strip_quoted(text: str) -> str:
    """Cut the reply chain so we read the *current* sender's signature."""
    cut = len(text)
    for marker in QUOTE_MARKERS:
        i = text.find(marker)
        if 40 < i < cut:
            cut = i
    m = RE_ON_WROTE.search(text)
    if m and 40 < m.start() < cut:
        cut = m.start()
    return text[:cut]


def tidy(text: str) -> str:
    s = text.replace("\r", "")
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def clean_body(raw: str, content_type: str) -> str:
    if content_type == "html":
        raw = html_to_text(raw)
    return tidy(strip_quoted(raw))


# ------------------------------------------------------------------ normalise
def norm_gstin(v: str | None) -> str | None:
    if not v:
        return None
    g = re.sub(r"\s", "", v).upper()
    return g if RE_GSTIN.fullmatch(g) else None


def norm_mobile(v: str | None) -> str | None:
    if not v:
        return None
    d = re.sub(r"\D", "", v)
    if len(d) > 10:
        d = d[-10:]
    return d if len(d) == 10 and d[0] in "6789" else None


def norm_email(v: str | None) -> str | None:
    if not v:
        return None
    e = v.strip().lower().rstrip(".,;:")
    return e if RE_EMAIL.fullmatch(e) else None


def domain_of(addr: str | None) -> str:
    if not addr or "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[1].lower()


# ------------------------------------------------------------------ prefilter
class Verdict:
    ENQUIRY = "enquiry"
    JUNK = "junk"
    NO_CONTACT = "no_contact"
    ERROR = "error"


def is_known_customer(from_addr: str, body: str) -> str:
    """Return a non-empty tag if the sender matches the customer master.

    Match order: exact email, then any known mobile appearing in the body,
    then corporate domain. Free-provider domains (gmail/yahoo) are NEVER
    domain-matched — only their exact addresses count — because most
    customers use them and a domain match there would admit the whole world.
    """
    addr = (from_addr or "").strip().lower()
    if addr in KNOWN_EMAILS:
        return "known email"

    dom = domain_of(addr)
    if dom and dom in KNOWN_DOMAINS:
        return "known domain"

    # A known customer mobile in the signature is a strong match even when
    # they've written from a new address.
    for m in RE_MOBILE_ANY.finditer(body):
        n = norm_mobile(m.group(1))
        if n and n in KNOWN_MOBILES:
            return "known mobile"

    return ""


def prefilter(subject: str, body: str, from_addr: str) -> tuple[bool, str]:
    """Cheap local gate. Returns (worth_sending_to_llm, reason_if_not)."""
    if domain_of(from_addr) in settings.internal_domains:
        return False, "internal sender"

    # Known customers bypass the keyword gate entirely. This is what rescues
    # the "send pricing for the usual" RFQs that carry intent but no product
    # word — the case the estimator flagged as ~443 dropped mails.
    tag = is_known_customer(from_addr, body)
    if tag:
        return True, f"allowlisted ({tag})"

    hay = f"{subject}\n{body}".lower()

    for marker in JUNK_MARKERS:
        if marker in hay:
            return False, f"junk marker: {marker}"

    product = sum(1 for k in KW_PRODUCT if k in hay)
    intent = sum(1 for k in KW_INTENT if k in hay)

    # Either signal alone is too loose. "steel" appears in half of B2B spam;
    # "quote" appears in every newsletter footer.
    if product == 0 or intent == 0:
        return False, f"no product+intent signal (p={product}, i={intent})"

    return True, ""


# ------------------------------------------------------------------ regex pass
def regex_fields(body: str, from_addr: str) -> dict[str, str | None]:
    gstin = None
    m = RE_GSTIN.search(body.upper())
    if m:
        gstin = m.group(0)

    mobile = None
    m = RE_MOBILE_LABELLED.search(body)
    if m:
        mobile = norm_mobile(m.group(1))
    if not mobile:
        # Take the first candidate that isn't part of a GSTIN or a long ID.
        for m in RE_MOBILE_ANY.finditer(body):
            if gstin and m.group(0) in gstin:
                continue
            mobile = norm_mobile(m.group(1))
            if mobile:
                break

    email = None
    for candidate in RE_EMAIL.findall(body):
        e = norm_email(candidate)
        if not e:
            continue
        if domain_of(e) in settings.internal_domains:
            continue
        if "noreply" in e or "no-reply" in e:
            continue
        email = e
        break
    if not email:
        email = norm_email(from_addr)

    return {"gstin": gstin, "mobile": mobile, "email": email}


# ------------------------------------------------------------------ the model
SYSTEM = """You read inbound B2B email for an Indian steel, roofing and pre-engineered-building group and extract the sender's contact details.

Return ONLY a JSON object. No preamble, no explanation, no markdown fences.

Schema:
{
  "is_enquiry": true|false,
  "sender_type": "customer"|"supplier"|"service_provider"|"marketing"|"other",
  "enquiry_type": "steel"|"roofing"|"peb"|"other"|"",
  "company_name": "",
  "contact_person": "",
  "email": "",
  "mobile": "",
  "gstin": "",
  "city": "",
  "confidence": "high"|"medium"|"low"
}

Rules:
- is_enquiry is true ONLY when a human is asking about steel, sheets, coils, roofing, PEB, sheds, structures, fabrication, pricing or a project. It is false for newsletters, marketing, invoices, statements, job applications, automated notifications and vendor cold-outreach.
- sender_type classifies who the sender is:
    "customer"         — a buyer asking us to supply, quote, or price steel/roofing/PEB/fabrication; someone placing or chasing an order.
    "supplier"         — offering to SELL us material, machinery, or raw stock; a vendor's own quotation or catalogue.
    "service_provider" — banks, logistics/transport, software, consultants, auditors, utilities, telecom.
    "marketing"        — newsletters, promotional blasts, event invites, cold sales outreach with no specific buying intent toward us.
    "other"            — anything that fits none of the above, including internal-looking mail and personal messages.
  A mail can be sender_type "customer" even when is_enquiry is false (e.g. an existing customer sending a payment confirmation). Judge the SENDER, not just this one message.
- Extract ONLY what is literally present. Never guess, never infer, never complete a partial value. An empty string is always better than a plausible invention.
- Read the most recent signature block only. Ignore quoted replies, legal disclaimers and footers.
- company_name: the sender's own company, not ours, and not a company they merely mention. Include the suffix (Pvt Ltd, LLP, Industries) if written.
- contact_person: a person's name only. Not a designation, not a company.
- gstin is exactly 15 characters. mobile is 10 digits, no country code.
- confidence is "low" whenever you had to interpret rather than read.
"""


def call_claude(subject: str, from_addr: str, from_name: str, body: str) -> dict[str, Any]:
    payload = {
        "model": settings.model,
        "max_tokens": 600,
        "system": SYSTEM,
        "messages": [{
            "role": "user",
            "content": (
                f"SUBJECT: {subject}\n"
                f"FROM: {from_name} <{from_addr}>\n\n"
                f"BODY:\n{body[:12000]}"
            ),
        }],
    }

    with httpx.Client(timeout=90.0) as client:
        for attempt in range(3):
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 529) or resp.status_code >= 500:
                import time as _t
                _t.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Anthropic API [{resp.status_code}]: {resp.text[:300]}")
        else:
            raise RuntimeError("Anthropic API unavailable after retries")

    text = "".join(
        block.get("text", "")
        for block in resp.json().get("content", [])
        if block.get("type") == "text"
    ).strip()

    # Defensive: strip fences if the model ever adds them.
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise RuntimeError(f"Model did not return JSON: {text[:200]}")
        return json.loads(m.group(0))


# ------------------------------------------------------------------ orchestrate
def extract(subject: str, from_addr: str, from_name: str, body: str) -> dict[str, Any]:
    """Returns verdict, reason, llm_used, sender_type and the extracted fields.

    Fields and sender_type are populated whenever the model ran, even when the
    mail is not an enquiry — the contact book enriches on any mail from a known
    customer, not only on enquiries.
    """
    ok, reason = prefilter(subject, body, from_addr)
    if not ok:
        return {"verdict": Verdict.JUNK, "reason": reason, "llm_used": False,
                "sender_type": None, "fields": {}}

    try:
        llm = call_claude(subject, from_addr, from_name, body)
    except Exception as exc:              # noqa: BLE001 — we want to record any failure
        log.exception("Extraction failed")
        return {"verdict": Verdict.ERROR, "reason": str(exc)[:400], "llm_used": True,
                "sender_type": None, "fields": {}}

    # Parse fields up front — needed for the contact book regardless of verdict.
    rx = regex_fields(body, from_addr)
    gstin = rx["gstin"] or norm_gstin(llm.get("gstin"))
    mobile = rx["mobile"] or norm_mobile(llm.get("mobile"))
    email = norm_email(llm.get("email")) or rx["email"]
    company = (llm.get("company_name") or "").strip() or None
    person = (llm.get("contact_person") or "").strip() or None
    city = (llm.get("city") or "").strip() or None
    etype = (llm.get("enquiry_type") or "other").strip().lower() or "other"

    sender_type = (llm.get("sender_type") or "other").strip().lower()
    if sender_type not in ("customer", "supplier", "service_provider", "marketing", "other"):
        sender_type = "other"

    score = (2 if gstin else 0) + (1 if mobile else 0) + \
            (1 if company else 0) + (1 if person else 0)
    confidence = "high" if score >= 4 else ("medium" if score >= 2 else "low")

    fields = {
        "gstin": gstin,
        "mobile": mobile,
        "email": email,
        "company_name": company,
        "contact_person": person,
        "city": city,
        "enquiry_type": etype if etype in ("steel", "roofing", "peb", "other") else "other",
        "state_code": gstin[:2] if gstin else None,
        "confidence": confidence,
        "sender_type": sender_type,
    }

    # Verdict governs the PROSPECTS table only. The contact book acts on
    # sender_type + fields independently, in the sync layer.
    if not llm.get("is_enquiry"):
        return {"verdict": Verdict.JUNK, "reason": "model: not an enquiry",
                "llm_used": True, "sender_type": sender_type, "fields": fields}

    if not any([gstin, mobile, email]):
        return {"verdict": Verdict.NO_CONTACT,
                "reason": "enquiry, but no gstin/mobile/email found",
                "llm_used": True, "sender_type": sender_type, "fields": fields}

    return {"verdict": Verdict.ENQUIRY, "reason": "", "llm_used": True,
            "sender_type": sender_type, "fields": fields}
