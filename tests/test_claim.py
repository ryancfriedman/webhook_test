"""Claim tests. Run from the repo root against a SCRATCH database (these tests TRUNCATE tables):

    export TEST_DATABASE_URL='...'          # a scratch database, never the demo database
    python -m unittest tests.test_claim -v

Needs schema.sql already loaded. The tests create their own event and seats.
RACE_ROUNDS (default 100) sets how many rounds the race tests run.
"""
import os
import threading
import time
import unittest

import psycopg

from claim import (LOCK_SQL, BadRequest, SeatConflict, claim_seats, connect, fee_minor)

URL = os.environ.get("TEST_DATABASE_URL")
ROUNDS = int(os.environ.get("RACE_ROUNDS", "100"))
HOLD = 600
ADMIN = None  # one autocommit connection, separate from every connection that claims


def setUpModule():
    global ADMIN
    if not URL:
        raise RuntimeError("Set TEST_DATABASE_URL to a SCRATCH database.")
    ADMIN = connect(URL)
    ADMIN.autocommit = True  # every statement commits at once, so other connections see it
    name = q("SELECT current_database()")[0][0]
    if "test" not in name and "scratch" not in name:
        ADMIN.close()
        raise RuntimeError(f"Refusing to run: database '{name}' is not named test or scratch.")


def tearDownModule():
    if ADMIN is not None:
        ADMIN.close()


def q(sql, params=None):
    with ADMIN.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def reset():
    q("INSERT INTO events (id, name, starts_at, venue) "
      "VALUES (1, 'Test show', now() + interval '30 days', 'Hall') ON CONFLICT (id) DO NOTHING")
    q("TRUNCATE order_seats, tickets, refunds, orders, seats RESTART IDENTITY CASCADE")
    q("INSERT INTO seats (event_id, label, price_minor) "
      "SELECT 1, 'S' || g, 5000 FROM generate_series(1, 40) g")


def free_seats(*ids):
    q("UPDATE seats SET status='available', order_id=NULL, held_until=NULL WHERE id = ANY(%s::bigint[])",
      (list(ids),))


_counter = 0
_lock = threading.Lock()


def token():
    global _counter
    with _lock:
        _counter += 1
        return f"tok-{os.getpid()}-{_counter}"


def do_claim(seat_ids, client_token=None, event_id=1, hold=HOLD):
    """One claim on a brand-new connection (so nothing is open on it before the call)."""
    conn = connect(URL)
    try:
        return claim_seats(conn, client_token=client_token or token(), event_id=event_id,
                           seat_ids=seat_ids, hold_seconds=hold)
    finally:
        conn.close()


def race(jobs):
    """Run several claims at the same moment. Each thread opens its OWN connection first,
    then all wait at a barrier, then all claim. Returns each result or the exception it raised."""
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def worker(i, kwargs):
        conn = connect(URL)
        try:
            barrier.wait(timeout=20)
            results[i] = claim_seats(conn, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - the test inspects the type
            results[i] = exc
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i, kw)) for i, kw in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def kw(seat_ids, client_token=None):
    return dict(client_token=client_token or token(), event_id=1, seat_ids=seat_ids, hold_seconds=HOLD)


def count(sql, params=None):
    return q(sql, params)[0][0]


class Basics(unittest.TestCase):
    def setUp(self):
        reset()

    def test_claim_prices_fee_and_commit_visible_from_another_connection(self):
        r = do_claim([1, 2, 3])
        self.assertEqual((r.outcome, r.subtotal_minor, r.fee_minor, r.amount_minor),
                         ("CLAIMED", 15000, 500, 15500))
        # read through the admin connection: proves the claim really committed
        self.assertEqual(count("SELECT count(*) FROM seats WHERE status='held' AND order_id=%s", (r.order_id,)), 3)
        self.assertEqual(q("SELECT subtotal_minor, fee_minor, amount_minor, status FROM orders WHERE id=%s",
                           (r.order_id,))[0], (15000, 500, 15500, "held"))
        self.assertEqual(count("SELECT count(*) FROM order_seats WHERE order_id=%s", (r.order_id,)), 3)

    def test_fee_function_matches_the_decision(self):
        self.assertEqual(fee_minor(15000), 500)
        self.assertEqual(fee_minor(5000), 200)

    def test_order_and_seats_share_one_expiry_from_the_database_clock(self):
        r = do_claim([1, 2])
        self.assertEqual(count("SELECT count(*) FROM seats s JOIN orders o ON o.id = s.order_id "
                               "WHERE o.id=%s AND s.held_until = o.hold_expires_at", (r.order_id,)), 2)
        left = float(q("SELECT extract(epoch FROM (hold_expires_at - now())) FROM orders WHERE id=%s",
                       (r.order_id,))[0][0])
        self.assertTrue(HOLD - 10 < left <= HOLD, left)

    def test_bad_requests_change_nothing(self):
        for ids, event in (([999], 1), ([1, 1], 1), ([], 1), (list(range(1, 10)), 1), ([1, 2], 2)):
            with self.subTest(ids=ids, event=event):
                with self.assertRaises(BadRequest):
                    do_claim(ids, event_id=event)
        self.assertEqual(count("SELECT count(*) FROM orders"), 0)
        self.assertEqual(count("SELECT count(*) FROM seats WHERE status <> 'available'"), 0)

    def test_all_or_nothing(self):
        do_claim([2])
        with self.assertRaises(SeatConflict):
            do_claim([1, 2, 3])
        self.assertEqual(count("SELECT count(*) FROM orders"), 1)  # no orphan order from the failed claim
        self.assertEqual(count("SELECT count(*) FROM seats WHERE id IN (1,3) AND status='available'"), 2)

    def test_live_hold_is_not_taken(self):
        do_claim([1])
        with self.assertRaises(SeatConflict):
            do_claim([1])

    def test_expired_hold_is_taken_over(self):
        a = do_claim([1, 2])
        q("UPDATE seats SET held_until = now() - interval '1 minute' WHERE order_id=%s", (a.order_id,))
        b = do_claim([1, 2])
        self.assertEqual(b.outcome, "CLAIMED")
        self.assertEqual(count("SELECT count(*) FROM seats WHERE id IN (1,2) AND order_id=%s", (b.order_id,)), 2)

    def test_sold_seat_is_never_taken(self):
        do_claim([1])
        q("UPDATE seats SET status='sold', held_until=NULL WHERE id=1")
        with self.assertRaises(SeatConflict):
            do_claim([1, 2])
        self.assertEqual(count("SELECT count(*) FROM seats WHERE id=2 AND status='available'"), 1)

    def test_lock_statement_is_ordered(self):
        # A tripwire, not a proof: it fails if someone deletes the ordering from the lock query.
        self.assertTrue(LOCK_SQL.rstrip().endswith("ORDER BY id FOR UPDATE"))


class DoubleClick(unittest.TestCase):
    def setUp(self):
        reset()

    def test_repeat_with_same_token_returns_the_existing_order(self):
        a = do_claim([1, 2], client_token="dc-1")
        b = do_claim([1, 2], client_token="dc-1")
        self.assertEqual((a.outcome, b.outcome), ("CLAIMED", "EXISTING"))
        self.assertEqual(a.order_id, b.order_id)
        self.assertEqual(count("SELECT count(*) FROM orders"), 1)
        self.assertEqual(count("SELECT count(*) FROM seats WHERE status='held'"), 2)

    def test_same_token_with_different_seats_is_refused(self):
        do_claim([1, 2], client_token="dc-2")
        with self.assertRaises(BadRequest):
            do_claim([3, 4], client_token="dc-2")
        self.assertEqual(count("SELECT count(*) FROM seats WHERE id IN (3,4) AND status='available'"), 2)

    def test_simultaneous_double_click_makes_one_order(self):
        for i in range(20):
            reset()
            t = f"dc-sim-{i}"
            results = race([kw([1, 2], t), kw([1, 2], t)])
            outcomes = sorted(r.outcome if hasattr(r, "outcome") else repr(r) for r in results)
            self.assertEqual(outcomes, ["CLAIMED", "EXISTING"], results)
            self.assertEqual(count("SELECT count(*) FROM orders"), 1)


class Races(unittest.TestCase):
    def setUp(self):
        reset()

    def _one_winner(self, results):
        won = [r for r in results if hasattr(r, "outcome")]
        lost = [r for r in results if isinstance(r, SeatConflict)]
        other = [r for r in results if r not in won and r not in lost]
        self.assertEqual((len(won), len(lost), other), (1, 1, []), results)

    def test_two_buyers_one_seat_many_rounds(self):
        for _ in range(ROUNDS):
            free_seats(1)
            self._one_winner(race([kw([1]), kw([1])]))
        self.assertEqual(count("SELECT count(*) FROM seats s WHERE s.status='held' AND NOT EXISTS "
                               "(SELECT 1 FROM order_seats os WHERE os.order_id=s.order_id AND os.seat_id=s.id)"), 0)

    def test_opposite_listing_order_never_deadlocks(self):
        # The claim sorts ids itself, so listing order must not matter.
        for _ in range(max(10, ROUNDS // 2)):
            free_seats(1, 2, 3)
            self._one_winner(race([kw([1, 2, 3]), kw([3, 2, 1])]))

    def _hold_lock_then_claim(self, finish):
        """Thread A keeps a raw FOR UPDATE on seat 5. The real claim for seat 5 must WAIT.
        Then A either takes the seat and commits, or rolls back."""
        a = connect(URL)
        out = []
        try:
            a.execute("SELECT id FROM seats WHERE id = 5 FOR UPDATE")

            def b():
                try:
                    out.append(do_claim([5]))
                except BaseException as exc:  # noqa: BLE001
                    out.append(exc)

            t = threading.Thread(target=b)
            t.start()
            deadline = time.time() + 10
            while count("SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()") < 1:
                self.assertLess(time.time(), deadline, "the claim never started waiting on the lock")
                time.sleep(0.05)
            self.assertTrue(t.is_alive() and not out, "the claim should still be waiting")
            finish(a)
            t.join(timeout=15)
            self.assertFalse(t.is_alive())
            return out[0]
        finally:
            a.close()

    def test_waiting_claim_conflicts_after_other_buyer_commits(self):
        def take_and_commit(a):
            sub = 5000
            order_id = a.execute(
                "INSERT INTO orders (public_token, client_token, event_id, subtotal_minor, fee_minor, "
                "amount_minor, hold_expires_at) VALUES ('tokA', 'ctA', 1, %s, %s, %s, now() + interval '10 minutes') "
                "RETURNING id", (sub, fee_minor(sub), sub + fee_minor(sub))).fetchone()[0]
            a.execute("UPDATE seats SET status='held', order_id=%s, held_until=now() + interval '10 minutes' "
                      "WHERE id=5", (order_id,))
            a.commit()
        result = self._hold_lock_then_claim(take_and_commit)
        self.assertIsInstance(result, SeatConflict, result)
        self.assertEqual(count("SELECT count(*) FROM orders"), 1)  # only A's order; B's was rolled back

    def test_waiting_claim_succeeds_after_other_buyer_rolls_back(self):
        result = self._hold_lock_then_claim(lambda a: a.rollback())
        self.assertEqual(getattr(result, "outcome", result), "CLAIMED")


if __name__ == "__main__":
    unittest.main()
