"""Stubber agent monitor — web service.

Architecture:
    Stubber (daily scheduled action)
        ↓ POST /ingest  (bearer token auth)
    This service (hosted on Render free tier)
        ↓ writes to
    Supabase Postgres  (free forever, 0.5GB)
        ↑ reads from
    Dashboard at /  (the UI you look at)

The check logic itself lives inside Stubber (stubberdb_run_sql + code
task evaluate the rules, apicall posts the result here). All we do is
accept the POSTed results and display them.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone

import psycopg
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from psycopg.rows import dict_row


log = logging.getLogger("monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS checks (
    id              BIGSERIAL PRIMARY KEY,
    agent_name      TEXT NOT NULL,
    template_uuid   TEXT NOT NULL,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT NOT NULL,
    stubs_checked   INTEGER NOT NULL DEFAULT 0,
    errors_count    INTEGER NOT NULL DEFAULT 0,
    stuck_count     INTEGER NOT NULL DEFAULT 0,
    flagged_count   INTEGER NOT NULL DEFAULT 0,
    agent_live      BOOLEAN NOT NULL DEFAULT FALSE,
    summary         TEXT,
    raw_payload     JSONB
);

CREATE INDEX IF NOT EXISTS idx_checks_run_at ON checks(run_at DESC);
CREATE INDEX IF NOT EXISTS idx_checks_agent ON checks(agent_name, run_at DESC);

CREATE TABLE IF NOT EXISTS issues (
    id          BIGSERIAL PRIMARY KEY,
    check_id    BIGINT NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
    severity    TEXT NOT NULL,
    rule        TEXT NOT NULL,
    message     TEXT NOT NULL,
    details     JSONB,
    resolved    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_issues_check_id ON issues(check_id);
CREATE INDEX IF NOT EXISTS idx_issues_unresolved
    ON issues(resolved, created_at DESC)
    WHERE resolved = FALSE;
"""


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. In Render, add it as an environment "
            "variable using the Supabase connection string (Session pooler)."
        )
    return url


def db_connect():
    """Fresh connection per request. Low-volume service, no pool needed."""
    return psycopg.connect(get_db_url(), row_factory=dict_row)


def init_db() -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    log.info("Database schema ready")


# ---------------------------------------------------------------------------
# Auth — bearer token for /ingest
# ---------------------------------------------------------------------------

def get_ingest_token() -> str:
    token = os.environ.get("INGEST_TOKEN")
    if not token:
        raise RuntimeError(
            "INGEST_TOKEN is not set. Generate a long random string and "
            "configure it as an environment variable both here and in "
            "Stubber's credentials."
        )
    return token


def require_ingest_auth(request) -> None:
    """Raise 401 if the request doesn't carry the correct bearer token.

    secrets.compare_digest gives us constant-time comparison so timing
    attacks can't leak the token character-by-character.
    """
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {get_ingest_token()}"
    if not secrets.compare_digest(header, expected):
        log.warning("Rejected /ingest request: bad or missing token")
        abort(401)


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

VALID_STATUSES = {"healthy", "degraded", "down", "error"}
VALID_SEVERITIES = {"critical", "warning", "info"}


def validate_payload(data: dict) -> tuple[dict, list[dict]]:
    """Validate and normalise the incoming check payload.

    Returns (check_row, issue_rows) ready to insert. Raises ValueError on
    anything structurally wrong — Flask converts that to a 400 for us.
    """
    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")

    required = ("agent_name", "template_uuid", "status")
    for key in required:
        if key not in data:
            raise ValueError(f"Missing required field: {key}")

    status = str(data["status"]).lower()
    if status not in VALID_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}"
        )

    check = {
        "agent_name": str(data["agent_name"])[:200],
        "template_uuid": str(data["template_uuid"])[:64],
        "status": status,
        "stubs_checked": int(data.get("stubs_checked", 0)),
        "errors_count": int(data.get("errors_count", 0)),
        "stuck_count": int(data.get("stuck_count", 0)),
        "flagged_count": int(data.get("flagged_count", 0)),
        "agent_live": bool(data.get("agent_live", False)),
        "summary": (str(data.get("summary", ""))[:500]) or None,
        "raw_payload": data,
    }

    raw_issues = data.get("issues") or []
    if not isinstance(raw_issues, list):
        raise ValueError("issues must be a list if provided")

    issues: list[dict] = []
    for i, raw in enumerate(raw_issues):
        if not isinstance(raw, dict):
            raise ValueError(f"issues[{i}] must be an object")
        severity = str(raw.get("severity", "warning")).lower()
        if severity not in VALID_SEVERITIES:
            raise ValueError(
                f"issues[{i}].severity must be one of {sorted(VALID_SEVERITIES)}"
            )
        issues.append({
            "severity": severity,
            "rule": str(raw.get("rule", "unspecified"))[:100],
            "message": str(raw.get("message", ""))[:1000],
            "details": raw.get("details"),
        })

    return check, issues


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def insert_check(check: dict, issues: list[dict]) -> int:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO checks (agent_name, template_uuid, status,
                                stubs_checked, errors_count, stuck_count,
                                flagged_count, agent_live, summary, raw_payload)
            VALUES (%(agent_name)s, %(template_uuid)s, %(status)s,
                    %(stubs_checked)s, %(errors_count)s, %(stuck_count)s,
                    %(flagged_count)s, %(agent_live)s, %(summary)s,
                    %(raw_payload)s)
            RETURNING id
            """,
            {**check, "raw_payload": json.dumps(check["raw_payload"])},
        )
        check_id = cur.fetchone()["id"]
        for issue in issues:
            cur.execute(
                """
                INSERT INTO issues (check_id, severity, rule, message, details)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    check_id,
                    issue["severity"],
                    issue["rule"],
                    issue["message"],
                    json.dumps(issue["details"]) if issue["details"] else None,
                ),
            )
    return check_id


def latest_check_per_agent() -> list[dict]:
    """For each agent, the most recent check. Drives the status cards."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (agent_name) *
            FROM checks
            ORDER BY agent_name, run_at DESC
        """)
        return cur.fetchall()


def recent_checks(limit: int = 30) -> list[dict]:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM checks ORDER BY run_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def unresolved_issues() -> list[dict]:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT i.*, c.agent_name, c.run_at
            FROM issues i
            JOIN checks c ON c.id = i.check_id
            WHERE i.resolved = FALSE
            ORDER BY i.created_at DESC
        """)
        return cur.fetchall()


def weekly_stats() -> dict:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*)                                             AS total_checks,
                SUM(CASE WHEN status='healthy'  THEN 1 ELSE 0 END)   AS healthy_count,
                SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END)   AS degraded_count,
                SUM(CASE WHEN status IN ('down','error') THEN 1 ELSE 0 END) AS down_count,
                COALESCE(SUM(stubs_checked), 0)                      AS total_stubs,
                COALESCE(SUM(errors_count), 0)                       AS total_errors
            FROM checks
            WHERE run_at >= NOW() - INTERVAL '7 days'
        """)
        row = cur.fetchone() or {}
        cur.execute("SELECT COUNT(*) AS n FROM issues WHERE resolved = FALSE")
        row["open_issues"] = cur.fetchone()["n"]
    # Replace Nones from an empty table with 0 so the template doesn't choke.
    return {k: (v if v is not None else 0) for k, v in row.items()}


def mark_issue_resolved(issue_id: int) -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE issues SET resolved = TRUE WHERE id = %s", (issue_id,)
        )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.template_filter("pretty_json")
def pretty_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2)
    try:
        return json.dumps(json.loads(value), indent=2)
    except (ValueError, TypeError):
        return str(value)


@app.template_filter("status_class")
def status_class(status: str) -> str:
    return {
        "healthy": "status-ok",
        "degraded": "status-warn",
        "down": "status-bad",
        "error": "status-bad",
    }.get(status, "status-unknown")


@app.template_filter("format_dt")
def format_dt(value) -> str:
    """Render tz-aware datetimes as 'YYYY-MM-DD HH:MM UTC' — easier to read
    than full ISO-8601 with microseconds."""
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------- Routes ---------

@app.route("/")
def index():
    return render_template(
        "index.html",
        agents=latest_check_per_agent(),
        stats=weekly_stats(),
        history=recent_checks(limit=20),
        open_issues=unresolved_issues(),
    )


@app.route("/healthz")
def healthz():
    """Cheap liveness check for the hosting platform. Does not touch the DB
    so it can't get rate-limited off the free tier."""
    return {"ok": True}, 200


@app.route("/ingest", methods=["POST"])
def ingest():
    """Receives daily check results from Stubber's scheduled template."""
    require_ingest_auth(request)

    if not request.is_json:
        abort(400, "Expected application/json")

    try:
        check, issues = validate_payload(request.get_json())
    except ValueError as e:
        log.warning("Rejected /ingest payload: %s", e)
        return jsonify({"error": str(e)}), 400

    try:
        check_id = insert_check(check, issues)
    except psycopg.Error as e:
        log.error("DB insert failed: %s", e)
        return jsonify({"error": "database error"}), 500

    log.info(
        "Ingested check %d for agent=%s status=%s issues=%d",
        check_id, check["agent_name"], check["status"], len(issues),
    )
    return jsonify({"ok": True, "check_id": check_id}), 201


@app.route("/issues/<int:issue_id>/resolve", methods=["POST"])
def resolve(issue_id: int):
    # Internal-only endpoint. No CSRF token since this dashboard is not
    # intended for hostile users — if you ever expose it publicly with
    # multiple users, add Flask-WTF CSRF.
    if issue_id <= 0:
        abort(400)
    mark_issue_resolved(issue_id)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Gunicorn imports `app` directly; init_db needs to run once at boot.
# Flask 3 removed before_first_request, so we call init_db at import time —
# this is safe because SCHEMA_SQL is idempotent (CREATE TABLE IF NOT EXISTS).
try:
    init_db()
except Exception as e:
    # Don't crash on startup if the DB is briefly unreachable — the first
    # real request will surface the issue via a 500. Crashing here would
    # loop the container on Render.
    log.error("init_db failed at startup: %s (will retry on first request)", e)


if __name__ == "__main__":
    # Local dev only. In production Render runs this via gunicorn.
    app.run(host="127.0.0.1", port=5000, debug=True)
