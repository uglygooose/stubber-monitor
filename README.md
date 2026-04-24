# Stubber Agent Monitor

Web service that receives daily health-check results from Stubber and
displays them on a dashboard. All monitoring logic runs *inside*
Stubber — this service just accepts POSTs, stores them, and shows you
the data.

## Architecture

```
Stubber (daily scheduled template)
    │
    │  1. stubberdb_run_sql — queries the stubs table
    │  2. code — evaluates health rules
    │  3. apicall — POST to /ingest with bearer token
    │
    ▼
Render (free tier web service)
    │  - /ingest   receives check payloads
    │  - /         dashboard UI
    │  - /healthz  liveness check
    │
    ▼
Supabase (free Postgres)
    checks + issues tables
```

This repo contains the web service. The Stubber template is built
separately inside Stubber's UI — instructions are further down.

## Layout

```
stubber-monitor/
├── app.py              # Flask app — ingest, dashboard, healthz
├── templates/
│   └── index.html      # Dashboard UI
├── static/
│   └── style.css
├── post_demo.py        # Post fake data to preview the dashboard
├── requirements.txt
├── render.yaml         # Render deployment blueprint
├── .gitignore
└── README.md
```

---

## Part 1 — Deploy the web service

### What you need before starting

- Supabase project created (you should have the database password saved)
- Render account created
- GitHub account (Render pulls code from GitHub)
- Git installed on your PC — download from <https://git-scm.com/download/win>
  if you don't have it

### Step 1: Get the Supabase connection string

1. In Supabase, open your project dashboard
2. Click the **Connect** button (top of the page, looks like a database
   connection icon)
3. In the dialog, select the **"Session pooler"** tab (not "Direct
   connection" — the session pooler is more stable for this use case
   and works through corporate firewalls)
4. Copy the connection string. It looks like:
   ```
   postgresql://postgres.xxxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:5432/postgres
   ```
5. **Replace `[YOUR-PASSWORD]`** with the actual database password you
   saved when creating the project
6. Save this full string somewhere — you'll paste it into Render in a
   moment

### Step 2: Generate an ingest token

This is the secret that Stubber uses to prove its POSTs are legitimate.
Generate a long random string. In PowerShell:

```powershell
# 40 random alphanumeric characters
-join ((48..57) + (65..90) + (97..122) | Get-Random -Count 40 | ForEach-Object {[char]$_})
```

Copy the output. Save it — you'll need it in Render AND in Stubber.

### Step 3: Put this code into a GitHub repo

Render deploys from GitHub. You need the code there.

In PowerShell, from the `stubber-monitor` folder:

```powershell
git init
git add .
git commit -m "initial commit"
```

Then on GitHub:
1. Go to <https://github.com/new>
2. Name the repo `stubber-monitor` (or whatever you like)
3. Keep it **Private**
4. Don't tick any of the "Initialize this repository" options — we
   already have files
5. Click **Create repository**
6. GitHub shows you a "push an existing repository" snippet — copy the
   three lines and run them in PowerShell. They look like:
   ```powershell
   git remote add origin https://github.com/YOUR-USERNAME/stubber-monitor.git
   git branch -M main
   git push -u origin main
   ```

Refresh the GitHub page — your files should now be there.

### Step 4: Deploy to Render

1. Go to <https://dashboard.render.com/>
2. Click **New** (top right) → **Blueprint**
3. Connect your GitHub account if you haven't already, and grant access
   to the `stubber-monitor` repo
4. Select the `stubber-monitor` repo
5. Render reads `render.yaml` and offers to create the service. Click
   **Apply**
6. Render will prompt for the two environment variables it can't
   auto-generate. Paste:
   - **DATABASE_URL** → the Supabase connection string from step 1
   - **INGEST_TOKEN** → the random string from step 2
7. Click **Deploy** and wait ~3–5 minutes for the build

When it's done, Render shows you a URL like
`https://stubber-monitor-xxxx.onrender.com`. Open it in a browser.

You should see the dashboard with an empty state saying "No checks
received yet." **That's the success state for this step.**

If you see an error page, check Render's **Logs** tab — most issues
are either a typo in DATABASE_URL or the password wasn't swapped in.

### Step 5: Sanity check — post demo data

On your PC, in PowerShell (same folder):

```powershell
python post_demo.py https://your-render-url.onrender.com YOUR_INGEST_TOKEN
```

Replace both placeholders with your real values. It will post 14 days
of fake check history. Refresh the dashboard — you should see a
"Rep Stock Manager" agent card, weekly stats, one critical issue, and
a history table with ~14 rows.

**Important:** Render's free tier sleeps after 15 minutes of
inactivity. The first request after a sleep takes 30–60 seconds to
wake. Don't worry if the first load is slow; subsequent loads are
instant. The daily POST from Stubber also wakes it.

---

## Part 2 — Build the Stubber monitor template

This is where the actual health-check logic lives. Built inside
Stubber's template editor.

### Overview of what we're building

A Stubber template with:
- A **Schedule Task** that triggers daily at 07:00 Africa/Johannesburg
- One action containing:
  1. `stubberdb_run_sql` — query the stubs table for the last 24h
  2. `code` task — evaluate health rules and build the payload
  3. `apicall` — POST the payload to this service's `/ingest`

### Prerequisite: create the `INGEST_TOKEN` credential in Stubber

Before building the template, add your ingest token as a Stubber
credential so it's not hardcoded in the template JSON.

1. In Stubber's management console, go to **Credentials**
2. Click **Add Credential** (or similar)
3. Name it `monitor_ingest_token` (anything, but use this name for
   clarity)
4. Value: the same `INGEST_TOKEN` you set in Render
5. Save and copy the credential UUID

### Building the template

*(This section will be filled in step-by-step together — the exact UI
varies. For now, the JSON-level shape of each task looks like this,
based on Stubber's documented patterns.)*

Key queries the `stubberdb_run_sql` task will run:

```sql
SELECT
  COUNT(*) AS total_stubs,
  SUM(CASE WHEN metrics_is_flagged THEN 1 ELSE 0 END) AS flagged_count,
  SUM(CASE WHEN updated_at < NOW() - INTERVAL '6 hours' AND state = 'active'
           THEN 1 ELSE 0 END) AS stuck_count,
  MAX(updated_at) AS latest_update
FROM stubs
WHERE program_templateuuid = '308f49cc-7c8d-53b1-bf40-4e6cd43895fe'
  AND updated_at >= NOW() - INTERVAL '24 hours';
```

The code task then builds the payload and posts it:

```json
{
  "agent_name": "Rep Stock Manager",
  "template_uuid": "308f49cc-7c8d-53b1-bf40-4e6cd43895fe",
  "status": "healthy | degraded | down",
  "stubs_checked": 37,
  "errors_count": 0,
  "stuck_count": 0,
  "flagged_count": 0,
  "agent_live": true,
  "summary": "Brief human-readable line",
  "issues": []
}
```

---

## The ingest API

For anyone writing their own poster (instead of Stubber), here's the
contract.

### POST /ingest

**Auth:** `Authorization: Bearer <INGEST_TOKEN>` (required)

**Content-Type:** `application/json`

**Body:**
```json
{
  "agent_name": "string, required, up to 200 chars",
  "template_uuid": "string, required, up to 64 chars",
  "status": "healthy | degraded | down | error",
  "stubs_checked": 0,
  "errors_count": 0,
  "stuck_count": 0,
  "flagged_count": 0,
  "agent_live": true,
  "summary": "optional, up to 500 chars",
  "issues": [
    {
      "severity": "critical | warning | info",
      "rule": "string rule name",
      "message": "human-readable description",
      "details": { "any": "json object" }
    }
  ]
}
```

**Responses:**
- `201` — created, returns `{"ok": true, "check_id": N}`
- `400` — validation error, returns `{"error": "..."}`
- `401` — missing or wrong bearer token
- `500` — database error (check Render logs)

---

## Local development

If you want to run this on your PC to iterate on the dashboard:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Point it at the same Supabase DB as production, or a local one
$env:DATABASE_URL = "postgresql://..."
$env:INGEST_TOKEN = "any-string-for-local-dev"

python app.py
```

Dashboard is at <http://127.0.0.1:5000>.

---

## Troubleshooting

**"DATABASE_URL is not set"** — you didn't configure it in Render's
env vars, or a typo crept in. Render dashboard → service → Environment
tab.

**Dashboard loads but shows "No checks received yet"** — nothing has
POSTed. Run `post_demo.py` to verify the ingest path works, then look
at why Stubber isn't reaching you (most likely the token mismatch, or
the Schedule Task hasn't fired yet).

**First dashboard load is slow (30–60s)** — Render free tier cold
start. Fine, ignore it. If it matters operationally, upgrade to
Render's $7/mo Starter plan (no sleep).

**Render build fails with "psycopg error"** — usually a Python version
mismatch. `render.yaml` pins Python 3.12.3; make sure your local
`requirements.txt` versions are compatible with that.

**Dashboard timezone** — all timestamps are displayed in UTC. If you
want SAST (Africa/Johannesburg, UTC+2), edit `format_dt` in `app.py`.
