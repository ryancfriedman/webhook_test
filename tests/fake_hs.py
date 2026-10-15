"""A small fake Hyperswitch on 127.0.0.1 for the confirm tests and the end-to-end web test.
It models ONLY behaviour observed in the sandbox or stated in the docs:
  duplicate payment_id -> 400 HE_01; session_expiry under 60 -> 422 IR_06; a fetch returns client_secret;
  duplicate refund_id -> 400 HE_01; refund over the payment amount -> IR_13; refunds are instant (succeeded).
Fault injection: fake.faults.append("500") makes the next POST fail with 500; fake.refund_status = "pending"
(or "failed", "review") makes the next refunds come back with that status. So these tests check OUR logic."""
import json
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeHS:
    def __init__(self):
        self.payments, self.refunds, self.posts, self.gets = {}, {}, [], []
        self.faults, self.refund_status = deque(), "succeeded"
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _err(self, status, code):
                self._send(status, {"error": {"type": "invalid_request", "code": code, "message": "x"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                path = self.path.split("?")[0]
                with fake.lock:
                    fake.posts.append({"path": path, "body": body})
                    fault = fake.faults.popleft() if fake.faults else None
                    if fault == "500":
                        return self._send(500, {"error": {"code": "HE_00", "message": "boom"}})
                    if path == "/payments":
                        pid = body["payment_id"]
                        if pid in fake.payments:
                            return self._err(400, "HE_01")
                        if body.get("session_expiry", 900) < 60:
                            return self._err(422, "IR_06")
                        exp = datetime.now(timezone.utc) + timedelta(seconds=body.get("session_expiry", 900))
                        fake.payments[pid] = {"payment_id": pid, "amount": body["amount"], "currency": body["currency"],
                                              "status": "requires_payment_method", "manual_retry_allowed": None,
                                              "client_secret": f"{pid}_secret_TOPSECRET",
                                              "expires_on": exp.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
                        return self._send(200, dict(fake.payments[pid]))
                    if path == "/refunds":
                        pay = fake.payments.get(body.get("payment_id"))
                        if pay is None or pay["status"] != "succeeded":
                            return self._err(400, "IR_07")
                        rid = body["refund_id"]
                        if rid in fake.refunds:
                            return self._err(400, "HE_01")
                        already = sum(r["amount"] for r in fake.refunds.values() if r["payment_id"] == pay["payment_id"]
                                      and r["status"] != "failed")
                        if body.get("amount", pay["amount"]) + already > pay["amount"]:
                            return self._err(400, "IR_13")
                        fake.refunds[rid] = {"refund_id": rid, "payment_id": pay["payment_id"],
                                             "amount": body.get("amount", pay["amount"]), "currency": pay["currency"],
                                             "status": fake.refund_status}
                        return self._send(200, dict(fake.refunds[rid]))
                self._err(404, "HE_02")

            def do_GET(self):
                pid = self.path.split("?")[0].rsplit("/", 1)[-1]
                table = fake.refunds if "/refunds/" in self.path else fake.payments
                with fake.lock:
                    fake.gets.append(pid)
                    obj = dict(table[pid]) if pid in table else None
                self._send(200, obj) if obj else self._send(404, {"error": {"message": "No payment found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # --- test helpers: what the buyer's browser would have done at Hyperswitch -----------------
    def set_status(self, payment_id, status, **extra):
        with self.lock:
            self.payments[payment_id].update(status=status, **extra)
