"""Seed demo data into a deployed stubber-monitor instance.

Posts five sample agents and 14 days of check history per agent. The
sample data is designed to exercise every UI state — healthy / degraded
/ down / stale / missing — so you can verify the dashboard works before
Stubber is wired up.

Usage:
    python post_demo.py <BASE_URL> <INGEST_TOKEN>

Example:
    python post_demo.py https://stubber-monitor.onrender.com abc123def456...
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone


def post(url: str, token: str, payload: dict) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except Exception as e:
        return 0, str(e)


def make_check(day_offset: int, verdict: str, base_stubs: int, **opts) -> dict:
    """Build a check payload for a day_offset days ago."""
    now = datetime.now(timezone.utc)
    checked_at = now - timedelta(days=day_offset)
    # set the morning hour for SAST 06:14
    checked_at = checked_at.replace(hour=4, minute=14, second=0, microsecond=0)

    if verdict == "down":
        return {
            "verdict": "down",
            "checked_at": checked_at.isoformat(),
            "stubs_total": 0,
            "stubs_flagged": 0,
            "issues": opts.get("issues") or [
                {"severity": "error", "message": "0 stubs handled in past 24h"},
                {"severity": "error", "message": "Webhook delivery from Stubber → agent failed (15 retries)"},
            ],
        }

    noise = round(math.sin(day_offset * 1.7) * 18 + math.cos(day_offset * 0.7) * 12)
    stubs = max(50, base_stubs + noise)

    if verdict == "degraded":
        flagged = max(15, round(stubs * 0.043))
        issues = opts.get("issues") or [
            {"severity": "warn", "message": f"Flag rate {(flagged/stubs*100):.1f}% (baseline < 1%)"},
            {"severity": "info", "message": "Likely cause: incoming WhatsApp template change"},
        ]
        return {
            "verdict": "degraded",
            "checked_at": checked_at.isoformat(),
            "stubs_total": stubs,
            "stubs_flagged": flagged,
            "issues": issues,
        }

    # healthy
    flagged = max(0, 2 + round(math.sin(day_offset * 1.1) * 2))
    issues = opts.get("issues") or []
    if flagged > 2 and not opts.get("silent"):
        issues = [{"severity": "warn", "message": f"{flagged} stubs flagged for human review (above 0 baseline)"}]
    return {
        "verdict": "healthy",
        "checked_at": checked_at.isoformat(),
        "stubs_total": stubs,
        "stubs_flagged": flagged,
        "issues": issues,
    }


# ─── 5 sample agents — every UI state covered across the fleet ──────────
# Pattern is indexed: pattern[0] = today (offset 0), pattern[13] = oldest.
# 'missing' means no check — we skip POSTing for that day.

AGENTS = [
    {
        "agent_id": "sales-qualifier",
        "name": "Sales Qualifier",
        "description": "Inbound lead qualification · WhatsApp + email",
        "model": "claude-opus-4-7",
        "deployed_at": "2026-02-14",
        "base_stubs": 412,
        "silent": True,  # suppress flagged-warns for the boring-good baseline
        "pattern": ["healthy"] * 14,
    },
    {
        "agent_id": "support-triage",
        "name": "Support Triage",
        "description": "Customer support ticket routing",
        "model": "claude-sonnet-4-6",
        "deployed_at": "2025-11-03",
        "base_stubs": 430,
        "pattern": [
            "degraded", "healthy", "healthy", "healthy",
            "degraded", "healthy", "healthy", "healthy",
            "healthy", "healthy", "healthy", "healthy",
            "healthy", "healthy",
        ],
    },
    {
        "agent_id": "booking-concierge",
        "name": "Booking Concierge",
        "description": "Restaurant + venue reservations",
        "model": "claude-opus-4-7",
        "deployed_at": "2026-01-22",
        "base_stubs": 220,
        "pattern": [
            "down", "degraded", "healthy", "healthy",
            "healthy", "healthy", "healthy", "healthy",
            "healthy", "healthy", "healthy", "healthy",
            "healthy", "healthy",
        ],
        "overrides": {
            0: {"issues": [
                {"severity": "error", "message": "Webhook delivery to agent endpoint failed 15 consecutive times"},
                {"severity": "error", "message": "Last successful stub at 18:42 SAST yesterday"},
                {"severity": "info",  "message": "Stubber API health check: green — issue is downstream"},
            ]},
            1: {"issues": [
                {"severity": "warn", "message": "Response latency p95 climbed to 8.4s (baseline 1.8s)"},
                {"severity": "warn", "message": "Tool-call timeouts: 4 of 188 stubs"},
            ]},
        },
    },
    {
        "agent_id": "feedback-analyzer",
        "name": "Feedback Analyzer",
        "description": "Post-purchase NPS + sentiment analysis",
        "model": "claude-haiku-4-5",
        "deployed_at": "2025-12-10",
        "base_stubs": 520,
        "pattern": [
            "healthy", "healthy", "healthy", "down",
            "healthy", "healthy", "healthy", "healthy",
            "healthy", "healthy", "healthy", "healthy",
            "healthy", "healthy",
        ],
        "overrides": {
            2: {"issues": [
                {"severity": "info", "message": "New prompt version v2.4 deployed at 04:30 SAST"}
            ]},
        },
    },
    {
        "agent_id": "onboarding-helper",
        "name": "Onboarding Helper",
        "description": "New customer setup walkthrough",
        "model": "claude-sonnet-4-6",
        "deployed_at": "2026-03-08",
        "base_stubs": 145,
        "pattern": [
            "missing", "missing", "healthy", "healthy",
            "healthy", "healthy", "degraded", "healthy",
            "healthy", "healthy", "healthy", "healthy",
            "healthy", "healthy",
        ],
    },
]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    base_url = sys.argv[1].rstrip("/")
    token = sys.argv[2]

    if token == "YOUR_INGEST_TOKEN":
        print("ERROR: replace YOUR_INGEST_TOKEN with the actual token (40-char string)")
        return 2

    agents_url = f"{base_url}/agents"
    checks_url = f"{base_url}/checks"

    # ─── 1. Register all 5 agents ────────────────────────────────────────
    print(f"\nRegistering {len(AGENTS)} agents → {agents_url}")
    for a in AGENTS:
        agent_payload = {
            "agent_id": a["agent_id"],
            "name": a["name"],
            "description": a["description"],
            "model": a["model"],
            "deployed_at": a["deployed_at"],
        }
        status, body = post(agents_url, token, agent_payload)
        if status in (200, 201):
            print(f"  ✓ {a['agent_id']}")
        else:
            print(f"  ✗ {a['agent_id']} — HTTP {status}: {body[:200]}")
            if status == 401:
                print("\n→ 401 means the bearer token is wrong. Check INGEST_TOKEN.")
                return 1
            if status == 0:
                print(f"\n→ Connection failed. Is {base_url} reachable?")
                return 1

    # ─── 2. Post 14 days of checks per agent ─────────────────────────────
    print(f"\nPosting checks → {checks_url}")
    total = 0
    for a in AGENTS:
        posted = 0
        for day_offset, verdict in enumerate(a["pattern"]):
            if verdict == "missing":
                continue
            opts = a.get("overrides", {}).get(day_offset, {})
            if a.get("silent"):
                opts = {**opts, "silent": True}
            check = make_check(day_offset, verdict, a["base_stubs"], **opts)
            check["agent_id"] = a["agent_id"]
            status, body = post(checks_url, token, check)
            if status in (200, 201):
                posted += 1
                total += 1
            else:
                print(f"  ✗ {a['agent_id']} day -{day_offset} — HTTP {status}: {body[:120]}")
        print(f"  ✓ {a['agent_id']}: {posted} checks posted")

    print(f"\n✓ Done. {total} checks across {len(AGENTS)} agents.")
    print(f"  Open {base_url}/ to view the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
