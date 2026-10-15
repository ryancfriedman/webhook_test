"""Create-payment tests (D17). Same rules as tests/test_claim.py: SCRATCH database only, direct host.

    export TEST_DATABASE_URL='...'
    python -m unittest tests.test_payments -v

Needs schema.sql (or migrate_id_tag.sql on an older scratch database). No Hyperswitch account or key is used:
a small fake server on 127.0.0.1 plays Hyperswitch. It models ONLY behaviour observed in the sandbox
(duplicate payment_id -> 400 HE_01, session_expiry under 60 -> 422 IR_06, a fetch returns client_secret),
so these tests check OUR logic, not Hyperswitch's.
"""
import json
import logging
import os
import re
import threading
import time
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claim import claim_seats, connect
from hyperswitch import Client
from payments import PaymentStart, derive_id, start_payment
from timing_rules import Timing

URL = os.environ.get("TEST_DATABASE_URL")
ADMIN = None
PROD = Timing(hold_seconds=600, margin_seconds=30, floor_seconds=120, fixed_secret_seconds=None)
TEST_MODE = Timing(hold_seconds=60, margin_seconds=30, floor_seconds=5, fixed_secret_seconds=900)
FAKE_KEY = "fake-api-key-never-real"


# ---------------------------------------------------------------- fake Hyperswitch ----------
class FakeHyperswitch:
    def __init__(self):
        self.payments, self.posts, self.gets, self.faults = {}, [], [], deque()
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the test output quiet
                pass

            def _send(self, status, obj):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.lock:
                    fake.posts.append({"body": body, "key": self.headers.get("api-key")})
                    fault = fake.faults.popleft() if fake.faults else None
                if fault == "500":
                    return self._send(500, {"error": {"code": "HE_00", "message": "boom"}})
                if fault == "422":
                    return self._send(422, {"error": {"code": "IR_06", "message": "bad"}})
                with fake.lock:
                    pid = body["payment_id"]
                    if pid in fake.payments:
                        return self._send(400, {"error": {"type": "invalid_request", "code": "HE_01",
                                                          "message": "Duplicate payment"}})
                    if body.get("session_expiry", 900) < 60:
                        return self._send(422, {"error": {"code": "IR_06", "message": "too short"}})
                    exp = datetime.now(timezone.utc) + timedelta(seconds=body.get("session_expiry", 900))
                    fake.payments[pid] = {"payment_id": pid, "amount": body["amount"], "currency": body["currency"],
                                          "status": "requires_payment_method", "manual_retry_allowed": None,
                                          "client_secret": f"{pid}_secret_TOPSECRET",
                                          "expires_on": exp.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
                    payment = dict(fake.payments[pid])
                if fault == "process_then_stall":  # the payment WAS created, but the answer is too slow
                    time.sleep(3)
                self._send(200, payment)

            def do_GET(self):
                pid = self.path.split("?")[0].rsplit("/", 1)[-1]
                with fake.lock:
                    fake.gets.append(pid)
                    payment = dict(fake.payments[pid]) if pid in fake.payments else None
                self._send(200, payment) if payment else self._send(404, {"error": {"message": "No payment found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------- database helpers ----------
def q(sql, params=None):
    with ADMIN.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def setUpModule():
    global ADMIN
    if not URL:
        raise RuntimeError("Set TEST_DATABASE_URL to a SCRATCH database.")
    ADMIN = connect(URL)
    ADMIN.autocommit = True
    name = q("SELECT current_database()")[0][0]
    if "test" not in name and "scratch" not in name:
        ADMIN.close()
        raise RuntimeError(f"Refusing to run: database '{name}' is not named test or scratch.")
    if not q("SELECT 1 FROM app_settings WHERE key = 'id_tag'"):
        raise RuntimeError("app_settings/id_tag missing: run migrate_id_tag.sql on this scratch database.")


def tearDownModule():
    if ADMIN is not None:
        ADMIN.close()


def reset():
    q("INSERT INTO events (id, name, starts_at, venue) VALUES (1, 'Test show', now() + interval '30 days', 'Hall') "
      "ON CONFLICT (id) DO NOTHING")
    q("TRUNCATE order_seats, tickets, refunds, orders, seats RESTART IDENTITY CASCADE")
    q("INSERT INTO seats (event_id, label, price_minor) SELECT 1, 'S' || g, 5000 FROM generate_series(1, 40) g")


_n = 0


def new_order(seat_ids=(1, 2, 3), hold=600):
    global _n
    _n += 1
    conn = connect(URL)
    try:
        return claim_seats(conn, client_token=f"pay-test-{os.getpid()}-{_n}", event_id=1,
                           seat_ids=list(seat_ids), hold_seconds=hold)
    finally:
        conn.close()


def order_row(order_id):
    return q("SELECT status, payment_id, last_payment_status, last_error, amount_minor "
             "FROM orders WHERE id = %s", (order_id,))[0]


def set_remaining(order_id, seconds):
    q("UPDATE orders SET hold_expires_at = now() + %s::int * interval '1 second' WHERE id = %s", (seconds, order_id))
    q("UPDATE seats SET held_until = now() + %s::int * interval '1 second' WHERE order_id = %s", (seconds, order_id))


class Base(unittest.TestCase):
    timing = PROD

    def setUp(self):
        reset()
        self.fake = FakeHyperswitch()
        self.client = Client(self.fake.start(), FAKE_KEY, timeout=1.0)

    def tearDown(self):
        self.fake.stop()

    def start(self, claim, client=None, timing=None):
        conn = connect(URL)
        try:
            return start_payment(conn, client or self.client, timing or self.timing,
                                 public_token=claim.public_token, return_url="https://example.test/return")
        finally:
            conn.close()


# ---------------------------------------------------------------- tests ---------------------
class Identifiers(unittest.TestCase):
    def test_derive_id_is_30_characters_and_distinct(self):
        a, b, c = derive_id("pay_", "ab12", 7, 1), derive_id("pay_", "ab12", 8, 1), derive_id("pay_", "ab12", 7, 2)
        self.assertEqual({len(a), len(b), len(c)}, {30})
        self.assertEqual(len({a, b, c}), 3)
        self.assertTrue(a.startswith("pay_ab12"))
        self.assertNotEqual(derive_id("pay_", "zz99", 7, 1), a)  # another database tag, another id

    def test_derive_id_refuses_a_bad_tag_or_range(self):
        for args in (("pay_", "", 1, 1), ("pay_", "AB12", 1, 1), ("pay_", "ab12", 0, 1), ("pay_", "ab12", 1, 100)):
            with self.subTest(args=args), self.assertRaises((RuntimeError, ValueError)):
                derive_id(*args)


class HappyPath(Base):
    def test_gate_commits_then_creates_with_the_derived_id_and_the_d10_secret(self):
        c = new_order()
        r = self.start(c)
        self.assertEqual(r.outcome, "READY")
        self.assertTrue(r.client_secret)
        status, pid, last, err, amount = order_row(c.order_id)
        self.assertEqual((status, last, err, amount), ("payment_pending", "requires_payment_method", None, 15500))
        self.assertEqual(len(pid), 30)
        self.assertTrue(pid.startswith("pay_"))
        sent = self.fake.posts[0]["body"]
        self.assertEqual((sent["payment_id"], sent["amount"], sent["currency"], sent["confirm"]),
                         (pid, 15500, "USD", False))
        self.assertEqual(sent["return_url"], "https://example.test/return")
        # D10: secret = whole seconds of hold left (database clock, as measured at the gate) minus the 30 s margin.
        # The exact relationship is the test. A fixed 569/570 window is wrong on a remote database, where each
        # round trip costs about a second of hold before the gate runs.
        self.assertEqual(sent["session_expiry"], r.remaining_seconds - 30)
        self.assertTrue(500 < r.remaining_seconds <= 600)  # sanity: the hold was nearly the full 600 s
        self.assertEqual(self.fake.posts[0]["key"], FAKE_KEY)

    def test_test_mode_sends_the_fixed_secret(self):
        c = new_order(hold=60)
        r = self.start(c, timing=TEST_MODE)
        self.assertEqual(r.outcome, "READY")
        self.assertEqual(self.fake.posts[0]["body"]["session_expiry"], 900)

    def test_secret_is_not_in_the_repr(self):
        r = self.start(new_order())
        self.assertNotIn("TOPSECRET", repr(r))


class Refusals(Base):
    def test_under_the_floor_is_refused_and_the_order_is_NOT_flipped(self):
        c = new_order()
        set_remaining(c.order_id, 100)  # below the 120 s floor
        r = self.start(c)
        self.assertEqual(r.outcome, "HOLD_ENDING")
        self.assertEqual(order_row(c.order_id)[0], "held")  # still releasable: the buyer is not stuck
        self.assertEqual(self.fake.posts, [])

    def test_just_above_the_floor_is_allowed(self):
        c = new_order()
        set_remaining(c.order_id, 125)  # the gate runs a moment later; 5 s of slack covers a slow remote database
        self.assertEqual(self.start(c).outcome, "READY")

    def test_lapsed_hold_is_refused(self):
        c = new_order()
        set_remaining(c.order_id, -5)
        self.assertEqual(self.start(c).outcome, "EXPIRED")
        self.assertEqual(self.fake.posts, [])

    def test_cancelled_paid_and_unknown_orders_make_no_call(self):
        c = new_order()
        q("UPDATE seats SET status='available', order_id=NULL, held_until=NULL WHERE order_id=%s", (c.order_id,))
        q("UPDATE orders SET status='cancelled' WHERE id=%s", (c.order_id,))
        self.assertEqual(self.start(c).outcome, "CANCELLED")
        q("UPDATE orders SET status='paid' WHERE id=%s", (c.order_id,))
        self.assertEqual(self.start(c).outcome, "PAID")
        ghost = type("G", (), {"public_token": "no-such-token"})()
        self.assertEqual(self.start(ghost).outcome, "NOT_FOUND")
        self.assertEqual(self.fake.posts, [])


class RetriesAndDoubleClicks(Base):
    def test_second_request_reuses_the_payment_via_he01_and_fetch(self):
        c = new_order()
        first, second = self.start(c), self.start(c)
        self.assertEqual((first.outcome, second.outcome), ("READY", "READY"))
        self.assertEqual(first.client_secret, second.client_secret)
        self.assertEqual(len(self.fake.payments), 1)  # one payment, never two
        self.assertEqual(len(self.fake.posts), 2)     # the retry did send the create again, and got HE_01
        self.assertEqual(len(self.fake.gets), 1)      # ... then fetched
        self.assertEqual(self.fake.posts[0]["body"]["payment_id"], self.fake.posts[1]["body"]["payment_id"])

    def test_simultaneous_double_click_makes_one_payment(self):
        for _ in range(10):
            reset()
            self.fake.payments.clear(); self.fake.posts.clear(); self.fake.gets.clear()
            c = new_order()
            barrier, results = threading.Barrier(2), [None, None]

            def worker(i):
                conn = connect(URL)
                try:
                    barrier.wait(timeout=15)
                    results[i] = start_payment(conn, self.client, PROD, public_token=c.public_token,
                                               return_url="https://example.test/return")
                except BaseException as exc:  # noqa: BLE001
                    results[i] = exc
                finally:
                    conn.close()

            ts = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
            [t.start() for t in ts]; [t.join(30) for t in ts]
            self.assertEqual([getattr(r, "outcome", r) for r in results], ["READY", "READY"])
            self.assertEqual(results[0].client_secret, results[1].client_secret)
            self.assertEqual(len(self.fake.payments), 1)
            self.assertEqual(order_row(c.order_id)[0], "payment_pending")

    def test_timeout_after_the_payment_was_created_is_resumed_not_duplicated(self):
        self.fake.faults.append("process_then_stall")
        c = new_order()
        first = self.start(c)
        self.assertEqual(first.outcome, "UNAVAILABLE")
        status, pid, _, err, _ = order_row(c.order_id)
        self.assertEqual(status, "payment_pending")  # NOT reverted: the request may have succeeded
        self.assertEqual(err, "TimeoutError")
        self.assertIn(pid, self.fake.payments)       # and it had
        second = self.start(c)
        self.assertEqual(second.outcome, "READY")
        self.assertEqual(len(self.fake.payments), 1)

    def test_server_error_before_processing_then_retry_creates_it(self):
        self.fake.faults.append("500")
        c = new_order()
        self.assertEqual(self.start(c).outcome, "UNAVAILABLE")
        self.assertEqual(self.fake.payments, {})
        self.assertEqual(self.start(c).outcome, "READY")
        self.assertEqual(len(self.fake.payments), 1)

    def test_a_refusal_is_reported_and_not_retried_inside_the_call(self):
        self.fake.faults.append("422")
        c = new_order()
        self.assertEqual(self.start(c).outcome, "REJECTED")
        self.assertEqual(order_row(c.order_id)[3], "IR_06")
        self.assertEqual(len(self.fake.posts), 1)


class ExistingPaymentCompare(Base):
    def _existing(self, c, **changes):
        """Plant a payment under the id this order will derive, as if an earlier request created it."""
        tag = q("SELECT value FROM app_settings WHERE key='id_tag'")[0][0]
        pid = derive_id("pay_", tag, c.order_id, 1)
        base = {"payment_id": pid, "amount": 15500, "currency": "USD", "status": "requires_payment_method",
                "manual_retry_allowed": None, "client_secret": f"{pid}_secret_TOPSECRET",
                "expires_on": (datetime.now(timezone.utc) + timedelta(seconds=500)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        base.update(changes)
        self.fake.payments[pid] = base

    def test_different_amount_is_a_mismatch_and_no_secret_is_handed_out(self):
        c = new_order()
        self._existing(c, amount=999)
        r = self.start(c)
        self.assertEqual((r.outcome, r.client_secret), ("MISMATCH", None))

    def test_declined_payment_with_retry_allowed_is_served_again(self):
        c = new_order()
        self._existing(c, status="failed", manual_retry_allowed=True)
        r = self.start(c)
        self.assertEqual(r.outcome, "READY")
        self.assertEqual(order_row(c.order_id)[2], "failed")

    def test_declined_payment_without_retry_is_not_payable(self):
        c = new_order()
        self._existing(c, status="failed", manual_retry_allowed=False)
        self.assertEqual(self.start(c).outcome, "NOT_PAYABLE")

    def test_succeeded_or_processing_payment_is_in_progress(self):
        for status in ("succeeded", "processing", "requires_customer_action"):
            with self.subTest(status=status):
                reset(); self.fake.payments.clear()
                c = new_order()
                self._existing(c, status=status)
                r = self.start(c)
                self.assertEqual((r.outcome, r.client_secret), ("IN_PROGRESS", None))

    def test_a_secret_that_has_expired_is_not_served(self):
        c = new_order()
        self._existing(c, expires_on=(datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        self.assertEqual(self.start(c).outcome, "NOT_PAYABLE")


class NothingSecretIsLogged(Base):
    def test_no_secret_or_key_in_any_log_line(self):
        self.fake.faults.append("500")
        c = new_order()
        with self.assertLogs(level=logging.DEBUG) as captured:
            self.start(c)   # an UNAVAILABLE path
            self.start(c)   # the create path
            self.start(c)   # the HE_01 + fetch path
        text = "\n".join(captured.output)
        self.assertNotIn("TOPSECRET", text)
        self.assertNotIn(FAKE_KEY, text)


if __name__ == "__main__":
    unittest.main()
