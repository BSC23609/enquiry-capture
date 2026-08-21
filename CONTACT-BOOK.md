# Customer contact book — how it works

A self-healing customer directory. Seeded once from your master, then every
inbound mail completes and extends it.

## The rules

**Matching.** An incoming mail is matched to a customer by exact email, then by
exact mobile — including against the alternate-email and alternate-mobile lists.
Name-only matches are deliberately NOT used for auto-merge (too risky).

**Fill blanks only.** If a matched customer is missing an email and the mail
carries one, it's filled. A field that already has a value is never overwritten
by automation — a human correction always stands.

**Alternates, not overwrites.** If a known customer writes from a *different*
email or mobile than the one on file, it's appended to `email_alt` / `mobile_alt`.
You keep the original and gain the new one; nothing is lost.

**Auto-create by sender type.** The model classifies every sender as customer /
supplier / service_provider / marketing / other. A *customer* not already in the
book is created automatically. Everyone else is logged in `non_customers` and
never pollutes the customer book.

**Enrichment happens on ANY mail, not just enquiries.** A known customer sending
a payment confirmation still completes their record. The enquiry verdict governs
the prospects table; the contact book runs independently on sender type + fields.

**Every change is logged.** `customer_enrichment_log` records each field filled or
added, and which message supplied it. `enrichment_summary()` powers the weekly
"what the book gained" view.

## OneDrive mirror

After each run (when something changed, or always if `MIRROR_ALWAYS=1`), the whole
book is rendered to `Customer_Master.xlsx` and uploaded to the
`ai@bharatsteels.in` drive under the `Enquiry Capture` folder.

One direction only: Postgres -> OneDrive. The database is the source of truth; the
Excel file is a fresh mirror your team can open like any other file. Nothing is
read back from OneDrive — two-way sync between a DB and a hand-edited spreadsheet
is a trap, so the mirror never does it.

The file has two sheets: **Customers** (the full book) and **Book Status**
(counts of missing email/mobile, and what was enriched this week).

### Requirements for the mirror
- The Azure app needs **Files.ReadWrite.All (application)** with admin consent.
- `GRAPH_DRIVE_USER` must be a **real, licensed mailbox with a OneDrive** —
  `ai@bharatsteels.in`. A share-only address won't work as a write target; a 404
  on upload means the drive isn't real or the permission/consent is missing.
- Set `GRAPH_*` in the env, or leave them blank to reuse the `MS_*` app creds.

## Tables

| Table | What's in it |
|---|---|
| `customers` | the book — seeded + auto-created, with alternates and lock protection |
| `non_customers` | suppliers / marketing / service — captured, kept out of the book |
| `customer_enrichment_log` | audit of every field filled or added, per message |

## Useful queries

Customers still missing contact details (the gaps mail will fill):
```sql
SELECT bp_code, company_name, email, mobile
  FROM customers
 WHERE email IS NULL OR mobile IS NULL
 ORDER BY company_name;
```

What the book gained this week:
```sql
SELECT field, count(*) FROM customer_enrichment_log
 WHERE created_at > now() - interval '7 days'
 GROUP BY field ORDER BY 2 DESC;
```

Who's been reclassified as a non-customer (sanity-check the model):
```sql
SELECT sender_type, count(*) FROM non_customers GROUP BY 1 ORDER BY 2 DESC;
```
