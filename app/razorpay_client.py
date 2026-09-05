"""
Razorpay order creation, real or mock.

Design: if RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are set in the
environment AND the `razorpay` package is installed, every order is a
real Razorpay TEST MODE order (hits Razorpay's sandbox API, returns a
real order_id you can look up in the Razorpay dashboard). If either is
missing, we fall back automatically to a local mock that produces
same-shaped responses (order_id, status, amount, currency) so the rest
of the pipeline — and the demo — never has to know or care which mode
it's in. The audit log always records which mode created the order.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()  # reads a .env file in the current directory into os.environ, if one exists
except ImportError:
    pass  # python-dotenv not installed -- fall back to whatever's already in the real environment

KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")

try:
    import razorpay  # type: ignore
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

LIVE_MODE = bool(KEY_ID and KEY_SECRET and _SDK_AVAILABLE)


@dataclass
class RazorpayOrderResult:
    order_id: str
    status: str
    amount_paise: int
    currency: str
    mode: str  # "live_test_mode" or "mock"


def _client():
    return razorpay.Client(auth=(KEY_ID, KEY_SECRET))


def create_order(amount_paise: int, receipt: str, notes: Optional[dict] = None) -> RazorpayOrderResult:
    """Create a Razorpay order for `amount_paise`. Raises on hard
    failure so the caller can record a rejection rather than silently
    treating a failed order-creation as a success."""
    if LIVE_MODE:
        client = _client()
        order = client.order.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "notes": notes or {},
            }
        )
        return RazorpayOrderResult(
            order_id=order["id"],
            status=order["status"],
            amount_paise=order["amount"],
            currency=order["currency"],
            mode="live_test_mode",
        )

    # --- mock mode ---
    # Deterministic-ish fake order id in Razorpay's own "order_<id>" shape.
    fake_id = f"order_MOCK{secrets.token_hex(7)}"
    time.sleep(0.01)  # simulate network latency so timing-sensitive tests are meaningful
    return RazorpayOrderResult(
        order_id=fake_id,
        status="created",
        amount_paise=amount_paise,
        currency="INR",
        mode="mock",
    )