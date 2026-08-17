"""estimate_cost.py — how much will Claude actually cost? Spends ZERO credits.

    python estimate_cost.py               # scans LOOKBACK_DAYS of mail
    python estimate_cost.py --days 90     # scans a specific window

It reads the mailbox over Graph (which is free) and runs the SAME local
prefilter the real pipeline uses. It counts how many mails WOULD reach the
model — but never calls Anthropic, so it costs nothing and needs no API key.

Output: the number of billable mails, and the rupee cost that many implies,
for the one-time backfill and for ongoing monthly traffic.

Needs the three Azure env vars + MAILBOX. Does NOT need ANTHROPIC_API_KEY
or DATABASE_URL — this touches neither.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

# Reuse the real pipeline's Graph reader and prefilter — same code, same verdicts.
from app.config import settings
from app.extract import Verdict, clean_body, prefilter, domain_of
from app.graph import GraphClient, address_of, body_of

# --- pricing assumptions (Haiku 4.5, verified Aug 2026) ---
USD_PER_MTOK_IN = 1.0
USD_PER_MTOK_OUT = 5.0
AVG_INPUT_TOKENS = 1500      # system prompt + cleaned body, typical
AVG_OUTPUT_TOKENS = 150      # the small JSON object
USD_TO_INR = 84.0            # adjust if the rate has moved

COST_PER_MAIL_USD = (
    AVG_INPUT_TOKENS / 1_000_000 * USD_PER_MTOK_IN
    + AVG_OUTPUT_TOKENS / 1_000_000 * USD_PER_MTOK_OUT
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="Days of mail to scan (default: LOOKBACK_DAYS from env)")
    args = ap.parse_args()

    # Only the Graph creds are required here.
    for name in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MAILBOX"):
        if not os.getenv(name):
            print(f"Missing env var: {name}")
            sys.exit(1)

    if args.days:
        os.environ["LOOKBACK_DAYS"] = str(args.days)
        # settings is frozen and already loaded, so override the attr we use.
        object.__setattr__(settings, "lookback_days_on_first_run", args.days)
    # Scan everything in the window — don't let the per-run cap truncate the sample.
    object.__setattr__(settings, "max_messages_per_run", 100_000)

    window = settings.lookback_days_on_first_run
    print(f"\nScanning ~{window} days of {settings.mailbox} (no credits spent)…\n")

    client = GraphClient()
    try:
        messages, _ = client.fetch_messages(None)
    finally:
        client.close()

    total = len(messages)
    would_reach_model = 0
    reasons: Counter[str] = Counter()
    junk_senders: Counter[str] = Counter()

    for msg in messages:
        from_addr, from_name = address_of(msg, "from")
        if not from_addr:
            from_addr, from_name = address_of(msg, "sender")
        subject = (msg.get("subject") or "").strip()
        raw, ctype = body_of(msg)
        body = clean_body(raw, ctype)

        ok, reason = prefilter(subject, body, from_addr)
        if ok:
            would_reach_model += 1
        else:
            # Bucket the reason (strip the specific keyword for a clean tally).
            bucket = reason.split(":")[0].strip()
            reasons[bucket] += 1
            junk_senders[domain_of(from_addr)] += 1

    filtered = total - would_reach_model
    pct_filtered = (filtered / total * 100) if total else 0

    # --- projections ---
    per_day = would_reach_model / window if window else 0
    monthly = per_day * 30
    cost_backfill_inr = would_reach_model * COST_PER_MAIL_USD * USD_TO_INR
    cost_monthly_inr = monthly * COST_PER_MAIL_USD * USD_TO_INR

    print("=" * 58)
    print(f"  Mails scanned                : {total}")
    print(f"  Filtered for free (no cost)  : {filtered}  ({pct_filtered:.0f}%)")
    print(f"  WOULD reach the model (cost) : {would_reach_model}")
    print("=" * 58)

    print("\n  Why mail was filtered (free):")
    for reason, n in reasons.most_common():
        print(f"     {n:5}  {reason}")

    print("\n  Heaviest filtered senders (sanity-check these are really junk):")
    for dom, n in junk_senders.most_common(8):
        print(f"     {n:5}  {dom or '(no domain)'}")

    print("\n" + "-" * 58)
    print("  COST PROJECTION  (Haiku 4.5, ~₹0.19 per mail reaching model)")
    print("-" * 58)
    print(f"  One-time backfill of this {window}-day window:")
    print(f"     {would_reach_model} mails  ->  ${would_reach_model * COST_PER_MAIL_USD:.2f}"
          f"  (≈ ₹{cost_backfill_inr:.0f})")
    print(f"\n  Projected ONGOING monthly traffic:")
    print(f"     ~{monthly:.0f} mails/month  ->  ${monthly * COST_PER_MAIL_USD:.2f}"
          f"  (≈ ₹{cost_monthly_inr:.0f})")
    print("-" * 58)

    # --- purchase guidance ---
    # 3-month backfill + 3 months ongoing, then round up to a sane top-up.
    backfill_90 = (per_day * 90) * COST_PER_MAIL_USD
    three_mo_run = monthly * 3 * COST_PER_MAIL_USD
    suggested = backfill_90 + three_mo_run
    print(f"\n  If your enquiry rate holds, a 90-day backfill plus 3 months")
    print(f"  of ongoing capture would cost about ${suggested:.2f}"
          f"  (≈ ₹{suggested * USD_TO_INR:.0f}).")
    buy = 20 if suggested < 15 else 50
    print(f"  A ${buy} top-up covers that with comfortable headroom.\n")

    print("  NOTE: this counts mails passing the FREE keyword prefilter.")
    print("  Claude then rejects some as 'not an enquiry' — so the real")
    print("  billable count and cost are a bit LOWER than shown here.")
    print("  Treat these figures as a safe upper bound.\n")

    intent_no_product = sum(
        n for r, n in reasons.items() if "i=" in r and "i=0" not in r
    )
    if intent_no_product >= 3:
        print(f"  ⚠  {intent_no_product} mails were dropped for having intent words")
        print("     (quote/price/send) but NO product keyword — this is exactly")
        print("     the shape of a known-client RFQ ('send pricing for our usual').")
        print("     If real RFQs are in there, build the known-sender allowlist")
        print("     BEFORE the backfill or you'll silently skip them.\n")


if __name__ == "__main__":
    main()
