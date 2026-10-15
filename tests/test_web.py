"""End-to-end through the Flask routes, with the fake Hyperswitch (tests/fake_hs.py) and a SCRATCH database.

    export TEST_DATABASE_URL='...'
    python -m unittest tests.test_web -v

This checks the web layer's wiring and what it shows. It does NOT load the real Hyperswitch SDK in a browser:
that is your first manual checkout.
"""
import logging
import os
import re
import unittest

from tests import test_payments as tp
from tests.fake_hs import FakeHS

FAKE = FakeHS()
BASE = FAKE.start()
os.environ.update(DATABASE_URL=tp.URL or "", HYPERSWITCH_SECRET_KEY=tp.FAKE_KEY, HYPERSWITCH_BASE_URL=BASE,
                  HYPERSWITCH_PUBLISHABLE_KEY="pk_snd_fake_publishable", PUBLIC_BASE_URL="https://shop.example.test",
                  SWEEP_TOKEN="sweep-secret-for-tests")
for name in ("HOLD_SECONDS", "SECRET_MARGIN_SECONDS", "CHECKOUT_FLOOR_SECONDS", "TEST_FIXED_SECRET_SECONDS"):
    os.environ.pop(name, None)  # production numbers: 600 / 30 / 120

import app as webapp  # noqa: E402  (after the environment is set)

q, reset, set_remaining = tp.q, tp.reset, tp.set_remaining


def setUpModule():
    tp.setUpModule()


def tearDownModule():
    tp.tearDownModule()
    FAKE.stop()


class Web(unittest.TestCase):
    def setUp(self):
        reset()
        q("UPDATE events SET name='Sample Live Show (demo)' WHERE id=1")
        FAKE.payments.clear(); FAKE.refunds.clear(); FAKE.posts.clear(); FAKE.gets.clear()
        FAKE.refund_status = "succeeded"
        webapp.hs_client.cache_clear()
        self.c = webapp.app.test_client()

    def form_token(self):
        html = self.c.get("/").get_data(as_text=True)
        return re.search(r'name="client_token" value="([^"]+)"', html).group(1)

    def claim(self, seats=(1, 2), token=None, client=None):
        c = client or self.c
        token = token or self.form_token()
        return c.post("/claim", data={"client_token": token, "event_id": "1", "seat": [str(s) for s in seats]})

    def order_url(self, resp):
        self.assertEqual(resp.status_code, 303)
        return resp.headers["Location"]


class Flow(Web):
    def test_home_lists_free_and_taken_seats(self):
        self.claim((1,))
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("Sample Live Show (demo)", html)
        self.assertRegex(html, r'value="1"\s+disabled')       # seat 1 is held
        self.assertNotRegex(html, r'value="2"\s+disabled')    # seat 2 is free

    def test_full_purchase(self):  # test 1, through the web layer
        url = self.order_url(self.claim((1, 2)))
        page = self.c.get(url).get_data(as_text=True)
        self.assertIn("$100.00", page)        # 2 x $50.00 subtotal
        self.assertIn("$3.50", page)          # fee: 3% of 100.00 = 3.00 + 0.50
        self.assertIn("$103.50", page)        # total
        pay = self.c.post(url + "/pay")
        self.assertEqual(pay.status_code, 200)
        body = pay.get_data(as_text=True)
        pid = q("SELECT payment_id FROM orders")[0][0]
        self.assertIn(FAKE.payments[pid]["client_secret"], body)        # the secret is in the page body only
        self.assertEqual(body.count(FAKE.payments[pid]["client_secret"]), 1)
        self.assertIn("pk_snd_fake_publishable", body)
        self.assertNotIn(tp.FAKE_KEY, body)                              # the SECRET api key never reaches the browser
        self.assertNotIn("TOPSECRET", pay.headers.get("Location", "") + str(dict(pay.headers)))
        self.assertEqual(FAKE.posts[0]["body"]["return_url"], url.replace("/order/", "https://shop.example.test/order/") + "/return")
        FAKE.set_status(pid, "succeeded")                                # the buyer paid in the SDK
        done = self.c.get(url + "/return?status=succeeded&payment_intent_client_secret=ignored").get_data(as_text=True)
        self.assertIn("You're going!", done)
        self.assertEqual(len(re.findall(r'class="ticket"', done)), 2)
        self.assertEqual(q("SELECT count(*) FROM tickets")[0][0], 2)

    def test_return_page_ignores_what_the_browser_says(self):  # nobody can flip an order by editing the URL
        url = self.order_url(self.claim((1,)))
        self.c.post(url + "/pay")
        html = self.c.get(url + "/return?status=succeeded&amount=1").get_data(as_text=True)
        self.assertIn("Payment not completed", html)
        self.assertEqual(q("SELECT count(*) FROM tickets")[0][0], 0)
        self.assertEqual(q("SELECT status FROM orders")[0][0], "payment_pending")

    def test_double_click_on_hold_makes_one_order(self):
        token = self.form_token()
        a, b = self.claim((1, 2), token), self.claim((1, 2), token)
        self.assertEqual(self.order_url(a), self.order_url(b))
        self.assertEqual(q("SELECT count(*) FROM orders")[0][0], 1)

    def test_double_click_on_pay_makes_one_payment(self):
        url = self.order_url(self.claim((1,)))
        self.assertEqual(self.c.post(url + "/pay").status_code, 200)
        self.assertEqual(self.c.post(url + "/pay").status_code, 200)
        self.assertEqual(len(FAKE.payments), 1)

    def test_a_taken_seat_gives_a_409_and_holds_nothing(self):
        self.claim((1, 2))
        r = self.claim((2, 3))
        self.assertEqual(r.status_code, 409)
        self.assertIn("just taken", r.get_data(as_text=True))
        self.assertEqual(q("SELECT count(*) FROM orders")[0][0], 1)

    def test_bad_input(self):
        self.assertEqual(self.c.post("/claim", data={"client_token": "x", "seat": "1"}).status_code, 400)
        self.assertEqual(self.c.post("/claim", data={"client_token": "a" * 20, "seat": "abc"}).status_code, 400)
        self.assertEqual(self.claim(()).status_code, 400)
        self.assertEqual(self.c.get("/order/nope").status_code, 404)
        self.assertEqual(self.c.post("/order/nope/pay").status_code, 404)
        self.assertEqual(self.c.get("/order/nope/return").status_code, 404)

    def test_too_little_hold_left_shows_a_message_and_makes_no_payment(self):
        url = self.order_url(self.claim((1,)))
        set_remaining(q("SELECT id FROM orders")[0][0], 60)
        r = self.c.post(url + "/pay")
        self.assertEqual(r.status_code, 303)
        self.assertIn("not enough hold time", self.c.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual(FAKE.payments, {})

    def test_lapsed_hold_page_offers_new_seats(self):
        url = self.order_url(self.claim((1,)))
        set_remaining(q("SELECT id FROM orders")[0][0], -5)
        html = self.c.get(url).get_data(as_text=True)
        self.assertIn("hold has ended", html)
        self.assertEqual(q("SELECT status FROM orders")[0][0], "expired")

    def test_decline_with_manual_retries_on_serves_the_same_payment_and_secret_again(self):
        url = self.order_url(self.claim((1,)))
        first = self.c.post(url + "/pay").get_data(as_text=True)
        pid = q("SELECT payment_id FROM orders")[0][0]
        FAKE.set_status(pid, "failed", manual_retry_allowed=True)
        page = self.c.get(url + "/return").get_data(as_text=True)
        self.assertIn("Payment not completed", page)
        self.assertIn("Try payment again", page)
        again = self.c.post(url + "/pay")
        self.assertEqual(again.status_code, 200)
        secret = FAKE.payments[pid]["client_secret"]
        self.assertIn(secret, first)
        self.assertIn(secret, again.get_data(as_text=True))
        self.assertEqual(len(FAKE.payments), 1)

    def test_decline_with_manual_retries_off_shows_the_dead_end_message(self):  # observed live
        url = self.order_url(self.claim((1,)))
        self.c.post(url + "/pay")
        pid = q("SELECT payment_id FROM orders")[0][0]
        FAKE.set_status(pid, "failed", manual_retry_allowed=False)
        self.assertIn("Payment not completed", self.c.get(url + "/return").get_data(as_text=True))
        r = self.c.post(url + "/pay")
        self.assertEqual(r.status_code, 303)
        self.assertIn("can no longer take a card", self.c.get(r.headers["Location"]).get_data(as_text=True))
        self.assertEqual((q("SELECT status FROM orders")[0][0], len(FAKE.payments)), ("payment_pending", 1))

    def test_late_success_seat_taken_shows_refund(self):  # test 10 through the web layer
        url_a = self.order_url(self.claim((1, 2)))
        self.c.post(url_a + "/pay")
        pid = q("SELECT payment_id FROM orders")[0][0]
        set_remaining(q("SELECT id FROM orders")[0][0], -5)
        other = webapp.app.test_client()
        self.order_url(self.claim((1, 2), client=other))
        FAKE.set_status(pid, "succeeded")
        html = self.c.get(url_a + "/return").get_data(as_text=True)
        self.assertIn("refunded", html.lower())
        self.assertIn("$103.50", html)
        self.assertEqual(q("SELECT status FROM orders WHERE payment_id=%s", (pid,))[0][0], "refunded")


class Plumbing(Web):
    def test_sweep_needs_its_token(self):
        self.assertEqual(self.c.post("/sweep").status_code, 404)
        self.assertEqual(self.c.post("/sweep", headers={"X-Sweep-Token": "wrong"}).status_code, 404)
        r = self.c.post("/sweep", headers={"X-Sweep-Token": "sweep-secret-for-tests"})
        self.assertEqual((r.status_code, r.get_json()), (200, {}))
        self.assertEqual(self.c.get("/sweep").status_code, 405)

    def test_sweep_is_off_when_no_token_is_configured(self):
        saved = os.environ.pop("SWEEP_TOKEN")
        try:
            self.assertEqual(self.c.post("/sweep", headers={"X-Sweep-Token": ""}).status_code, 404)
        finally:
            os.environ["SWEEP_TOKEN"] = saved

    def test_security_headers(self):
        r = self.c.get("/")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_live_and_health(self):
        self.assertEqual(self.c.get("/live").get_json(), {"status": "ok"})
        self.assertEqual(self.c.get("/health").status_code, 200)

    def test_missing_publishable_key_is_a_503_that_names_nothing_secret(self):
        url = self.order_url(self.claim((1,)))
        saved = os.environ.pop("HYPERSWITCH_PUBLISHABLE_KEY")
        try:
            r = self.c.post(url + "/pay")
            self.assertEqual(r.status_code, 503)
            self.assertNotIn(tp.FAKE_KEY, r.get_data(as_text=True))
        finally:
            os.environ["HYPERSWITCH_PUBLISHABLE_KEY"] = saved
        self.assertEqual(FAKE.payments, {})

    def test_no_secret_or_key_in_any_log_line(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            url = self.order_url(self.claim((1,)))
            self.c.post(url + "/pay")
            self.c.post(url + "/pay")
            self.c.get(url + "/return")
        text = "\n".join(captured.output)
        for needle in ("TOPSECRET", tp.FAKE_KEY, os.environ["DATABASE_URL"][:12] if len(os.environ["DATABASE_URL"]) > 12 else "~~"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
