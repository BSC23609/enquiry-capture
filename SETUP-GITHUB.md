# Repo setup

## 1. Create the repo on GitHub

github.com → **New repository**

- Name: `enquiry-capture`
- **Private** — this reads a company mailbox
- Don't add a README, .gitignore or licence (you already have them)

## 2. Get the files onto your machine

Create the folder and drop in every file I shared, in this layout:

```
enquiry-capture/
├── .github/
│   └── workflows/
│       └── sync.yml
├── app/
│   ├── __init__.py          ← empty file, must exist
│   ├── config.py
│   ├── db.py
│   ├── extract.py
│   ├── graph.py
│   ├── main.py
│   └── sync.py
├── .gitignore
├── .env.example
├── README.md
├── requirements.txt
├── schema.sql
└── smoke_test.py
```

On Windows, create the empty package marker with:

```powershell
New-Item -Path .\app\__init__.py -ItemType File
```

## 3. Push it

```bash
cd enquiry-capture
git init
git add .
git status          # ← STOP. Confirm .env is NOT in this list.
git commit -m "Enquiry capture: Graph + Claude + Postgres"
git branch -M main
git remote add origin https://github.com/<your-org>/enquiry-capture.git
git push -u origin main
```

> The `git status` check is not optional. `.gitignore` covers `.env`, but if
> you created the file before the ignore rule existed, git may already be
> tracking it — and a client secret in git history is painful to remove
> properly. Look at the list before you commit.

## 4. Add the secrets

Repo → **Settings → Secrets and variables → Actions**

**Secrets tab** → New repository secret, one each:

| Name | Value |
|---|---|
| `MS_TENANT_ID` | f3f819ba-724b-4c0b-a9c0-2aa8d12bcbcc |
| `MS_CLIENT_ID` | 9888cc16-bd60-40d8-b5bb-4fe98831e085 |
| `MS_CLIENT_SECRET` | the Value from the Azure app registration |
| `ANTHROPIC_API_KEY` | your key |
| `DATABASE_URL` | Neon pooled connection string |

**Variables tab** → New repository variable (these aren't secret, and being
visible makes debugging easier):

| Name | Value |
|---|---|
| `MAILBOX` | info@bharatsteels.in |
| `INTERNAL_DOMAINS` | bharatsteels.in,metfraa.com,crayonroofings.com,vestrics.in |
| `LOOKBACK_DAYS` | 1 |
| `MAX_MESSAGES_PER_RUN` | 50 |

Start with `LOOKBACK_DAYS=1` and `MAX_MESSAGES_PER_RUN=50`. Widen once you've
read the first batch of output.

## 5. Run it manually first

Repo → **Actions** tab → **Sync enquiries** → **Run workflow**

Watch the log. You're looking for:

```
INFO  sync  Graph returned 34 message(s)
INFO  sync  Done — fetched=34 new_prospects=6 merged=2 junk=24 ...
```

If it fails, the log tells you which step and why. That's the whole reason
this is better than a low-code tool: the failure is legible.

## 6. Let the schedule take over

Once a manual run is clean, the `*/15 * * * *` cron in `sync.yml` runs it
automatically. Nothing else to do — no VM, no cron daemon, no always-on box.

Bump `LOOKBACK_DAYS` to 7 in the variables once you're happy; it only matters
on a first run or after a cursor reset anyway.

---

## Why GitHub Actions rather than a server

- **Nothing to maintain.** No VM to patch, no daemon to keep alive.
- **Secrets are managed properly** — encrypted, masked in logs, rotatable
  without touching the code.
- **Every run is logged** and kept for 90 days, with the full stdout.
- **Free tier covers it.** 2,000 minutes/month on free plans; a sync run takes
  well under a minute, so ~96 runs/day lands around 50 minutes/month. Private
  repos on a paid plan get more.

### Caveats worth knowing

- **Scheduled runs can be delayed** at peak times, sometimes by 10+ minutes.
  Fine for enquiry capture. Not fine if you ever need sub-minute latency.
- **GitHub disables schedules on repos with no activity for 60 days.** If the
  repo goes quiet, the cron silently stops. Push something occasionally, or
  set a calendar reminder to check the Actions tab quarterly.
- **Neon cold starts.** On the free tier the database sleeps after inactivity
  and the first connection can take a few seconds. The sync handles it, but
  if you see intermittent connection timeouts, that's the cause.

### If you'd rather run it beside the Metfraa Portal

`app/main.py` is already a FastAPI app. Deploy it to Vercel from this same
repo and point Vercel Cron at `/sync`:

```json
{ "crons": [{ "path": "/sync", "schedule": "*/15 * * * *" }] }
```

Set `CRON_SECRET` in the environment and the endpoint rejects anything without
the matching header. Either approach works — Actions needs less setup, Vercel
puts the review UI and the sync in one place.
