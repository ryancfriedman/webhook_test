"""Hold / client-secret arithmetic (decision D10). Pure functions: no database, no network.

Production: secret = whole seconds left on the hold, minus a margin.
Checkout is refused when fewer than `floor` seconds are left.
Test mode (TEST_FIXED_SECRET_SECONDS set): the secret is a fixed value and the floor is small.
"""
import math
import os
from dataclasses import dataclass

API_MIN_SECRET_SECONDS = 60  # observed in the sandbox: session_expiry 30 -> 422 IR_06


@dataclass(frozen=True)
class Timing:
    hold_seconds: int
    margin_seconds: int
    floor_seconds: int
    fixed_secret_seconds: int | None  # test mode only; None in production


def _read_int(env, name, default):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None  # never echo the value


def load_timing(env=os.environ) -> Timing:
    t = Timing(
        hold_seconds=_read_int(env, "HOLD_SECONDS", 600),
        margin_seconds=_read_int(env, "SECRET_MARGIN_SECONDS", 30),
        floor_seconds=_read_int(env, "CHECKOUT_FLOOR_SECONDS", 120),
        fixed_secret_seconds=_read_int(env, "TEST_FIXED_SECRET_SECONDS", None),
    )
    validate(t)
    return t


def validate(t: Timing) -> None:
    """Refuse a configuration that could never work, so it fails at startup, not at checkout."""
    if t.hold_seconds <= 0 or t.margin_seconds < 0 or t.floor_seconds < 0:
        raise ValueError("hold must be positive; margin and floor must not be negative")
    if t.floor_seconds >= t.hold_seconds:
        raise ValueError("floor must be below the hold, or no payment could ever start")
    if t.fixed_secret_seconds is None:
        if t.floor_seconds < t.margin_seconds + API_MIN_SECRET_SECONDS:
            raise ValueError("floor must be at least margin + 60 (the API minimum)")
    elif t.fixed_secret_seconds < API_MIN_SECRET_SECONDS:
        raise ValueError("fixed test secret must be at least 60 (the API minimum)")


def whole_seconds(remaining) -> int:
    """Round DOWN: 119.9 s left counts as 119, never 120."""
    return math.floor(remaining)


def secret_seconds(remaining_seconds: int, t: Timing) -> int | None:
    """session_expiry to send, or None meaning 'refuse to open checkout'."""
    if remaining_seconds <= 0 or remaining_seconds < t.floor_seconds:
        return None
    if t.fixed_secret_seconds is not None:
        return t.fixed_secret_seconds
    secret = remaining_seconds - t.margin_seconds
    if secret < API_MIN_SECRET_SECONDS:  # unreachable once validate() passed; kept as a second guard
        return None
    return secret
