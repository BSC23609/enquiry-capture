"""seed_customers.py — load the customer master into Postgres, once.

    python tools/seed_customers.py "path/to/Customer_Contact.xlsx"

Run this ONCE at setup, before the first sync, so the contact book starts
complete and the pipeline only ever adds to it. Safe to re-run: it upserts on
bp_code, so re-loading an updated master fills gaps without creating duplicates.

Columns are matched case/space-insensitively: BP Code, BP Name, e-mail,
mobile, street/address, zip. Anything else is ignored.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pandas as pd
import psycopg

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.getenv("DATABASE_URL", "")
INTERNAL = {"bharatsteels.in", "metfraa.com", "crayonroofings.com", "vestrics.in"}
TYPO_FIX = {"gamil.com": "gmail.com", "gmial.com": "gmail.com", "gmai.com": "gmail.com"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def find_col(df: pd.DataFrame, *needles: str) -> str | None:
    for c in df.columns:
        low = str(c).strip().lower()
        if any(n in low for n in needles):
            return c
    return None


def clean_emails(raw) -> list[str]:
    if pd.isna(raw):
        return []
    out = []
    for part in re.split(r"[;,/\s]+", str(raw).strip().lower()):
        p = part.strip().strip('"').strip("'")
        for bad, good in TYPO_FIX.items():
            if p.endswith(bad):
                p = p[: -len(bad)] + good
        if EMAIL_RE.fullmatch(p):
            local, dom = p.split("@", 1)
            if re.fullmatch(r"\d+", local) or dom in INTERNAL:
                continue
            out.append(p)
    # de-dupe, preserve order
    seen, uniq = set(), []
    for e in out:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def clean_mobiles(raw) -> list[str]:
    if pd.isna(raw):
        return []
    nums = re.findall(r"[6-9]\d{9}", re.sub(r"\D", " ", str(raw)))
    seen, uniq = set(), []
    for n in nums:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def clean(v) -> str | None:
    if pd.isna(v):
        return None
    s = str(v).strip().replace('"', "").strip()
    return s or None


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if not DATABASE_URL:
        print("DATABASE_URL not set. export it (or put it in .env) first.")
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"File not found: {src}")
        sys.exit(1)

    df = pd.read_excel(src, header=0)
    df.columns = [str(c).strip() for c in df.columns]

    c_bp = find_col(df, "bp code", "code")
    c_name = find_col(df, "bp name", "name", "company")
    c_email = find_col(df, "mail", "email")
    c_mobile = find_col(df, "mobile", "phone")
    c_addr = find_col(df, "street", "address")
    c_zip = find_col(df, "zip", "pin")
    print(f"Columns → bp:{c_bp!r} name:{c_name!r} email:{c_email!r} "
          f"mobile:{c_mobile!r} addr:{c_addr!r} zip:{c_zip!r}")

    inserted = updated = skipped = 0
    processed = 0

    with psycopg.connect(DATABASE_URL) as conn:
        for _, row in df.iterrows():
            emails = clean_emails(row.get(c_email)) if c_email else []
            mobiles = clean_mobiles(row.get(c_mobile)) if c_mobile else []
            bp = clean(row.get(c_bp)) if c_bp else None
            name = clean(row.get(c_name)) if c_name else None

            # a row with no identifier at all is useless
            if not (emails or mobiles or bp):
                skipped += 1
                continue

            primary_email = emails[0] if emails else None
            primary_mobile = mobiles[0] if mobiles else None
            email_alt = emails[1:] if len(emails) > 1 else []
            mobile_alt = mobiles[1:] if len(mobiles) > 1 else []

            addr = clean(row.get(c_addr)) if c_addr else None
            zipc = clean(row.get(c_zip)) if c_zip else None

            # A per-row SAVEPOINT so a duplicate email/mobile rolls back ONLY
            # this row, not the whole batch. Without this, one UniqueViolation
            # discards every insert since the last commit — which is what makes
            # a naive loader crawl on data with shared contact details.
            try:
                with conn.transaction():
                    if bp:
                        cur = conn.execute(
                            """
                            INSERT INTO customers
                                (bp_code, company_name, email, mobile, email_alt,
                                 mobile_alt, address, zip_code, source, sender_type)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'seed','customer')
                            ON CONFLICT (bp_code) DO UPDATE SET
                                company_name = COALESCE(customers.company_name, EXCLUDED.company_name),
                                email        = COALESCE(customers.email, EXCLUDED.email),
                                mobile       = COALESCE(customers.mobile, EXCLUDED.mobile),
                                address      = COALESCE(customers.address, EXCLUDED.address),
                                zip_code     = COALESCE(customers.zip_code, EXCLUDED.zip_code),
                                updated_at   = now()
                            RETURNING (xmax = 0) AS was_insert
                            """,
                            (bp, name, primary_email, primary_mobile,
                             email_alt, mobile_alt, addr, zipc),
                        ).fetchone()
                        if cur and cur[0]:
                            inserted += 1
                        else:
                            updated += 1
                    else:
                        cur = conn.execute(
                            """
                            INSERT INTO customers
                                (company_name, email, mobile, email_alt, mobile_alt,
                                 address, zip_code, source, sender_type)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,'seed','customer')
                            ON CONFLICT DO NOTHING
                            RETURNING id
                            """,
                            (name, primary_email, primary_mobile,
                             email_alt, mobile_alt, addr, zipc),
                        ).fetchone()
                        if cur:
                            inserted += 1
                        else:
                            skipped += 1
            except psycopg.errors.UniqueViolation:
                # a bp-coded row whose email/mobile already belongs to another
                # customer — skip it, the savepoint already rolled back just this row
                skipped += 1

            processed += 1
            if processed % 250 == 0:
                print(f"  ...{processed} rows processed "
                      f"({inserted} in, {updated} upd, {skipped} skip)")

        # Explicit commit, then verify on THIS SAME connection before closing.
        conn.commit()
        check = conn.execute("SELECT count(*) FROM customers").fetchone()[0]
        print(f"\n[verify] this connection sees {check} rows after commit")
        if check < inserted:
            print(f"[verify] WARNING: expected at least {inserted}, saw {check}. "
                  "Writes are not persisting — likely the DATABASE_URL host is not "
                  "the one your SQL editor reads, or a pooler is dropping the session.")

    print(f"\nSeed complete → inserted {inserted}, updated {updated}, skipped {skipped}")
    with psycopg.connect(DATABASE_URL) as conn:
        total = conn.execute("SELECT count(*) FROM customers").fetchone()[0]
        with_email = conn.execute(
            "SELECT count(*) FROM customers WHERE email IS NOT NULL").fetchone()[0]
        with_mobile = conn.execute(
            "SELECT count(*) FROM customers WHERE mobile IS NOT NULL").fetchone()[0]
        missing_email = conn.execute(
            "SELECT count(*) FROM customers WHERE email IS NULL").fetchone()[0]
        missing_mobile = conn.execute(
            "SELECT count(*) FROM customers WHERE mobile IS NULL").fetchone()[0]
    print(f"Customers in book: {total}")
    print(f"  have email: {with_email}   missing email: {missing_email}")
    print(f"  have mobile: {with_mobile}   missing mobile: {missing_mobile}")
    print("\nThe missing-email / missing-mobile rows are exactly what inbound")
    print("mail will now fill in over time.")


if __name__ == "__main__":
    main()
