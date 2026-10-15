"""Seat claim (decision D9).

The whole claim is ONE transaction:
  price the seats -> insert the order -> lock the seats in id order -> claim them -> verify.
Any failure raises INSIDE the transaction block, so psycopg rolls everything back, the order row too.

The caller must pass a connection with no transaction open. If a query already ran on it, the
`conn.transaction()` block below would only be a savepoint and nothing would be committed.
"""
import secrets
from dataclasses import dataclass

import psycopg

MAX_SEATS = 8  # my guard against absurd requests; change it if you want a different limit


class BadRequest(Exception):
    """Seat ids are empty, repeated, too many, or not seats of this event. Nothing was changed."""


class SeatConflict(Exception):
    """At least one seat could not be claimed. Nothing was changed."""


@dataclass(frozen=True)
class Claim:
    outcome: str  # 'CLAIMED' (new order) or 'EXISTING' (this client_token was already used)
    order_id: int
    public_token: str
    status: str
    subtotal_minor: int
    fee_minor: int
    amount_minor: int


def fee_minor(subtotal_minor: int) -> int:
    """D14: 3% of the subtotal, rounded half up, plus 50 minor units, once per order."""
    return (3 * subtotal_minor + 50) // 100 + 50


def connect(url: str):
    conn = psycopg.connect(url, connect_timeout=15)
    conn.prepare_threshold = None  # same setting as the skeleton: safe behind a pooler
    return conn


# --- SQL. Every value goes in through %(name)s; nothing is formatted into the text. -----------
PRICE_SQL = """
SELECT id, price_minor FROM seats
 WHERE id = ANY(%(ids)s::bigint[]) AND event_id = %(event)s
 ORDER BY id"""

INSERT_ORDER_SQL = """
INSERT INTO orders (public_token, client_token, event_id,
                    subtotal_minor, fee_minor, amount_minor, hold_expires_at)
VALUES (%(public)s, %(client)s, %(event)s,
        %(subtotal)s, %(fee)s, %(amount)s, now() + %(hold)s::int * interval '1 second')
ON CONFLICT (client_token) DO NOTHING
RETURNING id, public_token, status"""

EXISTING_SQL = """
SELECT id, public_token, status, subtotal_minor, fee_minor, amount_minor
  FROM orders WHERE client_token = %(client)s"""

EXISTING_SEATS_SQL = "SELECT seat_id FROM order_seats WHERE order_id = %(order)s ORDER BY seat_id"

# Lock order is part of the design (D9): always ascending seat id, so two buyers can never
# wait on each other in a circle.
LOCK_SQL = """
SELECT id FROM seats
 WHERE id = ANY(%(ids)s::bigint[])
 ORDER BY id FOR UPDATE"""

CLAIM_SQL = """
UPDATE seats
   SET status = 'held', order_id = %(order)s::bigint,
       held_until = now() + %(hold)s::int * interval '1 second'
 WHERE id = ANY(%(ids)s::bigint[]) AND event_id = %(event)s
   AND ( status = 'available'
      OR (status = 'held' AND held_until < now())
      OR (status = 'held' AND order_id = %(order)s::bigint) )
RETURNING id, price_minor"""

SNAPSHOT_SQL = """
INSERT INTO order_seats (order_id, seat_id, price_minor)
SELECT %(order)s::bigint, id, price_minor FROM seats
 WHERE id = ANY(%(ids)s::bigint[]) AND order_id = %(order)s::bigint"""


def _check_input(client_token, event_id, seat_ids, hold_seconds):
    if not isinstance(client_token, str) or not (1 <= len(client_token) <= 100):
        raise BadRequest("client_token must be a string of 1 to 100 characters")
    if not isinstance(event_id, int) or isinstance(event_id, bool):
        raise BadRequest("event_id must be an integer")
    if not isinstance(hold_seconds, int) or isinstance(hold_seconds, bool) or hold_seconds <= 0:
        raise BadRequest("hold_seconds must be a positive integer")
    if not seat_ids or len(seat_ids) > MAX_SEATS:
        raise BadRequest(f"choose between 1 and {MAX_SEATS} seats")
    if any(not isinstance(i, int) or isinstance(i, bool) for i in seat_ids):
        raise BadRequest("seat ids must be integers")
    if len(set(seat_ids)) != len(seat_ids):
        raise BadRequest("a seat was listed twice")


def claim_seats(conn, *, client_token, event_id, seat_ids, hold_seconds):
    _check_input(client_token, event_id, seat_ids, hold_seconds)
    ids = sorted(seat_ids)
    p = {"ids": ids, "event": event_id, "client": client_token, "hold": hold_seconds}

    with conn.transaction():  # commits on normal exit, rolls back on any exception
        with conn.cursor() as cur:
            # 1. Price comes from the database, never from the caller.
            cur.execute(PRICE_SQL, p)
            priced = cur.fetchall()
            if len(priced) != len(ids):
                raise BadRequest("unknown seat, or a seat of another event")
            subtotal = sum(price for _, price in priced)
            fee = fee_minor(subtotal)

            # 2. The order. A repeated client_token (double click) inserts nothing.
            cur.execute(INSERT_ORDER_SQL, {**p, "public": secrets.token_urlsafe(16),
                                           "subtotal": subtotal, "fee": fee, "amount": subtotal + fee})
            row = cur.fetchone()
            if row is None:
                return _existing(cur, client_token, ids)
            order_id, public_token, status = row

            # 3. Lock the seats in id order, then 4. claim them in one conditional UPDATE.
            cur.execute(LOCK_SQL, p)
            cur.fetchall()
            cur.execute(CLAIM_SQL, {**p, "order": order_id})
            claimed = cur.fetchall()

            # 5. Verify: all seats, and the prices we charged for. Raising here rolls back the order too.
            if len(claimed) != len(ids) or sum(price for _, price in claimed) != subtotal:
                raise SeatConflict("one or more seats are no longer available")

            cur.execute(SNAPSHOT_SQL, {**p, "order": order_id})
            return Claim("CLAIMED", order_id, public_token, status, subtotal, fee, subtotal + fee)


def _existing(cur, client_token, ids):
    cur.execute(EXISTING_SQL, {"client": client_token})
    row = cur.fetchone()
    if row is None:  # cannot happen in READ COMMITTED, but never guess
        raise RuntimeError("order vanished after a client_token conflict")
    order_id, public_token, status, subtotal, fee, amount = row
    cur.execute(EXISTING_SEATS_SQL, {"order": order_id})
    if [r[0] for r in cur.fetchall()] != ids:
        raise BadRequest("this client_token was already used for different seats")
    return Claim("EXISTING", order_id, public_token, status, subtotal, fee, amount)
