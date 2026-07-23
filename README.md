# Enquiry Capture

Reads a mailbox over Microsoft Graph, extracts prospect details with Claude,
stores them in Postgres. No Power Automate, no Premium licence, no Office Script.

```
Graph delta query  →  prefilter  →  Claude  →  regex cross-check  →  Postgres
                       (free)      (~₹0.01)     (strict fields)      (Neon)
```

---

## 1. Azure app registration (one-time, ~10 min)

Entra admin centre → **App registrations → New registration**

- Name: `Enquiry Capture`
- Accounts: this organizational directory only
- No redirect URI needed

Then:

1. **Certificates & secrets → New client secret.** Copy the *value* now; it's never shown again.
2. **API permissions → Add → Microsoft Graph → Application permissions** → `Mail.Read`
3. Click **Grant admin consent**. Without this the token works but every read returns 403.
4. Copy the **Directory (tenant) ID** and **Application (client) ID** from Overview.

> **Tighten this before production.** `Mail.Read` as an application permission grants
> access to *every* mailbox in the tenant. Scope it down with an
> [application access policy](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access)
> so this app can only read the one enquiry mailbox. Two PowerShell commands,
> and it turns a tenant-wide grant into a single-mailbox grant.

## 2. Database

Neon (or any Postgres):

```bash
psql "$DATABASE_URL" -f schema.sql
```

## 3. Configure

```bash
cp .env.example .env    # fill it in
pip install -r requirements.txt
```

## 4. First run

```bash
export $(grep -v '^#' .env | xargs)
python -m app.sync
```

First run reads the last `LOOKBACK_DAYS` (default 7) and then stores a delta
cursor. Every run after that fetches only what's new.

Watch the output:

```
INFO  sync  Graph returned 34 message(s)
INFO  sync  NEW  Sri Venkateswara Industries Pvt Ltd     33AABCS1429B1ZX
INFO  sync  Done — fetched=34 new_prospects=6 merged=2 junk=24 no_contact=2 errors=0 llm_calls=10
```

`junk=24, llm_calls=10` is the prefilter doing its job — two-thirds of the batch
never reached the model.

## 5. Schedule it

**Cron** (simplest):
```cron
*/5 * * * * cd /opt/enquiry-capture && /usr/bin/python3 -m app.sync >> /var/log/enquiry.log 2>&1
```

**Vercel Cron** (if you're putting it next to the Metfraa Portal) — `vercel.json`:
```json
{ "crons": [{ "path": "/sync", "schedule": "*/10 * * * *" }] }
```
Set `CRON_SECRET` and Vercel will send it as a header; `/sync` checks it.

---

## Operating notes

**Idempotency.** Every message is keyed on its Graph id. Re-running a sync,
crashing mid-batch, or double-triggering cron cannot produce duplicate rows.

**The delta cursor only advances on success.** If a batch fails halfway, the next
run re-reads from the same point, and the `messages` table skips the ones already
done. Nothing is lost and nothing is doubled.

**Dedup priority is GSTIN → email → mobile.** GSTIN wins because it's a registered
identifier: two buyers at the same company have different emails but one GSTIN,
and that's one prospect, not two.

**Human edits are protected.** The importer only ever fills *blank* fields. If
someone corrects a company name through `PATCH /prospects/{id}`, that column is
added to `locked_fields` and no future email will overwrite it. An automation
that silently reverts a human correction is worse than no automation.

**Confidence is honest.** `high` needs a GSTIN plus most other fields. A blank
company name stays blank — the model is instructed never to guess, because an
invented company name quietly corrupts the master list in a way that's very hard
to detect later.

**Cost.** Prefilter kills ~70% for free. The rest is roughly ₹0.01 per mail on
Haiku. A thousand enquiry mails a month is under ₹100.

---

## Where this plugs into what you already run

- **GST enrichment** — any row with a GSTIN can be resolved to the authoritative
  legal name, trade name and registered address. Run it over
  `WHERE gstin IS NOT NULL AND NOT gst_enriched`, then set the flag. That's
  better data than any model infers from a signature block.
- **Master Prospect List** — this table is *staging*. Promote to the master by
  setting `status = 'promoted'` after review. Don't wire it straight through;
  an inbox-fed list with no gate fills with noise within a month.
- **Metfraa Portal** — mount `app.main:app` under `/enquiries` and you get the
  review UI for free.
- **WATI** — the same `extract.py` works on WhatsApp message bodies. Swap the
  Graph reader for a WATI webhook and the rest of the pipeline is unchanged.

## Tuning

Nearly everything you'll want to adjust is at the top of `app/extract.py`:
`JUNK_MARKERS`, `KW_PRODUCT`, `KW_INTENT`. Add to these as you see what your
actual inbox throws at it.

To check what's being rejected and why:

```sql
SELECT verdict, verdict_reason, count(*)
  FROM messages
 WHERE processed_at > now() - interval '7 days'
 GROUP BY 1, 2
 ORDER BY 3 DESC;
```

That query is the thing Power Automate never gave you: a straight answer to
"why isn't this enquiry in my list?"
