"""Create-payment slice (decisions D10, D13, D17).

  1. GATE      one short transaction: held -> payment_pending, storing the derived payment_id. It COMMITS first.
  2. CALL      the Hyperswitch request runs OUTSIDE any transaction. No row lock and no connection is held.
  3. RECORD    bookkeeping (last_payment_status / last_error). A failed call never reverts the order, because a
               timed-out request may have succeeded.

A retry (double click, reload, timeout) skips the gate, because the order is already payment_pending,
and sends the SAME payment_id. HE_01 means "that payment already exists": fetch it and compare.
Every checkout open, first or repeat, needs at least `floor` seconds of hold left (D10).
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hyperswitch import HyperswitchError, HyperswitchUnavailable
from timing_rules import secret_seconds

log = logging.getLogger("payments")

ID_LENGTH, SEQ_WIDTH = 30, 2


def derive_id(prefix: str, tag: str, order_id: int, seq: int) -> str:
    """D13: prefix + 4-character database tag + zero-padded order number + seq = exactly 30 characters."""
    if not re.fullmatch(r"[0-9a-z]{4}", tag or ""):
        raise RuntimeError("id_tag is missing or malformed: run migrate_id_tag.sql")
    order_width = ID_LENGTH - len(prefix) - len(tag) - SEQ_WIDTH
    if not (0 < seq < 10 ** SEQ_WIDTH) or not (0 < order_id < 10 ** order_width):
        raise ValueError("order number or seq out of range for a 30-character id")
    out = f"{prefix}{tag}{order_id:0{order_width}d}{seq:0{SEQ_WIDTH}d}"
    assert len(out) == ID_LENGTH
    return out


@dataclass(frozen=True)
class PaymentStart:
    # READY        client_secret is set: mount the payment form.
    # NOT_FOUND / CANCELLED / EXPIRED / PAID   the order cannot be paid.
    # HOLD_ENDING  fewer than `floor` seconds of hold left: ask the buyer to pick seats again.
    # UNAVAILABLE  Hyperswitch did not answer: tell the buyer to try again (same payment_id is reused).
    # REJECTED     Hyperswitch refused the request: do not retry blindly.
    # IN_PROGRESS  the payment is being processed or is finished: the return-page confirm decides.
    # NOT_PAYABLE  the payment cannot take a card any more (dead, or its secret expired).
    # MISMATCH     a payment with our id exists but has a different amount or currency.
    outcome: str
    client_secret: str | None = field(default=None, repr=False)  # repr=False: never printed by accident
    remaining_seconds: int | None = None


@dataclass(frozen=True)
class _Go:
    order_id: int
    payment_id: str
    amount: int
    currency: str
    remaining: int  # whole seconds of hold left, from the DATABASE clock


# --- SQL ----------------------------------------------------------------------------------------
ORDER_SQL = """
SELECT o.id, o.status, o.payment_id, o.payment_seq, o.amount_minor, o.currency,
       floor(extract(epoch FROM (o.hold_expires_at - now())))::int AS remaining,
       (SELECT value FROM app_settings WHERE key = 'id_tag') AS tag
  FROM orders o WHERE o.public_token = %(token)s"""

# The gate. The floor is INSIDE the WHERE: an order with too little hold left must never be flipped to
# payment_pending, because from there release is refused and the buyer would be stuck.
GATE_SQL = """
UPDATE orders SET status = 'payment_pending', payment_id = %(pid)s, updated_at = now()
 WHERE id = %(id)s AND status = 'held' AND payment_seq = %(seq)s
   AND hold_expires_at >= now() + %(floor)s::int * interval '1 second'
RETURNING floor(extract(epoch FROM (hold_expires_at - now())))::int"""

ALIVE_SQL = "SELECT CASE WHEN %(exp)s::timestamptz > now() THEN 1 ELSE 0 END"

RECORD_SQL = """
UPDATE orders SET last_payment_status = %(status)s, last_error = NULL, updated_at = now()
 WHERE id = %(id)s AND status = 'payment_pending'"""

ERROR_SQL = """
UPDATE orders SET last_error = %(err)s, updated_at = now()
 WHERE id = %(id)s AND status = 'payment_pending'"""


def _gate(conn, timing, token):
    """Returns _Go, or a PaymentStart explaining why the order cannot start a payment."""
    with conn.transaction():
        with conn.cursor() as cur:
            for attempt in (1, 2):
                cur.execute(ORDER_SQL, {"token": token})
                row = cur.fetchone()
                if row is None:
                    return PaymentStart("NOT_FOUND")
                order_id, status, payment_id, seq, amount, currency, remaining, tag = row

                if status == "held" and attempt == 1:
                    pid = derive_id("pay_", tag, order_id, seq)
                    cur.execute(GATE_SQL, {"id": order_id, "seq": seq, "pid": pid, "floor": timing.floor_seconds})
                    won = cur.fetchone()
                    if won is not None:
                        return _Go(order_id, pid, amount, currency, won[0])
                    continue  # lost the gate (another request, a release, or too little hold): read again
                break

    if status == "payment_pending":
        if remaining <= 0:
            return PaymentStart("EXPIRED", remaining_seconds=remaining)
        if remaining < timing.floor_seconds:
            return PaymentStart("HOLD_ENDING", remaining_seconds=remaining)
        if payment_id is None:
            raise RuntimeError("payment_pending order without a payment_id")
        return _Go(order_id, payment_id, amount, currency, remaining)  # the retry branch: same payment_id
    if status == "held":  # the gate refused it, so the floor was not met
        return PaymentStart("EXPIRED" if remaining <= 0 else "HOLD_ENDING", remaining_seconds=remaining)
    if status == "cancelled":
        return PaymentStart("CANCELLED")
    if status == "expired":
        return PaymentStart("EXPIRED")
    return PaymentStart("PAID")  # paid, refund_pending, refunded


def start_payment(conn, client, timing, *, public_token, return_url) -> PaymentStart:
    """`conn` must have no transaction open (same rule as claim_seats)."""
    go = _gate(conn, timing, public_token)
    if not isinstance(go, _Go):
        return go

    # The secret's lifetime: the hold left (database clock) minus the margin. This is where D10 is applied.
    secret = secret_seconds(go.remaining, timing)
    if secret is None:  # cannot happen: the gate already enforced the floor. Never guess.
        return PaymentStart("HOLD_ENDING", remaining_seconds=go.remaining)

    body = {"amount": go.amount, "currency": go.currency, "payment_id": go.payment_id,
            "session_expiry": secret, "confirm": False, "return_url": return_url,
            "description": "Ticket order"}
    try:
        try:
            payment = client.create_payment(body)
        except HyperswitchError as exc:
            if exc.code != "HE_01":
                return _fail(conn, go, "REJECTED", exc.code or f"HTTP {exc.status}")
            payment = client.retrieve_payment(go.payment_id)  # HE_01: it already exists, so look at it
    except HyperswitchError as exc:  # the fetch itself was refused
        return _fail(conn, go, "REJECTED", exc.code or f"HTTP {exc.status}")
    except HyperswitchUnavailable as exc:
        return _fail(conn, go, "UNAVAILABLE", str(exc))
    return _finish(conn, go, payment)


def _fail(conn, go, outcome, err):
    log.warning("payment start: order=%s outcome=%s err=%s", go.order_id, outcome, err)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(ERROR_SQL, {"id": go.order_id, "err": err[:100]})
    return PaymentStart(outcome, remaining_seconds=go.remaining)  # the order stays payment_pending


def _finish(conn, go, payment):
    outcome, secret = _evaluate(conn, go, payment)
    log.info("payment start: order=%s outcome=%s", go.order_id, outcome)
    status = payment.get("status")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(RECORD_SQL, {"id": go.order_id, "status": status if isinstance(status, str) else "unknown"})
    return PaymentStart(outcome, client_secret=secret, remaining_seconds=go.remaining)


def _evaluate(conn, go, payment):
    """Compare what Hyperswitch holds with what the order says. Returns (outcome, client_secret or None)."""
    if (payment.get("payment_id"), payment.get("amount"), payment.get("currency")) != (go.payment_id, go.amount, go.currency):
        return "MISMATCH", None

    status = payment.get("status")
    if status == "requires_payment_method" or (status == "failed" and payment.get("manual_retry_allowed") is True):
        pass  # fresh, or declined with a retry allowed: a card can still be entered
    elif status in ("failed", "cancelled", "expired"):
        return "NOT_PAYABLE", None
    else:
        return "IN_PROGRESS", None  # processing, succeeded, 3DS, ...: do not hand out a secret; the return page decides

    secret = payment.get("client_secret")
    if not isinstance(secret, str) or not secret:
        return "NOT_PAYABLE", None

    expires_on = payment.get("expires_on")
    if expires_on is not None:
        try:
            exp = datetime.fromisoformat(str(expires_on))
        except ValueError:
            return "NOT_PAYABLE", None
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)  # assumption: a timestamp without a zone is UTC (unverified)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(ALIVE_SQL, {"exp": exp})
                if cur.fetchone()[0] != 1:  # judged by the DATABASE clock, like every other expiry
                    return "NOT_PAYABLE", None
    return "READY", secret
