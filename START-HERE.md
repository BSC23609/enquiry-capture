# Start here

Reads the `info@bharatsteels.in` mailbox, pulls out prospect details with
Claude, stores them in Postgres. Runs on GitHub Actions — no server needed.

You have already done the Azure setup. Three things left.

---


## Seed the customer book (one-time, before first sync)

You have a customer master (Customer_Contact.xlsx). Load it once so the contact
book starts complete and the pipeline only ever adds to it:

```
python tools/seed_customers.py "path\to\Customer_Contact.xlsx"
```

It prints how many customers loaded and — importantly — how many are missing an
email or a mobile. Those gaps are exactly what inbound mail fills in over time.

To refresh the allowlist after the customer list grows:
```
python tools\build_allowlist.py "path\to\Customer_Contact.xlsx"
```

---

## Want to know the cost first? (spends nothing)

Before buying any Anthropic credits, you can measure exactly how many mails
would reach the model — the free prefilter does the counting, no API key needed:

```
python estimate_cost.py --days 90
```

It reads your mailbox, runs the free filter over 90 days, and prints how many
mails would be billable plus the rupee cost that implies. Needs only the three
Azure secrets and MAILBOX in your .env — not the Anthropic key, not the database.
Run it, read the number, then buy credits with a real figure in hand.

---

## 1. Database (5 min)

1. Go to **neon.tech** → your project → create a database named `enquiries`
2. Left menu → **SQL Editor**
3. Open `schema.sql` from this folder, copy all of it, paste, **Run**
4. Left menu → **Tables** — you should see `messages`, `prospects`, `sync_state`
5. **Dashboard → Connection string** → copy the **Pooled connection** one.
   Keep it handy, it's needed in step 3.

---

## 2. Push to GitHub (5 min)

1. On **github.com** → New repository
   - Name: `enquiry-capture`
   - **Private** — this reads a company mailbox
   - Do NOT tick "Add a README" or any other initial file
2. Copy the repo URL it shows you
   (looks like `https://github.com/your-org/enquiry-capture.git`)
3. **Double-click `push.bat`** in this folder
4. Paste the URL when it asks

The script checks Git is installed, refuses to run without a `.gitignore`,
and stops hard if `.env` ever ends up staged. It shows you the file list
before committing — read it.

> If Git isn't installed, get it from https://git-scm.com/download/win,
> accept every default, then open a **new** window and run `push.bat` again.
> The new window matters — PATH doesn't update in an already-open one.

---

## 3. Add secrets and run (5 min)

On github.com, in your new repo: **Settings → Secrets and variables → Actions**

**Secrets** tab — click *New repository secret* for each:

| Name | Value |
|---|---|
| `MS_TENANT_ID` | `f3f819ba-724b-4c0b-a9c0-2aa8d12bcbcc` |
| `MS_CLIENT_ID` | `9888cc16-bd60-40d8-b5bb-4fe98831e085` |
| `MS_CLIENT_SECRET` | the Value from your Azure app registration |
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `DATABASE_URL` | Neon pooled connection string from step 1 |

**Variables** tab — click *New repository variable* for each:

| Name | Value |
|---|---|
| `MAILBOX` | `info@bharatsteels.in` |
| `INTERNAL_DOMAINS` | `bharatsteels.in,metfraa.com,crayonroofings.com,vestrics.in` |
| `LOOKBACK_DAYS` | `1` |
| `MAX_MESSAGES_PER_RUN` | `50` |

Then: **Actions** tab → **Sync enquiries** → **Run workflow** → **Run workflow**

Wait about a minute, click into the run, expand "Run sync". You want:

```
INFO  sync  Graph returned 34 message(s)
INFO  sync  NEW  Sri Venkateswara Industries Pvt Ltd   33AABCS1429B1ZX
INFO  sync  Done — fetched=34 new_prospects=6 merged=2 junk=24 errors=0
```

That's it. From then on it runs itself every 15 minutes.

---

## The step that actually matters

Setup either works or throws a clear error. The silent failure is a **real
enquiry classified as junk** — and the only way to catch it is to look.

After a couple of days, in the Neon SQL editor:

```sql
SELECT verdict, verdict_reason, count(*)
  FROM messages
 WHERE processed_at > now() - interval '7 days'
 GROUP BY 1,2 ORDER BY 3 DESC;
```

Then read the rejects:

```sql
SELECT subject, from_address, verdict_reason
  FROM messages WHERE verdict = 'junk'
 ORDER BY received_at DESC LIMIT 40;
```

If a genuine enquiry is in there, note why it was rejected and add the missing
word to `KW_PRODUCT` or `KW_INTENT` at the top of `app/extract.py`. Push the
change and the next run picks it up.

`info@` is a general-purpose address, so expect a heavy junk load —
vendor mail, invoices, portal notifications, courier updates. Budget half an
hour for this once, and it'll be right for good.

---

## What's in this folder

| File | What it is |
|---|---|
| `START-HERE.md` | this file |
| `push.bat` | pushes everything to GitHub, with safety checks |
| `SETUP-GITHUB.md` | fuller detail on the repo and Actions setup |
| `README.md` | how the system works, tuning, operating notes |
| `schema.sql` | database tables — run once in Neon |
| `smoke_test.py` | optional: tests Azure access on its own, no DB needed |
| `app/` | the service itself |
| `.github/workflows/` | the 15-minute schedule |

---

## If something breaks

| Symptom | Cause |
|---|---|
| `Token request failed [401]` | Wrong client secret — you may have copied the Secret ID instead of the Value |
| `Graph GET failed [403]` | Access policy still propagating (up to an hour), or admin consent not granted |
| `Graph GET failed [404]` | `MAILBOX` variable doesn't match a real mailbox |
| `Missing required env vars` | A secret or variable is missing, or misspelled — names are case-sensitive |
| Everything lands as `junk` | Your enquiries use words that aren't in `KW_PRODUCT`/`KW_INTENT` |
| Actions stopped running | GitHub disables schedules on repos with no commits for 60 days — push anything to re-enable |
