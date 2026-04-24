"""Post fake check data to /ingest so you can preview the dashboard
before your Stubber template is wired up.

Usage:
    python post_demo.py https://your-monitor.onrender.com YOUR_INGEST_TOKEN

Or against a local dev server:
    python post_demo.py http://127.0.0.1:5000 test-token-123

Generates 14 days of realistic-looking daily checks. Safe to run multiple
times — each run adds a fresh batch.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.request
from datetime import datetime, timedelta, timezone


def make_payload(days_ago: int) -> dict:
    """Build one day's check payload, varying what 'went wrong' that day."""
    roll = random.random()

    base = {
        "agent_name": "Rep Stock Manager",
        "template_uuid": "308f49cc-7c8d-53b1-bf40-4e6cd43895fe",
        "agent_live": True,
    }

    if days_ago == 2:
        # A definite bad day
        return {
            **base,
            "status": "degraded",
            "stubs_checked": 42,
            "errors_count": 5,
            "stuck_count": 1,
            "flagged_count": 0,
            "summary": "Error rate 11.9% over threshold; 1 stuck stub.",
            "issues": [
                {
                    "severity": "critical",
                    "rule": "max_error_rate_percent",
                    "message": "Error rate is 11.9% (5/42), over the 5.0% threshold.",
                    "details": {
                        "error_rate_percent": 11.9,
                        "error_count": 5,
                        "total_stubs": 42,
                    },
                },
                {
                    "severity": "critical",
                    "rule": "max_stuck_stubs",
                    "message": "1 stub has not updated in over 6h.",
                    "details": {"stuck_count": 1, "sample_stubrefs": ["2026-04-22-STUB-ABCD"]},
                },
            ],
        }

    if days_ago == 5:
        return {
            **base,
            "status": "down",
            "stubs_checked": 0,
            "errors_count": 0,
            "stuck_count": 0,
            "flagged_count": 0,
            "agent_live": False,
            "summary": "No activity for over 4 hours — agent appears offline.",
            "issues": [
                {
                    "severity": "critical",
                    "rule": "agent_quiet",
                    "message": "No stub activity in the last 120 minutes.",
                    "details": {"last_activity": "2026-04-19T02:13:00+00:00"},
                },
            ],
        }

    if roll < 0.15:
        stubs = random.randint(20, 60)
        errs = random.randint(1, 3)
        return {
            **base,
            "status": "degraded",
            "stubs_checked": stubs,
            "errors_count": errs,
            "stuck_count": 0,
            "flagged_count": 0,
            "summary": f"Error rate slightly elevated: {errs}/{stubs}.",
            "issues": [
                {
                    "severity": "warning",
                    "rule": "error_rate_elevated",
                    "message": f"{errs} errors out of {stubs} stubs.",
                },
            ],
        }

    stubs = random.randint(30, 80)
    return {
        **base,
        "status": "healthy",
        "stubs_checked": stubs,
        "errors_count": 0,
        "stuck_count": 0,
        "flagged_count": 0,
        "summary": f"All good. {stubs} stubs, 0 errors, 0 stuck.",
        "issues": [],
    }


def post(url: str, token: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        print(f"  -> {resp.status} {body[:100]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="e.g. https://your-app.onrender.com")
    parser.add_argument("token", help="The INGEST_TOKEN you set in Render")
    parser.add_argument(
        "--days", type=int, default=14,
        help="How many days of fake history to generate (default 14)"
    )
    args = parser.parse_args()

    ingest_url = args.base_url.rstrip("/") + "/ingest"
    print(f"Posting {args.days} days of demo data to {ingest_url}")

    for days_ago in range(args.days - 1, -1, -1):
        payload = make_payload(days_ago)
        print(f"Day -{days_ago}: status={payload['status']}", end="")
        try:
            post(ingest_url, args.token, payload)
        except Exception as e:
            print(f"\n  FAILED: {e}", file=sys.stderr)
            sys.exit(1)

    print("\nAll done. Refresh your dashboard to see the data.")


if __name__ == "__main__":
    main()
