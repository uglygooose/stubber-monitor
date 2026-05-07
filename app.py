"""Stubber agent monitor — web service v0.4 (fleet shape, Way B).

Architecture:
    Stubber (daily scheduled template per agent)
        ↓ POST /checks  (bearer token auth)
    This service (Render free tier)
        ↓ writes to
    Supabase Postgres
        ↑ reads from
    Dashboard at /

Endpoints:
    GET  /              dashboard UI (renders all agents + 14-day strips)
    GET  /healthz       liveness check, no auth, no DB
    POST /agents        register an agent (one-time per agent), bearer auth
    GET  /agents        list registered agents
    POST /checks        daily check from Stubber, bearer auth
    GET  /api/fleet     JSON of fleet state (used by the dashboard)

Auth: bearer token in Authorization header for all writes.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from flask import Flask, abort, jsonify, render_template, request
from psycopg.rows import dict_row


# ── logging ─────────────────────────────────────────────────────────────
log = logging.getLogger("monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

SAST = ZoneInfo("Africa/Johannesburg")


# ── DB ──────────────────────────────────────────────────────────────────
def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. In Render, paste the Supabase Session "
            "pooler connection string with the password substituted in."
        )
    return url


def db_connect():
    """Fresh connection per request — low volume, no pool needed."""
    return psycopg.connect(get_db_url(), row_factory=dict_row)


# ── auth ────────────────────────────────────────────────────────────────
def require_bearer(req) -> None:
    expected = os.environ.get("INGEST_TOKEN")
    if not expected:
        abort(500, "INGEST_TOKEN not configured on the server")
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401, "Missing bearer token")
    presented = auth[len("Bearer "):].strip()
    # constant-time compare
    if not _consteq(presented, expected):
        abort(401, "Bad bearer token")


def _consteq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    out = 0
    for x, y in zip(a, b):
        out |= ord(x) ^ ord(y)
    return out == 0


# ── payload validation ──────────────────────────────────────────────────
VALID_VERDICTS = {"healthy", "degraded", "down"}
VALID_SEVERITIES = {"info", "warn", "error"}


def validate_agent(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("agent_id (string) is required")
    # slug-ish: lowercase, alnum, hyphen
    if not all(c.isalnum() or c == "-" for c in agent_id):
        raise ValueError("agent_id must be alphanumeric with hyphens (e.g. 'rep-stock-manager')")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name (string) is required")

    return {
        "agent_id": agent_id.strip().lower(),
        "name": name.strip(),
        "description": (payload.get("description") or "").strip() or None,
        "model": (payload.get("model") or "").strip() or None,
        "template_uuid": (payload.get("template_uuid") or "").strip() or None,
        "deployed_at": payload.get("deployed_at"),  # 'YYYY-MM-DD' or None
    }


def validate_check(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("agent_id (string) is required")

    verdict = payload.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}, got {verdict!r}")

    checked_at = payload.get("checked_at")
    if checked_at:
        try:
            # accept ISO with or without timezone; default to SAST
            dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SAST)
        except (ValueError, AttributeError):
            raise ValueError(f"checked_at must be ISO 8601 (got {checked_at!r})")
    else:
        dt = datetime.now(SAST)

    issues = payload.get("issues") or []
    if not isinstance(issues, list):
        raise ValueError("issues must be a list")
    cleaned_issues = []
    for i, iss in enumerate(issues):
        if not isinstance(iss, dict):
            raise ValueError(f"issues[{i}] must be an object")
        sev = iss.get("severity")
        msg = iss.get("message")
        if sev not in VALID_SEVERITIES:
            raise ValueError(f"issues[{i}].severity must be one of {sorted(VALID_SEVERITIES)}")
        if not isinstance(msg, str) or not msg.strip():
            raise ValueError(f"issues[{i}].message (string) is required")
        cleaned_issues.append({"severity": sev, "message": msg.strip()})

    def _intornone(v):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError(f"expected integer or null, got {v!r}")

    return {
        "agent_id": agent_id.strip().lower(),
        "checked_at": dt,
        "verdict": verdict,
        "stubs_total": _intornone(payload.get("stubs_total")),
        "stubs_flagged": _intornone(payload.get("stubs_flagged")),
        "issues": cleaned_issues,
        "raw": payload,
    }


# ── DB ops ──────────────────────────────────────────────────────────────
def upsert_agent(a: dict) -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agents (agent_id, name, description, model, template_uuid, deployed_at)
            VALUES (%(agent_id)s, %(name)s, %(description)s, %(model)s, %(template_uuid)s, %(deployed_at)s)
            ON CONFLICT (agent_id) DO UPDATE SET
                name          = EXCLUDED.name,
                description   = EXCLUDED.description,
                model         = EXCLUDED.model,
                template_uuid = EXCLUDED.template_uuid,
                deployed_at   = EXCLUDED.deployed_at,
                updated_at    = NOW()
            """,
            a,
        )


def upsert_check(c: dict) -> int:
    """Insert a check, or update if one already exists for the same agent + day."""
    with db_connect() as conn, conn.cursor() as cur:
        # Verify agent exists first — gives a clearer error than a FK violation
        cur.execute("SELECT 1 FROM agents WHERE agent_id = %s", (c["agent_id"],))
        if not cur.fetchone():
            raise ValueError(
                f"agent_id '{c['agent_id']}' not registered. "
                f"POST to /agents first to register the agent."
            )

        cur.execute(
            """
            INSERT INTO checks
                (agent_id, checked_at, verdict, stubs_total, stubs_flagged, issues, raw)
            VALUES
                (%(agent_id)s, %(checked_at)s, %(verdict)s, %(stubs_total)s,
                 %(stubs_flagged)s, %(issues)s, %(raw)s)
            ON CONFLICT (agent_id, (date_trunc('day', checked_at AT TIME ZONE 'Africa/Johannesburg')))
            DO UPDATE SET
                checked_at    = EXCLUDED.checked_at,
                received_at   = NOW(),
                verdict       = EXCLUDED.verdict,
                stubs_total   = EXCLUDED.stubs_total,
                stubs_flagged = EXCLUDED.stubs_flagged,
                issues        = EXCLUDED.issues,
                raw           = EXCLUDED.raw
            RETURNING id
            """,
            {
                **c,
                "issues": json.dumps(c["issues"]),
                "raw": json.dumps(c["raw"]),
            },
        )
        return cur.fetchone()["id"]


def list_agents() -> list[dict]:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, name, description, model, template_uuid, deployed_at "
            "FROM agents ORDER BY name"
        )
        return list(cur.fetchall())


def fleet_data(days: int = 14) -> list[dict]:
    """Return all agents with their last `days` checks, oldest day first.

    Days with no check are filled with a 'missing' placeholder so the
    front-end can render the strip without computing gaps.
    """
    cutoff = (datetime.now(SAST).date() - timedelta(days=days - 1))

    with db_connect() as conn, conn.cursor() as cur:
        agents = list(cur.execute(
            "SELECT agent_id, name, description, model, template_uuid, deployed_at "
            "FROM agents ORDER BY name"
        ).fetchall())

        if not agents:
            return []

        cur.execute(
            """
            SELECT
                agent_id,
                (checked_at AT TIME ZONE 'Africa/Johannesburg')::date AS day,
                checked_at,
                verdict,
                stubs_total,
                stubs_flagged,
                issues,
                raw
            FROM checks
            WHERE (checked_at AT TIME ZONE 'Africa/Johannesburg')::date >= %s
            ORDER BY agent_id, day
            """,
            (cutoff,),
        )
        rows_by_agent: dict[str, dict] = {}
        for r in cur.fetchall():
            rows_by_agent.setdefault(r["agent_id"], {})[r["day"]] = r

    today = datetime.now(SAST).date()
    out = []
    for a in agents:
        agent_checks = rows_by_agent.get(a["agent_id"], {})
        series = []
        for i in range(days):
            d = today - timedelta(days=(days - 1 - i))
            row = agent_checks.get(d)
            if row:
                # Pull metrics out of raw (the Stubber template puts it there).
                # Tolerant: if raw is null or shape is wrong, metrics is null.
                raw = row.get("raw") or {}
                metrics = raw.get("metrics") if isinstance(raw, dict) else None
                series.append({
                    "day_offset": (today - d).days,
                    "date": d.isoformat(),
                    "verdict": row["verdict"],
                    "stubs_total": row["stubs_total"],
                    "stubs_flagged": row["stubs_flagged"],
                    "checked_at": row["checked_at"].isoformat(),
                    "issues": row["issues"] or [],
                    "metrics": metrics,
                })
            else:
                series.append({
                    "day_offset": (today - d).days,
                    "date": d.isoformat(),
                    "verdict": "missing",
                    "stubs_total": None,
                    "stubs_flagged": None,
                    "checked_at": None,
                    "issues": [],
                    "metrics": None,
                })

        out.append({
            "agent_id": a["agent_id"],
            "name": a["name"],
            "description": a["description"],
            "model": a["model"],
            "template_uuid": a["template_uuid"],
            "deployed_at": a["deployed_at"].isoformat() if a["deployed_at"] else None,
            "checks": series,  # oldest first; series[-1] is today
        })
    return out


# ── schema bootstrap (idempotent — no-op after migrate.sql ran) ─────────
def init_db() -> None:
    """If migrate.sql hasn't run yet, this prints a clear hint instead of crashing."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS ("
            "  SELECT FROM information_schema.tables "
            "  WHERE table_name = 'agents'"
            ")"
        )
        if not cur.fetchone()["exists"]:
            log.error(
                "Tables don't exist yet. Run migrate.sql in the Supabase SQL editor first."
            )


# ── Flask ───────────────────────────────────────────────────────────────
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return {"ok": True}, 200


@app.route("/agents", methods=["POST"])
def register_agent():
    require_bearer(request)
    if not request.is_json:
        abort(400, "Expected application/json")
    try:
        agent = validate_agent(request.get_json())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        upsert_agent(agent)
    except psycopg.Error as e:
        log.error("DB error in /agents: %s", e)
        return jsonify({"error": "database error"}), 500
    log.info("Registered agent %s", agent["agent_id"])
    return jsonify({"ok": True, "agent_id": agent["agent_id"]}), 201


@app.route("/agents", methods=["GET"])
def get_agents():
    try:
        agents = list_agents()
    except psycopg.Error as e:
        log.error("DB error listing agents: %s", e)
        return jsonify({"error": "database error"}), 500
    # Convert dates to ISO strings for JSON
    for a in agents:
        if a.get("deployed_at"):
            a["deployed_at"] = a["deployed_at"].isoformat()
    return jsonify({"agents": agents})


@app.route("/checks", methods=["POST"])
def post_check():
    require_bearer(request)
    if not request.is_json:
        abort(400, "Expected application/json")
    try:
        check = validate_check(request.get_json())
    except ValueError as e:
        log.warning("Rejected /checks: %s", e)
        return jsonify({"error": str(e)}), 400
    try:
        check_id = upsert_check(check)
    except ValueError as e:
        # agent not registered
        return jsonify({"error": str(e)}), 400
    except psycopg.Error as e:
        log.error("DB error in /checks: %s", e)
        return jsonify({"error": "database error"}), 500
    log.info(
        "Check %d agent=%s verdict=%s stubs=%s flagged=%s issues=%d",
        check_id, check["agent_id"], check["verdict"],
        check["stubs_total"], check["stubs_flagged"], len(check["issues"]),
    )
    return jsonify({"ok": True, "check_id": check_id}), 201


@app.route("/api/fleet")
def api_fleet():
    try:
        data = fleet_data()
    except psycopg.Error as e:
        log.error("DB error in /api/fleet: %s", e)
        return jsonify({"error": "database error"}), 500
    return jsonify({"agents": data, "now_sast": datetime.now(SAST).isoformat()})


@app.route("/")
def index():
    """Renders the shell; the page fetches /api/fleet and renders client-side.
    This keeps the template simple and lets the same v4 fleet UI we built
    in the preview drop in with minor changes."""
    return render_template("index.html")


# ── boot ────────────────────────────────────────────────────────────────
try:
    init_db()
except Exception as e:
    log.error("init_db check failed (will surface on first request): %s", e)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
