"""Live-event ticketing prototype (sandbox only). Flask web layer.

Money, seats and timing decisions live in claim.py, payments.py, confirm.py and timing_rules.py (D9-D17).
This file only moves requests to those functions and shows the result. Secrets come from environment
variables and are never printed, logged, or put in a URL. The client secret appears only in the checkout page body.
"""
import hmac
import logging
import os
import secrets
from functools import lru_cache

from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, url_for

import claim
import confirm
import payments
import timing_rules
from hyperswitch import Client

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")

TIMING = timing_rules.load_timing()
timing_rules.validate(TIMING)  # refuses to start on an impossible combination (D10)

CONNECT_TIMEOUT_SECONDS = 15
# docs: the SDK script and the Hyper() call. The sandbox backend URL is NOT stated by the docs page; see README notes.
SDK_URL = os.environ.get("HYPERSWITCH_SDK_URL", "https://beta.hyperswitch.io/v1/HyperLoader.js")


# --- helpers ------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def hs_client():
    return Client.from_env()  # raises RuntimeError naming the variable if the key is missing


def db():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return claim.connect(url)


def money(minor):
    return f"${minor / 100:,.2f}"


def event_time(dt):
    """Show the event time in Pacific time (the demo event is in a Pacific-time venue); UTC if tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%a %b %d, %Y %I:%M %p %Z")
    except Exception:  # noqa: BLE001
        return dt.strftime("%a %b %d, %Y %I:%M %p %Z")


app.jinja_env.filters["money"] = money
app.jinja_env.filters["event_time"] = event_time


@app.after_request
def headers(resp):
    resp.headers["Referrer-Policy"] = "same-origin"  # the order token is in the URL: never send it elsewhere
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if request.path != "/live":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(RuntimeError)
def config_error(exc):
    log.error("configuration error: %s", exc)  # names the variable, never a value
    return render_template("message.html", title="Not available", message="The site is not configured yet."), 503


def read_order(conn, token):
    """Everything the order page shows, with the hold's remaining time from the DATABASE clock."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""
            SELECT o.id, o.status, o.subtotal_minor, o.fee_minor, o.amount_minor, o.payment_id IS NOT NULL,
                   floor(extract(epoch FROM (o.hold_expires_at - now())))::int, e.name, e.venue, e.starts_at
              FROM orders o JOIN events e ON e.id = o.event_id WHERE o.public_token = %s""", (token,))
        row = cur.fetchone()
        if row is None:
            return None
        order = dict(zip(("id", "status", "subtotal", "fee", "amount", "has_payment", "remaining",
                          "event", "venue", "starts_at"), row))
        cur.execute("""SELECT s.label, os.price_minor FROM order_seats os JOIN seats s ON s.id = os.seat_id
                        WHERE os.order_id = %s ORDER BY s.id""", (order["id"],))
        order["seats"] = cur.fetchall()
    return order


def expire_if_due(conn, order):
    """Lazy flip of a held order whose hold has ended. (Seats already count as free by the DB clock.)"""
    if order["status"] == "held" and order["remaining"] <= 0:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("""UPDATE orders SET status = 'expired', updated_at = now()
                            WHERE id = %s AND status = 'held' AND hold_expires_at <= now()""", (order["id"],))
        order["status"] = "expired"


def seat_page(conn, message=None, status=200):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT id, name, venue, starts_at FROM events ORDER BY id LIMIT 1")
        event = cur.fetchone()
        if event is None:
            return render_template("message.html", title="No event", message="No event has been set up."), 200
        cur.execute("""SELECT id, label, price_minor, effective_status FROM seats_effective
                        WHERE event_id = %s ORDER BY id""", (event[0],))
        seats = cur.fetchall()
    rows = {}
    for sid, label, price, st in seats:
        rows.setdefault(label[0], []).append({"id": sid, "label": label, "price": price, "free": st == "available"})
    html = render_template("home.html", event=event, rows=rows, client_token=secrets.token_urlsafe(18),
                           message=message, max_seats=claim.MAX_SEATS)
    return html, status


# --- pages --------------------------------------------------------------------------------------
@app.get("/")
def index():
    conn = db()
    try:
        return seat_page(conn)
    finally:
        conn.close()


@app.post("/claim")
def claim_route():
    token = request.form.get("client_token", "")
    try:
        seat_ids = [int(x) for x in request.form.getlist("seat")]
    except ValueError:
        abort(400)
    if not (8 <= len(token) <= 64):
        abort(400)
    conn = db()
    try:
        try:
            result = claim.claim_seats(conn, client_token=token, event_id=int(request.form.get("event_id", "0") or 0),
                                       seat_ids=seat_ids, hold_seconds=TIMING.hold_seconds)
        except claim.SeatConflict:
            return seat_page(conn, "One of those seats was just taken. Nothing was held. Please pick again.", 409)
        except claim.BadRequest:
            return seat_page(conn, f"Pick between 1 and {claim.MAX_SEATS} seats.", 400)
        return redirect(url_for("order_page", token=result.public_token), code=303)
    finally:
        conn.close()


@app.get("/order/<token>")
def order_page(token):
    conn = db()
    try:
        order = read_order(conn, token)
        if order is None:
            abort(404)
        if order["status"] in ("paid", "refund_pending", "refunded") or (
                order["status"] == "payment_pending" and order["remaining"] <= 0):
            return redirect(url_for("return_page", token=token), code=303)  # the confirm decides what is true
        expire_if_due(conn, order)
        return render_template("order.html", o=order, token=token, notice=NOTICES.get(request.args.get("n", "")))
    finally:
        conn.close()


NOTICES = {
    "HOLD_ENDING": "There is not enough hold time left to start a payment safely. Your seats will be released "
                   "shortly; please pick seats again.",
    "EXPIRED": "Your hold has ended. Please pick seats again.",
    "CANCELLED": "This order was cancelled.",
    "UNAVAILABLE": "The payment service did not answer. Nothing was charged. Please try again in a moment.",
    "REJECTED": "The payment service refused the request. Nothing was charged. Please try again.",
    "IN_PROGRESS": "A payment for this order is already being processed. Check its status below.",
    "NOT_PAYABLE": "This payment can no longer take a card. Your seats stay held until the hold ends; "
                   "then please pick seats again.",
    "MISMATCH": "This order could not be matched with its payment. Nothing was charged by us. Please start over.",
}


@app.post("/order/<token>/pay")
def pay(token):
    publishable = os.environ.get("HYPERSWITCH_PUBLISHABLE_KEY")
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not publishable or not base:
        raise RuntimeError("HYPERSWITCH_PUBLISHABLE_KEY and PUBLIC_BASE_URL must be set")
    client = hs_client()
    conn = db()
    try:
        r = payments.start_payment(conn, client, TIMING, public_token=token,
                                   return_url=f"{base}/order/{token}/return")
        if r.outcome == "NOT_FOUND":
            abort(404)
        if r.outcome == "PAID":
            return redirect(url_for("return_page", token=token), code=303)
        if r.outcome != "READY":
            notice = r.outcome if r.outcome in NOTICES else "REJECTED"
            if r.outcome == "IN_PROGRESS":
                return redirect(url_for("return_page", token=token), code=303)
            return redirect(url_for("order_page", token=token, n=notice), code=303)
        order = read_order(conn, token)
        return render_template("checkout.html", o=order, token=token, client_secret=r.client_secret,
                               publishable_key=publishable, sdk_url=SDK_URL,
                               backend_url=os.environ.get("HYPERSWITCH_SDK_BACKEND_URL"),
                               return_url=f"{base}/order/{token}/return")
    finally:
        conn.close()


@app.get("/order/<token>/return")
def return_page(token):
    # Query values Hyperswitch appends (status, client secret) are ignored on purpose: we ask Hyperswitch ourselves.
    client = hs_client()
    conn = db()
    try:
        result = confirm.confirm_order(conn, client, public_token=token)
        if result.outcome == "NOT_FOUND":
            abort(404)
        order = read_order(conn, token)
        return render_template("result.html", r=result, o=order, token=token)
    finally:
        conn.close()


@app.post("/sweep")
def sweep_route():
    """Re-checks unattended orders. Needs the X-Sweep-Token header to equal SWEEP_TOKEN; off when it is unset."""
    expected = os.environ.get("SWEEP_TOKEN")
    given = request.headers.get("X-Sweep-Token", "")
    if not expected or not hmac.compare_digest(expected.encode(), given.encode()):
        abort(404)
    client = hs_client()
    conn = db()
    try:
        return jsonify(confirm.sweep(conn, client))
    finally:
        conn.close()


# --- from the deployment skeleton (unchanged) ---------------------------------------------------
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
        conn = claim.connect(url)
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("select now()")
                (db_time,) = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        log.error("health: database check failed (%s)", type(exc).__name__)  # class name only
        return jsonify(status="error", detail="database unavailable"), 503
    return jsonify(status="ok", db_time=db_time.isoformat()), 200
