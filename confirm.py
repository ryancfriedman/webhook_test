"""Return-page confirm, late success and refunds (decisions D4, D8, D11, D12, D13).

Nothing the browser says is believed. The buyer's return only triggers THIS code, which asks Hyperswitch
for the payment by the payment_id we stored, compares it with the order, and only then changes anything.

  succeeded + seats still ours (or re-claimable)  -> order paid, seats sold, one ticket per seat   (one transaction)
  succeeded + a seat is gone                      -> order refund_pending, refund of the FULL amount (fee included)
  not paid / still processing                     -> nothing is sold; the order is only labelled

Everything is safe to repeat: the order row is locked first (same lock order as the claim and the payment gate),
a finished order is returned as it is, tickets.seat_id is UNIQUE, and the refund_id is derived and reused.
"""
import logging
import secrets
from dataclasses import dataclass

from hyperswitch import HyperswitchError, HyperswitchUnavailable
from payments import derive_id

log = logging.getLogger("confirm")

MAX_REFUND_ATTEMPTS = 3  # refund_seq 1..3: a refund that fails three times needs a person, not a loop

NOT_PAID_STATUSES = ("requires_payment_method", "failed", "cancelled", "expired")


@dataclass(frozen=True)
class Confirmation:
    # PAID            the order is paid; `tickets` has (code, seat label) pairs.
    # PENDING         Hyperswitch says the payment is not finished (processing, 3DS, ...): look again later.
    # NOT_PAID        the payment did not succeed (declined, abandoned, never created): nothing was sold.
    # NO_PAYMENT      the order has no payment yet.
    # MISMATCH        the payment does not match the order (amount, currency or id): nothing was sold.
    # UNAVAILABLE     Hyperswitch could not be asked: nothing changed, look again.
    # REFUNDED        the seats were gone when the payment succeeded; the full amount was refunded.
    # REFUND_PENDING  same, but the refund is not finished (pending, review, failed, unreachable).
    # NOT_FOUND       no such order.   PROBLEM  a state that should be impossible (logged).
    outcome: str
    tickets: tuple = ()


# --- SQL ----------------------------------------------------------------------------------------
ORDER_SQL = """
SELECT id, status, payment_id, amount_minor, currency, hold_expires_at <= now() AS hold_over
  FROM orders WHERE public_token = %(token)s"""

LOCK_ORDER_SQL = "SELECT status, amount_minor FROM orders WHERE id = %(id)s FOR UPDATE"

# Lock this order's seats in id order (same order as the claim: no deadlock). `lapsed` = a hold that has ended.
SEATS_SQL = """
SELECT s.id, s.status, s.order_id, (s.status = 'held' AND s.held_until <= now()) AS lapsed
  FROM order_seats os JOIN seats s ON s.id = os.seat_id
 WHERE os.order_id = %(id)s ORDER BY s.id FOR UPDATE OF s"""

SELL_SQL = """
UPDATE seats SET status = 'sold', order_id = %(id)s, held_until = NULL
 WHERE id = ANY(%(ids)s::bigint[])"""

TICKET_SQL = "INSERT INTO tickets (order_id, seat_id, code) VALUES (%(id)s, %(seat)s, %(code)s)"

PAID_SQL = """
UPDATE orders SET status = 'paid', paid_at = now(), last_payment_status = 'succeeded', last_error = NULL,
                  updated_at = now()
 WHERE id = %(id)s"""

REFUND_PENDING_SQL = """
UPDATE orders SET status = 'refund_pending', last_payment_status = 'succeeded', updated_at = now()
 WHERE id = %(id)s"""

TICKETS_SQL = """
SELECT t.code, s.label FROM tickets t JOIN seats s ON s.id = t.seat_id
 WHERE t.order_id = %(id)s ORDER BY s.id"""

RECORD_SQL = """
UPDATE orders SET last_payment_status = %(status)s, updated_at = now()
 WHERE id = %(id)s AND status IN ('payment_pending', 'expired')"""

EXPIRE_SQL = """
UPDATE orders SET status = 'expired', updated_at = now()
 WHERE id = %(id)s AND status = 'payment_pending' AND hold_expires_at <= now()"""

ERROR_SQL = "UPDATE orders SET last_error = %(err)s, updated_at = now() WHERE id = %(id)s"

TAG_SQL = "SELECT value FROM app_settings WHERE key = 'id_tag'"

REFUND_ROWS_SQL = """
SELECT refund_id, refund_seq, amount_minor, status FROM refunds
 WHERE order_id = %(id)s ORDER BY refund_seq DESC"""

REFUND_INSERT_SQL = """
INSERT INTO refunds (order_id, payment_id, refund_id, refund_seq, amount_minor, status, reason)
VALUES (%(id)s, %(pid)s, %(rid)s, %(seq)s, %(amount)s, 'pending', 'seats_unavailable')"""

REFUND_SET_SQL = "UPDATE refunds SET status = %(status)s WHERE refund_id = %(rid)s"

REFUNDED_SQL = "UPDATE orders SET status = 'refunded', updated_at = now() WHERE id = %(id)s AND status = 'refund_pending'"

SWEEP_SQL = """
SELECT public_token FROM orders
 WHERE (status IN ('payment_pending', 'expired') AND payment_id IS NOT NULL
        AND last_payment_status IS DISTINCT FROM 'succeeded' AND created_at > now() - interval '1 day')
    OR status = 'refund_pending'
 ORDER BY updated_at LIMIT %(limit)s"""


# --- public API ---------------------------------------------------------------------------------
def confirm_order(conn, client, *, public_token) -> Confirmation:
    """`conn` must have no transaction open (same rule as claim_seats and start_payment)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(ORDER_SQL, {"token": public_token})
        row = cur.fetchone()
    if row is None:
        return Confirmation("NOT_FOUND")
    order_id, status, payment_id, amount, currency, hold_over = row

    if status == "paid":
        return Confirmation("PAID", _tickets(conn, order_id))
    if status == "refunded":
        return Confirmation("REFUNDED")
    if status == "refund_pending":
        return _refund(conn, client, order_id)  # finish (or look at) the refund; safe to repeat
    if payment_id is None:
        return Confirmation("NO_PAYMENT")

    try:
        payment = client.retrieve_payment(payment_id)
    except HyperswitchUnavailable:
        return Confirmation("UNAVAILABLE")
    except HyperswitchError as exc:
        if exc.status == 404:  # the create call never reached Hyperswitch: nothing to confirm
            return Confirmation("NOT_PAID")
        _note_error(conn, order_id, exc.code or f"HTTP {exc.status}")
        return Confirmation("UNAVAILABLE")

    if (payment.get("payment_id"), payment.get("amount"), payment.get("currency")) != (payment_id, amount, currency):
        log.error("confirm: payment does not match order=%s", order_id)  # ids/amounts are not logged
        _note_error(conn, order_id, "MISMATCH")
        return Confirmation("MISMATCH")

    pstatus = payment.get("status")
    if pstatus == "succeeded":
        return _finalize(conn, client, order_id)

    label = pstatus if isinstance(pstatus, str) else "unknown"
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(RECORD_SQL, {"id": order_id, "status": label})
        if label in NOT_PAID_STATUSES:
            cur.execute(EXPIRE_SQL, {"id": order_id})  # the hold is over and nothing was paid: label it expired
    return Confirmation("NOT_PAID" if label in NOT_PAID_STATUSES else "PENDING")


def sweep(conn, client, limit=20) -> dict:
    """Re-check orders whose outcome nobody has looked at (no webhooks reach us: D8). Returns outcome counts."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(SWEEP_SQL, {"limit": limit})
        tokens = [r[0] for r in cur.fetchall()]
    counts = {}
    for token in tokens:
        outcome = confirm_order(conn, client, public_token=token).outcome
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


# --- internals ----------------------------------------------------------------------------------
def _tickets(conn, order_id):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(TICKETS_SQL, {"id": order_id})
        return tuple((code, label) for code, label in cur.fetchall())


def _note_error(conn, order_id, err):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(ERROR_SQL, {"id": order_id, "err": err[:100]})


def _finalize(conn, client, order_id) -> Confirmation:
    """The payment SUCCEEDED. One transaction decides: tickets, or a refund. The order row is locked first."""
    needs_refund = False
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(LOCK_ORDER_SQL, {"id": order_id})
        status, amount = cur.fetchone()
        if status == "paid":
            pass  # another request got here first: nothing to do
        elif status in ("refund_pending", "refunded"):
            needs_refund = status == "refund_pending"
        elif status in ("payment_pending", "expired"):
            cur.execute(SEATS_SQL, {"id": order_id})
            seats = cur.fetchall()
            # D11: re-claimable = not sold, and either free, a hold that has ended, or still held by this order.
            ok = bool(seats) and all(
                st == "available" or (st == "held" and (oid == order_id or lapsed)) for _, st, oid, lapsed in seats)
            if ok:
                ids = [sid for sid, *_ in seats]
                cur.execute(SELL_SQL, {"id": order_id, "ids": ids})
                for sid in ids:
                    cur.execute(TICKET_SQL, {"id": order_id, "seat": sid, "code": secrets.token_hex(6).upper()})
                cur.execute(PAID_SQL, {"id": order_id})
            else:
                cur.execute(REFUND_PENDING_SQL, {"id": order_id})
                needs_refund = True
        else:  # held / cancelled with a payment: payment_id is only stored by the gate, so this cannot happen
            log.error("confirm: succeeded payment on an order in an impossible state (order=%s)", order_id)
            return Confirmation("PROBLEM")
    if needs_refund:
        return _refund(conn, client, order_id)
    return Confirmation("PAID", _tickets(conn, order_id))


def _refund(conn, client, order_id) -> Confirmation:
    """Refund the FULL amount (fee included) of a succeeded payment whose seats are gone. Idempotent:
    the refund_id is derived from (order, refund_seq), and a duplicate (HE_01) is fetched, not repeated."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(LOCK_ORDER_SQL, {"id": order_id})
        status, amount = cur.fetchone()
        if status == "refunded":
            return Confirmation("REFUNDED")
        if status != "refund_pending":
            return Confirmation("PROBLEM")
        cur.execute("SELECT payment_id FROM orders WHERE id = %(id)s", {"id": order_id})
        pid = cur.fetchone()[0]
        cur.execute(REFUND_ROWS_SQL, {"id": order_id})
        rows = cur.fetchall()
        live = next((r for r in rows if r[3] != "failed"), None)
        if live is None:
            seq = (rows[0][1] + 1) if rows else 1  # all earlier attempts failed: a NEW refund_id
            if seq > MAX_REFUND_ATTEMPTS:
                log.error("refund: %s attempts failed for order=%s; needs a person", MAX_REFUND_ATTEMPTS, order_id)
                return Confirmation("REFUND_PENDING")
            cur.execute(TAG_SQL)
            rid = derive_id("ref_", cur.fetchone()[0], order_id, seq)
            cur.execute(REFUND_INSERT_SQL, {"id": order_id, "pid": pid, "rid": rid, "seq": seq, "amount": amount})
            rstatus = "pending"
        else:
            rid, _, amount, rstatus = live
    if rstatus == "succeeded":  # the row says so but the order did not move (a crash between the two writes)
        return _record_refund(conn, order_id, rid, "succeeded")

    # The Hyperswitch call runs OUTSIDE any transaction, like the payment call (D17).
    try:
        try:
            resp = client.create_refund({"payment_id": pid, "refund_id": rid, "amount": amount})
        except HyperswitchError as exc:
            if exc.code != "HE_01":
                _note_error(conn, order_id, "refund " + (exc.code or f"HTTP {exc.status}"))
                return Confirmation("REFUND_PENDING")
            resp = client.retrieve_refund(rid)  # it already exists: look at it
    except HyperswitchError as exc:
        _note_error(conn, order_id, "refund " + (exc.code or f"HTTP {exc.status}"))
        return Confirmation("REFUND_PENDING")
    except HyperswitchUnavailable:
        return Confirmation("REFUND_PENDING")  # may or may not have happened: the next visit repeats it safely

    if (resp.get("payment_id"), resp.get("amount")) != (pid, amount):
        log.error("refund: response does not match order=%s", order_id)
        _note_error(conn, order_id, "refund MISMATCH")
        return Confirmation("REFUND_PENDING")
    rstatus = resp.get("status")
    if rstatus not in ("succeeded", "failed", "pending", "review"):
        return Confirmation("REFUND_PENDING")
    return _record_refund(conn, order_id, rid, rstatus)


def _record_refund(conn, order_id, rid, rstatus) -> Confirmation:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(REFUND_SET_SQL, {"status": rstatus, "rid": rid})
        if rstatus == "succeeded":
            cur.execute(REFUNDED_SQL, {"id": order_id})
    return Confirmation("REFUNDED" if rstatus == "succeeded" else "REFUND_PENDING")
