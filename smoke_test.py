"""Smoke test — proves the Azure side works before you involve the database.

    python smoke_test.py

Needs only httpx and three env vars. No Postgres, no Anthropic key.
Tells you exactly which link in the chain is broken.
"""
import os
import sys

import httpx

TENANT = os.getenv("MS_TENANT_ID", "")
CLIENT = os.getenv("MS_CLIENT_ID", "")
SECRET = os.getenv("MS_CLIENT_SECRET", "")
MAILBOX = os.getenv("MAILBOX", "info@bharatsteels.in")

FAIL = "\033[91m✗\033[0m"
OK = "\033[92m✓\033[0m"


def die(msg: str, detail: str = "") -> None:
    print(f"{FAIL} {msg}")
    if detail:
        print(f"\n   {detail}\n")
    sys.exit(1)


print(f"\nTesting Graph access to {MAILBOX}\n" + "-" * 50)

# --- 1. env present ---
missing = [n for n, v in
           {"MS_TENANT_ID": TENANT, "MS_CLIENT_ID": CLIENT,
            "MS_CLIENT_SECRET": SECRET}.items() if not v]
if missing:
    die(f"Missing env vars: {', '.join(missing)}",
        "Run:  export $(grep -v '^#' .env | xargs)")
print(f"{OK} Environment variables present")

# --- 2. token ---
resp = httpx.post(
    f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
    data={
        "client_id": CLIENT,
        "client_secret": SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    },
    timeout=30,
)

if resp.status_code != 200:
    body = resp.text
    hint = ""
    if "AADSTS7000215" in body:
        hint = "Invalid client secret. You probably copied the Secret ID instead of the Value."
    elif "AADSTS700016" in body:
        hint = "App not found in this tenant. Check MS_CLIENT_ID and MS_TENANT_ID."
    elif "AADSTS90002" in body:
        hint = "Tenant not found. Check MS_TENANT_ID."
    die(f"Token request failed [{resp.status_code}]", hint or body[:300])

token = resp.json()["access_token"]
print(f"{OK} Got an access token")

# --- 3. roles actually granted ---
import base64
import json

payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
roles = claims.get("roles", [])

if "Mail.Read" not in roles:
    die(f"Token has no Mail.Read role (roles: {roles or 'none'})",
        "API permissions → add Mail.Read as an APPLICATION permission, "
        "then click 'Grant admin consent'.")
print(f"{OK} Token carries Mail.Read  (roles: {', '.join(roles)})")

# --- 4. read the mailbox ---
headers = {"Authorization": f"Bearer {token}"}
url = (f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/mailFolders/inbox"
       f"/messages?$top=3&$select=subject,receivedDateTime,from")
r = httpx.get(url, headers=headers, timeout=30)

if r.status_code == 403:
    die("403 Forbidden reading the mailbox",
        "Either admin consent wasn't granted, or the ApplicationAccessPolicy\n"
        "   hasn't propagated yet. That can take up to an hour. Wait and retry\n"
        "   before changing anything.")
if r.status_code == 404:
    die(f"404 — mailbox '{MAILBOX}' not found",
        "Check the address is exact, and that it's a real mailbox\n"
        "   (not just an alias or a distribution group).")
if r.status_code != 200:
    die(f"Graph read failed [{r.status_code}]", r.text[:300])

msgs = r.json().get("value", [])
print(f"{OK} Read the inbox — {len(msgs)} message(s) returned\n")

for m in msgs:
    sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
    when = (m.get("receivedDateTime") or "")[:16].replace("T", " ")
    subj = (m.get("subject") or "(no subject)")[:52]
    print(f"   {when}  {sender[:32]:34} {subj}")

# --- 5. confirm the scope restriction bit ---
print("\n" + "-" * 50)
other = input("Type another mailbox to confirm it's BLOCKED (or Enter to skip): ").strip()
if other:
    r2 = httpx.get(
        f"https://graph.microsoft.com/v1.0/users/{other}/mailFolders/inbox/messages?$top=1",
        headers=headers, timeout=30,
    )
    if r2.status_code == 403:
        print(f"{OK} {other} correctly BLOCKED — access policy is working")
    elif r2.status_code == 200:
        print(f"{FAIL} {other} is READABLE — the access policy did NOT apply.")
        print("   Re-check New-ApplicationAccessPolicy, or wait for propagation.")
    else:
        print(f"   {other} returned {r2.status_code} (inconclusive)")

print(f"\n{OK} Azure side is working. Proceed to the database.\n")
