"""Postgres access. No ORM — the queries are short and explicit."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .config import settings

log = logging.getLogger(__name__)

# Columns the upsert is allowed to fill in on an existing row.
FILLABLE = [
    "gstin", "email", "mobile", "company_name",
    "contact_person", "enquiry_type", "city", "state_code",
]


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(settings.database_url, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ sync state
def get_delta_link(conn: psycopg.Connection, mailbox: str) -> str | None:
    row = conn.execute(
        "SELECT delta_link FROM sync_state WHERE mailbox = %s", (mailbox,)
    ).fetchone()
    return row["delta_link"] if row else None


def save_delta_link(conn: psycopg.Connection, mailbox: str, link: str | None) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (mailbox, delta_link, last_run_at,
                                last_error, consecutive_errors)
             VALUES (%s, %s, now(), NULL, 0)
        ON CONFLICT (mailbox) DO UPDATE
                SET delta_link = COALESCE(EXCLUDED.delta_link, sync_state.delta_link),
                    last_run_at = now(),
                    last_error = NULL,
                    consecutive_errors = 0
        """,
        (mailbox, link),
    )


def record_sync_error(conn: psycopg.Connection, mailbox: str, err: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (mailbox, last_run_at, last_error, consecutive_errors)
             VALUES (%s, now(), %s, 1)
        ON CONFLICT (mailbox) DO UPDATE
                SET last_run_at = now(),
                    last_error = EXCLUDED.last_error,
                    consecutive_errors = sync_state.consecutive_errors + 1
        """,
        (mailbox, err[:1000]),
    )


# ------------------------------------------------------------------ idempotency
def already_processed(conn: psycopg.Connection, graph_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM messages WHERE graph_id = %s", (graph_id,)
    ).fetchone()
    return row is not None


# ------------------------------------------------------------------ upsert
def find_prospect(
    conn: psycopg.Connection,
    gstin: str | None,
    email: str | None,
    mobile: str | None,
) -> dict[str, Any] | None:
    """Match on GSTIN first, then email, then mobile.

    GSTIN wins because it's a registered identifier — two people at the same
    company will have different emails but the same GSTIN, and that's one
    prospect, not two. FOR UPDATE locks the row so concurrent runs serialise.
    """
    for column, value in (("gstin", gstin), ("email", email), ("mobile", mobile)):
        if not value:
            continue
        row = conn.execute(
            f"SELECT * FROM prospects WHERE {column} = %s FOR UPDATE",  # noqa: S608
            (value,),
        ).fetchone()
        if row:
            row["_matched_on"] = column
            return row
    return None


def upsert_prospect(
    conn: psycopg.Connection,
    fields: dict[str, Any],
    subject: str,
    from_address: str,
) -> tuple[int, str]:
    """Insert or merge. Returns (prospect_id, 'inserted'|'updated')."""
    existing = find_prospect(
        conn, fields.get("gstin"), fields.get("email"), fields.get("mobile")
    )

    if existing is None:
        row = conn.execute(
            """
            INSERT INTO prospects
                (gstin, email, mobile, company_name, contact_person,
                 enquiry_type, city, state_code, confidence,
                 last_subject, from_address)
            VALUES (%(gstin)s, %(email)s, %(mobile)s, %(company_name)s,
                    %(contact_person)s, %(enquiry_type)s, %(city)s,
                    %(state_code)s, %(confidence)s, %(last_subject)s,
                    %(from_address)s)
            RETURNING id
            """,
            {**fields, "last_subject": subject[:500], "from_address": from_address},
        ).fetchone()
        return row["id"], "inserted"

    # --- merge path ---
    # Only ever fill blanks. Never overwrite a value, because a human may have
    # corrected it — and a correction that silently reverts is worse than no
    # automation at all. locked_fields lets someone pin a value explicitly.
    locked = set(existing.get("locked_fields") or [])
    updates: dict[str, Any] = {}

    for col in FILLABLE:
        new_val = fields.get(col)
        if new_val and not existing.get(col) and col not in locked:
            updates[col] = new_val

    # Confidence only ever improves.
    rank = {"low": 0, "medium": 1, "high": 2, None: -1}
    if rank.get(fields.get("confidence"), -1) > rank.get(existing.get("confidence"), -1):
        updates["confidence"] = fields["confidence"]

    set_sql = ", ".join(f"{c} = %({c})s" for c in updates)
    if set_sql:
        set_sql += ", "

    conn.execute(
        f"""
        UPDATE prospects
           SET {set_sql}
               last_seen = now(),
               times_seen = times_seen + 1,
               last_subject = %(last_subject)s,
               updated_at = now()
         WHERE id = %(id)s
        """,  # noqa: S608 — column names come from the FILLABLE allowlist
        {**updates, "id": existing["id"], "last_subject": subject[:500]},
    )
    return existing["id"], "updated"


# ------------------------------------------------------------------ audit
def record_message(
    conn: psycopg.Connection,
    *,
    graph_id: str,
    internet_msg_id: str | None,
    received_at: str | None,
    subject: str,
    from_address: str,
    from_name: str,
    body_snippet: str,
    verdict: str,
    verdict_reason: str,
    prospect_id: int | None,
    extracted: dict[str, Any] | None,
    llm_used: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO messages
            (graph_id, internet_msg_id, received_at, subject, from_address,
             from_name, body_snippet, verdict, verdict_reason, prospect_id,
             extracted, llm_used)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (graph_id) DO NOTHING
        """,
        (
            graph_id, internet_msg_id, received_at, subject[:500], from_address,
            from_name[:200], body_snippet[:4000], verdict, verdict_reason[:500],
            prospect_id, json.dumps(extracted) if extracted else None, llm_used,
        ),
    )


# ==================================================================
# Customer contact book
# ==================================================================

def _norm_email(v):
    return (v or "").strip().lower() or None


def _norm_mobile(v):
    import re
    if not v:
        return None
    d = re.sub(r"\D", "", str(v))
    if len(d) > 10:
        d = d[-10:]
    return d if len(d) == 10 and d[0] in "6789" else None


def _log_enrichment(conn, customer_id, field, old, new, graph_id):
    conn.execute(
        """INSERT INTO customer_enrichment_log
               (customer_id, field, old_value, new_value, graph_id)
           VALUES (%s,%s,%s,%s,%s)""",
        (customer_id, field, old, new, graph_id),
    )


def find_customer(conn, email, mobile):
    """Match an inbound mail to a customer: exact email first, then mobile.

    Also checks the *_alt arrays, so a customer who once wrote from a second
    address is still recognised. FOR UPDATE locks the row against concurrent
    enrichment.
    """
    email = _norm_email(email)
    mobile = _norm_mobile(mobile)

    if email:
        row = conn.execute(
            """SELECT * FROM customers
                WHERE email = %s OR %s = ANY(email_alt)
                FOR UPDATE""",
            (email, email),
        ).fetchone()
        if row:
            return row
    if mobile:
        row = conn.execute(
            """SELECT * FROM customers
                WHERE mobile = %s OR %s = ANY(mobile_alt)
                FOR UPDATE""",
            (mobile, mobile),
        ).fetchone()
        if row:
            return row
    return None


def enrich_or_create_customer(conn, fields, graph_id, received_at):
    """Apply an inbound mail to the customer book.

    sender_type governs the destination:
      - "customer"  -> enrich an existing row, or auto-create one
      - anything else -> parked in non_customers, book untouched

    Enrichment fills BLANK primary fields only. A different email/mobile than
    the one on file is appended to the *_alt array, never overwritten. Every
    change is written to customer_enrichment_log.

    Returns (action, customer_id|None):
      action in {"enriched", "created", "noop", "non_customer", "skipped"}
    """
    sender_type = fields.get("sender_type") or "other"
    email = _norm_email(fields.get("email"))
    mobile = _norm_mobile(fields.get("mobile"))
    company = (fields.get("company_name") or "").strip() or None
    person = (fields.get("contact_person") or "").strip() or None
    gstin = (fields.get("gstin") or "").strip() or None
    city = (fields.get("city") or "").strip() or None

    if not (email or mobile):
        return "skipped", None

    # ---- non-customers: keep out of the book ----
    if sender_type != "customer":
        _upsert_non_customer(conn, sender_type, company, person, email, mobile,
                             gstin, fields.get("last_subject"))
        return "non_customer", None

    # ---- customers ----
    existing = find_customer(conn, email, mobile)

    if existing is None:
        row = conn.execute(
            """INSERT INTO customers
                   (company_name, contact_person, email, mobile, gstin, city,
                    source, sender_type, times_seen, last_contact_at)
               VALUES (%s,%s,%s,%s,%s,%s,'email','customer',1,%s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (company, person, email, mobile, gstin, city, received_at),
        ).fetchone()
        if row:
            _log_enrichment(conn, row["id"], "created", None,
                            company or email or mobile, graph_id)
            return "created", row["id"]
        # lost a race — fall through and treat as existing
        existing = find_customer(conn, email, mobile)
        if existing is None:
            return "skipped", None

    cid = existing["id"]
    locked = set(existing.get("locked_fields") or [])
    changed = False

    def fill_primary(col, new):
        nonlocal changed
        if new and not existing.get(col) and col not in locked:
            conn.execute(
                f"UPDATE customers SET {col} = %s, updated_at = now() WHERE id = %s",  # noqa: S608
                (new, cid),
            )
            _log_enrichment(conn, cid, col, None, new, graph_id)
            changed = True

    fill_primary("company_name", company)
    fill_primary("contact_person", person)
    fill_primary("gstin", gstin)
    fill_primary("city", city)
    fill_primary("email", email)     # only fills if the row had no email at all
    fill_primary("mobile", mobile)   # only fills if the row had no mobile at all

    # A different address/number than what's on file -> append to alternates.
    def add_alt(primary_col, alt_col, value):
        nonlocal changed
        if not value:
            return
        if value == existing.get(primary_col):
            return
        current_alt = existing.get(alt_col) or []
        if value in current_alt:
            return
        # also skip if we just filled the primary with this exact value
        fresh = conn.execute(
            f"SELECT {primary_col} FROM customers WHERE id = %s", (cid,)  # noqa: S608
        ).fetchone()
        if fresh and fresh[primary_col] == value:
            return
        conn.execute(
            f"UPDATE customers SET {alt_col} = array_append({alt_col}, %s), "  # noqa: S608
            f"updated_at = now() WHERE id = %s",
            (value, cid),
        )
        _log_enrichment(conn, cid, alt_col, None, value, graph_id)
        changed = True

    add_alt("email", "email_alt", email)
    add_alt("mobile", "mobile_alt", mobile)

    conn.execute(
        "UPDATE customers SET times_seen = times_seen + 1, "
        "last_contact_at = %s, updated_at = now() WHERE id = %s",
        (received_at, cid),
    )

    return ("enriched" if changed else "noop"), cid


def _upsert_non_customer(conn, sender_type, company, person, email, mobile,
                         gstin, subject):
    email = _norm_email(email)
    mobile = _norm_mobile(mobile)
    key_col, key_val = ("email", email) if email else ("mobile", mobile)
    if not key_val:
        return
    # Partial unique indexes (WHERE ... IS NOT NULL) don't work as ON CONFLICT
    # targets without matching the predicate, so match-then-update explicitly.
    existing = conn.execute(
        f"SELECT id FROM non_customers WHERE {key_col} = %s",  # noqa: S608 — fixed pair
        (key_val,),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE non_customers SET
                   times_seen = times_seen + 1,
                   last_subject = %s,
                   sender_type = COALESCE(sender_type, %s),
                   last_seen = now()
                WHERE id = %s""",
            ((subject or "")[:500], sender_type, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO non_customers
                   (sender_type, company_name, contact_person, email, mobile,
                    gstin, last_subject, times_seen)
               VALUES (%s,%s,%s,%s,%s,%s,%s,1)""",
            (sender_type, company, person, email, mobile, gstin, (subject or "")[:500]),
        )


def enrichment_summary(conn, days=7):
    """What the book gained recently — for a weekly glance."""
    rows = conn.execute(
        """SELECT field, count(*) AS n
             FROM customer_enrichment_log
            WHERE created_at > now() - (%s || ' days')::interval
            GROUP BY field ORDER BY n DESC""",
        (days,),
    ).fetchall()
    return {r["field"]: r["n"] for r in rows}


def fetch_all_customers(conn):
    """Every customer, for the OneDrive mirror export."""
    return conn.execute(
        """SELECT bp_code, company_name, contact_person, email, mobile,
                  email_alt, mobile_alt, gstin, city, address, zip_code,
                  source, times_seen, last_contact_at
             FROM customers
            ORDER BY company_name NULLS LAST, id"""
    ).fetchall()
