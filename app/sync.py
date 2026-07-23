"""One sync pass. Run this from cron / Vercel Cron / a systemd timer.

    python -m app.sync

Safe to run as often as you like — Graph delta means an empty run costs one
HTTP call, and every message is keyed on its Graph id so nothing is ever
processed twice.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

from . import db
from .config import settings
from .extract import Verdict, clean_body, extract
from .graph import GraphClient, address_of, body_of

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
)
log = logging.getLogger("sync")


@dataclass
class RunStats:
    fetched: int = 0
    skipped_seen: int = 0
    junk: int = 0
    no_contact: int = 0
    errors: int = 0
    inserted: int = 0
    updated: int = 0
    llm_calls: int = 0

    def summary(self) -> str:
        return (
            f"fetched={self.fetched} new_prospects={self.inserted} "
            f"merged={self.updated} junk={self.junk} no_contact={self.no_contact} "
            f"errors={self.errors} llm_calls={self.llm_calls} "
            f"already_seen={self.skipped_seen}"
        )


def run_once() -> RunStats:
    settings.validate()
    stats = RunStats()
    client = GraphClient()

    try:
        with db.connect() as conn:
            delta = db.get_delta_link(conn, settings.mailbox)

        log.info("Fetching from %s (%s delta cursor)",
                 settings.mailbox, "with" if delta else "no")
        messages, new_delta = client.fetch_messages(delta)
        stats.fetched = len(messages)
        log.info("Graph returned %d message(s)", len(messages))

        for msg in messages:
            graph_id = msg["id"]

            # Each message gets its own transaction. One bad mail must not
            # roll back the whole batch.
            try:
                with db.connect() as conn:
                    if db.already_processed(conn, graph_id):
                        stats.skipped_seen += 1
                        continue

                    from_addr, from_name = address_of(msg, "from")
                    if not from_addr:
                        from_addr, from_name = address_of(msg, "sender")

                    subject = (msg.get("subject") or "").strip()
                    raw, ctype = body_of(msg)
                    body = clean_body(raw, ctype)

                    result = extract(subject, from_addr, from_name, body)
                    verdict = result["verdict"]
                    prospect_id = None

                    if verdict == Verdict.ENQUIRY:
                        prospect_id, action = db.upsert_prospect(
                            conn, result["fields"], subject, from_addr
                        )
                        if action == "inserted":
                            stats.inserted += 1
                            log.info("NEW  %-40s %s",
                                     result["fields"].get("company_name") or "(no company)",
                                     result["fields"].get("gstin") or "")
                        else:
                            stats.updated += 1
                    elif verdict == Verdict.JUNK:
                        stats.junk += 1
                    elif verdict == Verdict.NO_CONTACT:
                        stats.no_contact += 1
                    else:
                        stats.errors += 1
                        log.warning("ERROR on %s: %s", subject[:60], result["reason"])

                    if result["llm_used"]:
                        stats.llm_calls += 1

                    db.record_message(
                        conn,
                        graph_id=graph_id,
                        internet_msg_id=msg.get("internetMessageId"),
                        received_at=msg.get("receivedDateTime"),
                        subject=subject,
                        from_address=from_addr,
                        from_name=from_name,
                        body_snippet=body,
                        verdict=verdict,
                        verdict_reason=result["reason"],
                        prospect_id=prospect_id,
                        extracted=result["fields"] or None,
                        llm_used=result["llm_used"],
                    )
            except Exception:  # noqa: BLE001
                stats.errors += 1
                log.exception("Failed on message %s", graph_id)

        # Only advance the cursor once the whole batch is durably written.
        if new_delta:
            with db.connect() as conn:
                db.save_delta_link(conn, settings.mailbox, new_delta)
                log.info("Delta cursor advanced")
        else:
            log.info("No delta cursor returned — will resume from same point")

    except Exception as exc:  # noqa: BLE001
        log.exception("Sync run failed")
        try:
            with db.connect() as conn:
                db.record_sync_error(conn, settings.mailbox, str(exc))
        except Exception:  # noqa: BLE001
            log.exception("Could not even record the error")
        raise
    finally:
        client.close()

    log.info("Done — %s", stats.summary())
    return stats


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        sys.exit(1)
