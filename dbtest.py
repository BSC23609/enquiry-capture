"""Ten-second test: does a write to Neon actually persist?
   python dbtest.py
Reads DATABASE_URL from .env, inserts one marker row, commits, reopens a NEW
connection, and checks the row is there. Tells you plainly if writes vanish.
"""
import os
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
import psycopg

url = os.getenv("DATABASE_URL","")
print("DATABASE_URL host:", url.split("@")[-1].split("/")[0] if "@" in url else "(not set)")
if not url:
    raise SystemExit("DATABASE_URL not set")

# 1. write on connection A
with psycopg.connect(url) as a:
    a.execute("CREATE TABLE IF NOT EXISTS _writetest (id int)")
    a.execute("DELETE FROM _writetest")
    a.execute("INSERT INTO _writetest (id) VALUES (12345)")
    a.commit()
    seen_a = a.execute("SELECT count(*) FROM _writetest").fetchone()[0]
    print(f"connection A after commit: {seen_a} row(s)")

# 2. read on a brand-new connection B
with psycopg.connect(url) as b:
    seen_b = b.execute("SELECT count(*) FROM _writetest").fetchone()[0]
    print(f"connection B (fresh)     : {seen_b} row(s)")

if seen_b == 1:
    print("\n>>> WRITES PERSIST. Your Neon connection is fine.")
    print(">>> If the seed still shows a mismatch, it's a seed-script bug — tell Claude.")
else:
    print("\n>>> WRITES DO NOT PERSIST across connections.")
    print(">>> The DATABASE_URL in .env is not the same target your other tools read.")
    print(">>> Double-check the host/branch, or try the DIRECT (non-pooled) endpoint.")

# cleanup
with psycopg.connect(url) as c:
    c.execute("DROP TABLE IF EXISTS _writetest"); c.commit()
