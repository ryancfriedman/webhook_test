"""Minimal Hyperswitch client (standard library only: no new dependency).

Two kinds of failure, kept apart on purpose:
  HyperswitchError        a 4xx answer: Hyperswitch received the request and REFUSED it (code e.g. 'HE_01').
  HyperswitchUnavailable  timeout, connection failure or 5xx: we do NOT know whether it was processed.

Bodies are never logged and never put in an exception: they can contain the client secret.
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("hyperswitch")

DEFAULT_BASE_URL = "https://sandbox.hyperswitch.io"  # docs: sandbox base URL


class HyperswitchError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(f"HTTP {status} code={code or '-'}")  # status and code only, never the body


class HyperswitchUnavailable(Exception):
    pass


def _error_code(raw: bytes):
    """Read the error code from a refusal. The documented body shape is not certain, so look in both places."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    inner = body.get("error") if isinstance(body.get("error"), dict) else body
    code = inner.get("code")
    return code if isinstance(code, str) else None


class Client:
    def __init__(self, base_url, api_key, timeout=10):
        self.base_url, self._api_key, self.timeout = base_url.rstrip("/"), api_key, timeout

    @classmethod
    def from_env(cls, env=os.environ):
        key = env.get("HYPERSWITCH_SECRET_KEY")
        if not key:
            raise RuntimeError("HYPERSWITCH_SECRET_KEY is not set")  # names the variable, never a value
        return cls(env.get("HYPERSWITCH_BASE_URL", DEFAULT_BASE_URL), key)

    def _request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
            "api-key": self._api_key,  # docs example: --header 'api-key: <your_api_key>'
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:  # an answer with an error status
            code = _error_code(exc.read())
            log.warning("hyperswitch %s %s -> HTTP %s code=%s", method, path.split("?")[0], exc.code, code)
            if exc.code >= 500:
                raise HyperswitchUnavailable(f"HTTP {exc.code}") from None
            raise HyperswitchError(exc.code, code) from None
        except (urllib.error.URLError, OSError) as exc:  # timeout, refused, DNS, reset
            log.warning("hyperswitch %s %s -> %s", method, path.split("?")[0], type(exc).__name__)
            raise HyperswitchUnavailable(type(exc).__name__) from None
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise HyperswitchUnavailable("unreadable response") from None
        if not isinstance(parsed, dict):
            raise HyperswitchUnavailable("unexpected response shape")
        log.info("hyperswitch %s %s -> HTTP %s", method, path.split("?")[0], status)
        return parsed

    def create_payment(self, body: dict) -> dict:
        return self._request("POST", "/payments", body)

    def retrieve_payment(self, payment_id: str) -> dict:
        # expand_attempts=true is the exact call whose response was observed to include client_secret (S2).
        return self._request("GET", f"/payments/{urllib.parse.quote(payment_id, safe='')}?expand_attempts=true")

    def create_refund(self, body: dict) -> dict:
        # docs: POST /refunds; only payment_id is required; refund_id (30 chars) and amount are optional.
        return self._request("POST", "/refunds", body)

    def retrieve_refund(self, refund_id: str) -> dict:
        # docs: GET /refunds/{refund_id}
        return self._request("GET", f"/refunds/{urllib.parse.quote(refund_id, safe='')}")
