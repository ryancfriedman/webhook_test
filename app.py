"""Deployment skeleton: proves Render + Neon + environment variables work.

No ticketing logic yet. DATABASE_URL is read from the environment and is never
printed, logged, or returned in a response.
"""
import logging
import os

import psycopg
from flask import Flask, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("skeleton")

# Neon's free compute suspends when idle, so the first connection after a quiet
# period can be slow. Allow for a cold start.
CONNECT_TIMEOUT_SECONDS = 15


@app.get("/")
def index():
    return "ticketing skeleton - see /live and /health\n", 200


@app.get("/live")
def live():
    # Touches no database. Use this, never /health, as any platform health check.
    return jsonify(status="ok"), 200


@app.get("/health")
def health():
    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("health: DATABASE_URL is not set")
        return jsonify(status="error", detail="database not configured"), 503
    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            # Safe default if the URL points at a connection pooler.
            conn.prepare_threshold = None
            with conn.cursor() as cur:
                cur.execute("select now()")
                (db_time,) = cur.fetchone()
    except Exception as exc:
        # Class name only: driver messages can contain host and user names.
        log.error("health: database check failed (%s)", type(exc).__name__)
        return jsonify(status="error", detail="database unavailable"), 503
    return jsonify(status="ok", db_time=db_time.isoformat()), 200
