# Update notes — v0.4 fleet shape

This update replaces the single-agent dashboard with a multi-agent fleet UI
and switches the schema to a register-once `agents` table + a `checks` table
that references it.

## Files in this drop

```
stubber-monitor/
├── migrate.sql                ← NEW · run once in Supabase SQL editor
├── app.py                     ← REPLACES old app.py
├── templates/
│   └── index.html             ← REPLACES old index.html
├── static/
│   └── style.css              ← REPLACES old style.css
├── post_demo.py               ← REPLACES old post_demo.py
├── requirements.txt           ← unchanged or replace
├── render.yaml                ← unchanged or replace
├── .gitignore                 ← unchanged
└── UPDATE-NOTES.md            ← this file (delete after reading)
```

## What changed

- Schema: dropped `checks` (old shape) and `issues`, created new `agents` + `checks`.
  - Old fields like `errors_count`, `stuck_count`, `agent_live` are gone.
  - New `verdict` field on every check: `healthy | degraded | down`.
  - Issues are now a JSONB array on the check itself, not a separate table.
- Endpoints:
  - **NEW** `POST /agents` — register an agent (one-time per agent)
  - **NEW** `GET /agents` — list registered agents
  - **NEW** `POST /checks` — replaces old `/ingest`, accepts the new payload
  - **NEW** `GET /api/fleet` — JSON used by the dashboard
  - `GET /` — dashboard, now fetches data client-side
  - `GET /healthz` — unchanged
- Dashboard UI: list-detail fleet shape (sidebar with all agents + mini-strips,
  detail pane shows selected agent's full 14-day history + selected day).

## Step-by-step deploy

### 1. Replace files locally

In `C:\Users\athom\Documents\Projects\stubber-monitor\`:

- Open `app.py` in Notepad → Ctrl+A → Delete → paste the new `app.py` contents → Save
- Replace `templates/index.html` the same way
- Replace `static/style.css` the same way
- Replace `post_demo.py` the same way
- Add a new file `migrate.sql` next to them (paste contents from this drop)

### 2. Run the migration in Supabase

This **deletes** the old tables and creates the new ones. You confirmed
there's only demo data, so this is safe.

1. Open your Supabase project → **SQL Editor** (left sidebar)
2. Click **New query**
3. Paste the entire contents of `migrate.sql`
4. Click **Run** (or Ctrl+Enter)
5. The output panel should say `Migration complete. agents and checks tables ready.`

If you see an error about `extension "pgcrypto" does not exist` or similar —
paste the error here, but Supabase has these by default, so it shouldn't.

### 3. Push to GitHub → Render auto-deploys

In PowerShell, from the `stubber-monitor` folder:

```powershell
git add .
git commit -m "Fleet UI: agents + checks schema, multi-agent dashboard"
git push
```

Render auto-detects the push and starts a new deploy. Watch the deploy logs
in the Render dashboard. Wait 2–3 minutes until it says "Your service is live".

### 4. Verify the dashboard loads with the empty state

Open `https://stubber-monitor.onrender.com` and hard-refresh (Ctrl+Shift+R).

You should see:
- The new dark fleet UI shell
- An "Agents 0" sidebar
- An empty state in the detail pane: *"No agents registered yet"*

If you see a stack trace or an error page, check Render's logs — usually means
either `migrate.sql` didn't run or one of the file replacements is incomplete.

### 5. Seed demo data and verify the fleet renders

In PowerShell:

```powershell
python post_demo.py https://stubber-monitor.onrender.com YOUR_INGEST_TOKEN
```

Replace `YOUR_INGEST_TOKEN` with your actual 40-char token (same one Render
already has in env vars).

Expected output:
```
Registering 5 agents → https://stubber-monitor.onrender.com/agents
  ✓ sales-qualifier
  ✓ support-triage
  ✓ booking-concierge
  ✓ feedback-analyzer
  ✓ onboarding-helper

Posting checks → https://stubber-monitor.onrender.com/checks
  ✓ sales-qualifier: 14 checks posted
  ✓ support-triage: 14 checks posted
  ✓ booking-concierge: 14 checks posted
  ✓ feedback-analyzer: 14 checks posted
  ✓ onboarding-helper: 12 checks posted        ← 12 because 2 days are 'missing'

✓ Done. 68 checks across 5 agents.
```

Hard-refresh the dashboard. You should see all 5 agents in the sidebar with
their pulsing pips and mini-strips, and the detail pane showing the first
agent's full 14-day strip.

### 6. (Optional) Clear demo data before going live

When you're ready to wire Stubber up to real data, you'll probably want to
clear the 5 sample agents. In Supabase SQL editor:

```sql
DELETE FROM agents WHERE agent_id IN (
  'sales-qualifier','support-triage','booking-concierge',
  'feedback-analyzer','onboarding-helper'
);
-- The CASCADE on checks.agent_id deletes their checks automatically.
```

That leaves the schema in place but empty. Then register your real agent:

```powershell
$body = @{
  agent_id = 'rep-stock-manager'
  name = 'Rep Stock Manager'
  description = 'Sales rep stock manager'
  model = 'claude-opus-4-7'
  deployed_at = '2025-11-03'
  template_uuid = '308f49cc-7c8d-53b1-bf40-4e6cd43895fe'
} | ConvertTo-Json -Compress

curl.exe -X POST https://stubber-monitor.onrender.com/agents `
  -H "Authorization: Bearer YOUR_INGEST_TOKEN" `
  -H "Content-Type: application/json" `
  -d $body
```

That's the one-time registration. After this, the Stubber template just
posts daily checks referencing `agent_id: rep-stock-manager`.

## What's next (Stubber side)

Once the dashboard side is up and showing the demo fleet, paste the column
listing from the SQL queries I sent (the three queries on the `stubs` table
in your draft schema). With those columns in hand, I'll write:

- The exact `stubberdb_run_sql` query
- The exact `code` task that turns SQL output into a verdict + issues
- The exact `apicall` config (URL = `/checks`, headers, body shape)

That's the last leg.
