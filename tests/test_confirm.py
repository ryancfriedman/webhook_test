"""Return-page confirm, late success, refunds (D4, D8, D11). SCRATCH database only, direct host.

    export TEST_DATABASE_URL='...'
    python -m unittest tests.test_confirm -v

Uses the fake Hyperswitch in tests/fake_hs.py (no account, no key). Database helpers come from tests/test_payments.py.
"""
import logging
import os
import threading
import unittest

from claim import claim_seats, connect
from confirm import confirm_order, sweep
from hyperswitch import Client
from payments import derive_id, start_payment
from tests import test_payments as tp
from tests.fake_hs import FakeHS
from timing_rules import Timing

PROD = Timing(hold_seconds=600, margin_seconds=30, floor_seconds=120, fixed_secret_seconds=None)
q, reset, new_order, set_remaining = tp.q, tp.reset, tp.new_order, tp.set_remaining
RETURN = "https://example.test/return"


def setUpModule():
    tp.setUpModule()


def tearDownModule():
    tp.tearDownModule()


def other_order(seat_ids=(1, 2, 3), hold=600):
    """A second buyer, with a different client_token."""
    conn = connect(tp.URL)
    try:
        return claim_seats(conn, client_token=f"other-{os.urandom(6).hex()}", event_id=1,
                           seat_ids=list(seat_ids), hold_seconds=hold)
    finally:
        conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        reset()
        self.fake = FakeHS()
        self.client = Client(self.fake.start(), tp.FAKE_KEY, timeout=1.0)

    def tearDown(self):
        self.fake.stop()

    def _conn_call(self, fn, *args, **kw):
        conn = connect(tp.URL)
        try:
            return fn(conn, *args, **kw)
        finally:
            conn.close()

    def pay(self, claim):  # the buyer opens checkout
        r = self._conn_call(start_payment, self.client, PROD, public_token=claim.public_token, return_url=RETURN)
        self.assertEqual(r.outcome, "READY")
        return q("SELECT payment_id FROM orders WHERE id=%s", (claim.order_id,))[0][0]

    def confirm(self, claim, client=None):
        return self._conn_call(confirm_order, client or self.client, public_token=claim.public_token)

    def order(self, claim):
        return q("SELECT status, last_payment_status FROM orders WHERE id=%s", (claim.order_id,))[0]

    def seat_states(self, ids=(1, 2, 3)):
        return q("SELECT status, order_id FROM seats WHERE id = ANY(%s::bigint[]) ORDER BY id", (list(ids),))

    def n_tickets(self):
        return q("SELECT count(*) FROM tickets")[0][0]


class HappyPath(Base):
    def test_succeeded_payment_sells_the_seats_and_issues_one_ticket_per_seat(self):  # test 1
        c = new_order()
        pid = self.pay(c)
        self.fake.set_status(pid, "succeeded")
        r = self.confirm(c)
        self.assertEqual(r.outcome, "PAID")
        self.assertEqual(sorted(label for _, label in r.tickets), ["S1", "S2", "S3"])
        self.assertEqual(len({code for code, _ in r.tickets}), 3)
        self.assertEqual(self.order(c), ("paid", "succeeded"))
        self.assertEqual(self.seat_states(), [("sold", c.order_id)] * 3)
        self.assertIsNotNone(q("SELECT paid_at FROM orders WHERE id=%s", (c.order_id,))[0][0])

    def test_visiting_again_shows_the_same_tickets_and_issues_nothing_new(self):  # double visit
        c = new_order()
        self.fake.set_status(self.pay(c), "succeeded")
        first, second = self.confirm(c), self.confirm(c)
        self.assertEqual((first.outcome, second.outcome), ("PAID", "PAID"))
        self.assertEqual(first.tickets, second.tickets)
        self.assertEqual(self.n_tickets(), 3)
        self.assertEqual(len(self.fake.gets), 1)  # the second visit did not even ask Hyperswitch

    def test_two_simultaneous_confirms_issue_tickets_once(self):  # duplicate delivery
        for _ in range(8):
            reset(); self.fake.payments.clear()
            c = new_order()
            self.fake.set_status(self.pay(c), "succeeded")
            barrier, out = threading.Barrier(2), [None, None]

            def worker(i):
                conn = connect(tp.URL)
                try:
                    barrier.wait(timeout=15)
                    out[i] = confirm_order(conn, self.client, public_token=c.public_token)
                except BaseException as exc:  # noqa: BLE001
                    out[i] = exc
                finally:
                    conn.close()

            ts = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
            [t.start() for t in ts]; [t.join(30) for t in ts]
            self.assertEqual([getattr(r, "outcome", r) for r in out], ["PAID", "PAID"])
            self.assertEqual(out[0].tickets, out[1].tickets)
            self.assertEqual(self.n_tickets(), 3)


class NotPaid(Base):
    def test_unknown_token_and_order_without_a_payment(self):
        c = new_order()
        self.assertEqual(self.confirm(c).outcome, "NO_PAYMENT")
        ghost = type("G", (), {"public_token": "nope"})()
        self.assertEqual(self.confirm(ghost).outcome, "NOT_FOUND")

    def test_processing_is_pending_and_sells_nothing(self):
        c = new_order()
        self.fake.set_status(self.pay(c), "processing")
        self.assertEqual(self.confirm(c).outcome, "PENDING")
        self.assertEqual(self.order(c), ("payment_pending", "processing"))
        self.assertEqual(self.n_tickets(), 0)
        self.assertEqual({s for s, _ in self.seat_states()}, {"held"})

    def test_decline_then_retry_then_success(self):  # test 2
        c = new_order()
        pid = self.pay(c)
        self.fake.set_status(pid, "failed", manual_retry_allowed=True)
        self.assertEqual(self.confirm(c).outcome, "NOT_PAID")
        self.assertEqual(self.order(c), ("payment_pending", "failed"))  # the hold continues
        self.assertEqual(self.pay(c), pid)                               # same payment, served again
        self.fake.set_status(pid, "succeeded")
        self.assertEqual(self.confirm(c).outcome, "PAID")
        self.assertEqual(self.n_tickets(), 3)

    def test_hold_over_and_unpaid_marks_the_order_expired_and_frees_the_seats(self):
        c = new_order()
        pid = self.pay(c)
        self.fake.set_status(pid, "requires_payment_method")
        set_remaining(c.order_id, -5)
        self.assertEqual(self.confirm(c).outcome, "NOT_PAID")
        self.assertEqual(self.order(c)[0], "expired")
        eff = q("SELECT effective_status FROM seats_effective WHERE id IN (1,2,3)")
        self.assertEqual({e for (e,) in eff}, {"available"})

    def test_payment_that_never_reached_hyperswitch_is_not_paid(self):
        c = new_order()
        self.pay(c)
        self.fake.payments.clear()  # the fetch now answers 404
        self.assertEqual(self.confirm(c).outcome, "NOT_PAID")

    def test_hyperswitch_unreachable_changes_nothing(self):
        c = new_order()
        self.fake.set_status(self.pay(c), "succeeded")
        dead = Client("http://127.0.0.1:9", tp.FAKE_KEY, timeout=1.0)
        self.assertEqual(self.confirm(c, dead).outcome, "UNAVAILABLE")
        self.assertEqual(self.order(c)[0], "payment_pending")
        self.assertEqual(self.confirm(c).outcome, "PAID")  # and the next try works


class Tampering(Base):
    def test_amount_that_differs_from_the_order_is_never_paid(self):  # test 11
        c = new_order()
        pid = self.pay(c)
        self.fake.set_status(pid, "succeeded", amount=100)
        self.assertEqual(self.confirm(c).outcome, "MISMATCH")
        self.assertEqual(self.order(c)[0], "payment_pending")
        self.assertEqual(self.n_tickets(), 0)
        self.assertEqual({s for s, _ in self.seat_states()}, {"held"})

    def test_browser_supplied_query_values_are_not_an_input_at_all(self):
        # confirm_order takes only the public token. There is no parameter through which a return-page
        # query string (status, client secret, amount) could reach the decision.
        import inspect
        self.assertEqual(list(inspect.signature(confirm_order).parameters), ["conn", "client", "public_token"])


class LateSuccess(Base):
    def test_late_success_with_the_seats_still_free_is_honoured(self):  # test 9
        c = new_order()
        pid = self.pay(c)
        set_remaining(c.order_id, -5)  # the hold ended, nobody else took the seats
        self.fake.set_status(pid, "succeeded")
        self.assertEqual(self.confirm(c).outcome, "PAID")
        self.assertEqual(self.seat_states(), [("sold", c.order_id)] * 3)

    def test_late_success_after_the_status_was_flipped_to_expired_is_honoured(self):
        c = new_order()
        pid = self.pay(c)
        self.fake.set_status(pid, "requires_payment_method")
        set_remaining(c.order_id, -5)
        self.assertEqual(self.confirm(c).outcome, "NOT_PAID")
        self.assertEqual(self.order(c)[0], "expired")
        self.fake.set_status(pid, "succeeded")
        self.assertEqual(self.confirm(c).outcome, "PAID")

    def test_late_success_with_a_seat_taken_by_another_buyer_is_refunded_in_full(self):  # test 10
        a = new_order()
        pid = self.pay(a)
        set_remaining(a.order_id, -5)
        b = other_order()  # takes over the lapsed seats and holds them
        self.fake.set_status(pid, "succeeded")
        r = self.confirm(a)
        self.assertEqual(r.outcome, "REFUNDED")
        self.assertEqual(self.order(a)[0], "refunded")
        refunds = q("SELECT amount_minor, status, refund_id FROM refunds WHERE order_id=%s", (a.order_id,))
        self.assertEqual([(amt, st) for amt, st, _ in refunds], [(15500, "succeeded")])  # subtotal + fee
        self.assertEqual(len(refunds[0][2]), 30)
        self.assertEqual(self.fake.posts[-1]["body"]["amount"], 15500)
        self.assertEqual(self.n_tickets(), 0)
        self.assertEqual(self.seat_states(), [("held", b.order_id)] * 3)  # B is untouched
        # repeat: no second refund
        self.assertEqual(self.confirm(a).outcome, "REFUNDED")
        self.assertEqual(len([p for p in self.fake.posts if p["path"] == "/refunds"]), 1)

    def test_late_success_with_one_of_three_seats_sold_to_another_refunds_all_of_it(self):
        a = new_order()
        pid = self.pay(a)
        set_remaining(a.order_id, -5)
        b = other_order(seat_ids=(2,))
        q("UPDATE seats SET status='sold', held_until=NULL WHERE id=2")  # B paid
        self.fake.set_status(pid, "succeeded")
        self.assertEqual(self.confirm(a).outcome, "REFUNDED")
        self.assertEqual(self.n_tickets(), 0)  # all-or-nothing: seats 1 and 3 were free but are not sold to A
        self.assertEqual(self.seat_states((1, 3))[0][0], "held")  # unchanged: still A's lapsed holds, claimable by anyone

    def test_a_sold_seat_with_a_live_hold_check(self):
        # a live hold by someone else (not lapsed) also blocks A: A's own hold would have to be lapsed for B to hold
        a = new_order()
        pid = self.pay(a)
        set_remaining(a.order_id, -5)
        other_order()
        self.fake.set_status(pid, "succeeded")
        self.assertEqual(self.confirm(a).outcome, "REFUNDED")


class Refunds(Base):
    def _setup_refund_case(self):
        a = new_order()
        pid = self.pay(a)
        set_remaining(a.order_id, -5)
        other_order()
        self.fake.set_status(pid, "succeeded")
        return a

    def test_pending_refund_stays_refund_pending_then_completes_without_a_second_refund(self):
        a = self._setup_refund_case()
        self.fake.refund_status = "pending"
        self.assertEqual(self.confirm(a).outcome, "REFUND_PENDING")
        self.assertEqual(self.order(a)[0], "refund_pending")
        self.assertEqual(self.confirm(a).outcome, "REFUND_PENDING")  # HE_01 -> fetch -> still pending
        rid = q("SELECT refund_id FROM refunds WHERE order_id=%s", (a.order_id,))[0][0]
        self.fake.refunds[rid]["status"] = "succeeded"
        self.assertEqual(self.confirm(a).outcome, "REFUNDED")
        self.assertEqual(q("SELECT count(*) FROM refunds WHERE order_id=%s", (a.order_id,))[0][0], 1)
        self.assertEqual(len(self.fake.refunds), 1)

    def test_failed_refund_is_retried_under_a_new_refund_id_and_stops_after_three(self):
        a = self._setup_refund_case()
        self.fake.refund_status = "failed"
        for _ in range(5):
            self.assertEqual(self.confirm(a).outcome, "REFUND_PENDING")
        rows = q("SELECT refund_seq, status FROM refunds WHERE order_id=%s ORDER BY refund_seq", (a.order_id,))
        self.assertEqual(rows, [(1, "failed"), (2, "failed"), (3, "failed")])  # then it stops: needs a person
        self.assertEqual(len(self.fake.refunds), 3)
        self.assertEqual(len({r["refund_id"] for r in self.fake.refunds.values()}), 3)

    def test_failed_then_succeeded_on_the_second_attempt(self):
        a = self._setup_refund_case()
        self.fake.refund_status = "failed"
        self.assertEqual(self.confirm(a).outcome, "REFUND_PENDING")
        self.fake.refund_status = "succeeded"
        self.assertEqual(self.confirm(a).outcome, "REFUNDED")
        self.assertEqual(q("SELECT refund_seq, status FROM refunds WHERE order_id=%s ORDER BY 1", (a.order_id,)),
                         [(1, "failed"), (2, "succeeded")])

    def test_server_error_on_the_refund_is_retried_with_the_same_refund_id(self):
        a = self._setup_refund_case()
        self.fake.faults.append("500")
        self.assertEqual(self.confirm(a).outcome, "REFUND_PENDING")
        self.assertEqual(self.confirm(a).outcome, "REFUNDED")
        posts = [p["body"]["refund_id"] for p in self.fake.posts if p["path"] == "/refunds"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(set(posts)), 1)

    def test_two_simultaneous_confirms_make_one_refund(self):
        for _ in range(5):
            reset(); self.fake.payments.clear(); self.fake.refunds.clear()
            a = self._setup_refund_case()
            barrier, out = threading.Barrier(2), [None, None]

            def worker(i):
                conn = connect(tp.URL)
                try:
                    barrier.wait(timeout=15)
                    out[i] = confirm_order(conn, self.client, public_token=a.public_token)
                except BaseException as exc:  # noqa: BLE001
                    out[i] = exc
                finally:
                    conn.close()

            ts = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
            [t.start() for t in ts]; [t.join(30) for t in ts]
            self.assertEqual([getattr(r, "outcome", r) for r in out], ["REFUNDED", "REFUNDED"])
            self.assertEqual(len(self.fake.refunds), 1)
            self.assertEqual(q("SELECT count(*) FROM refunds WHERE order_id=%s", (a.order_id,))[0][0], 1)

    def test_refund_ids_are_30_characters_and_derived(self):
        a = self._setup_refund_case()
        self.confirm(a)
        tag = q("SELECT value FROM app_settings WHERE key='id_tag'")[0][0]
        rid = q("SELECT refund_id FROM refunds WHERE order_id=%s", (a.order_id,))[0][0]
        self.assertEqual(rid, derive_id("ref_", tag, a.order_id, 1))


class Sweep(Base):
    def test_sweep_pays_an_order_nobody_returned_to(self):  # test 8
        c = new_order()
        self.fake.set_status(self.pay(c), "succeeded")  # the buyer paid and closed the tab
        self.assertEqual(sweep(connect(tp.URL), self.client), {"PAID": 1})
        self.assertEqual(self.order(c)[0], "paid")
        self.assertEqual(sweep(connect(tp.URL), self.client), {})  # nothing left to look at

    def test_sweep_finishes_a_pending_refund(self):
        a = new_order()
        pid = self.pay(a)
        set_remaining(a.order_id, -5)
        other_order()
        self.fake.set_status(pid, "succeeded")
        self.fake.refund_status = "pending"
        self.assertEqual(self.confirm(a).outcome, "REFUND_PENDING")
        rid = q("SELECT refund_id FROM refunds WHERE order_id=%s", (a.order_id,))[0][0]
        self.fake.refunds[rid]["status"] = "succeeded"
        self.assertEqual(sweep(connect(tp.URL), self.client), {"REFUNDED": 1})


class NothingSecretIsLogged(Base):
    def test_no_secret_or_key_in_any_log_line(self):
        a = new_order()
        pid = self.pay(a)
        set_remaining(a.order_id, -5)
        other_order()
        self.fake.set_status(pid, "succeeded")
        with self.assertLogs(level=logging.DEBUG) as captured:
            self.confirm(a)
            self.confirm(a)
        text = "\n".join(captured.output)
        self.assertNotIn("TOPSECRET", text)
        self.assertNotIn(tp.FAKE_KEY, text)


if __name__ == "__main__":
    unittest.main()
