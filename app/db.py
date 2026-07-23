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
