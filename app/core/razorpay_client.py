# app/core/razorpay_client.py
"""
Razorpay integration.

Security model:
  - Orders are created server-side only; the app never states an amount.
  - Every payment is verified with an HMAC signature check.
  - Each gateway order can be consumed exactly once (`payments.consumed`),
    which blocks replay attacks where a valid payment payload is submitted
    twice to pay for two bookings.
"""

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        logger.error("Razorpay keys not configured")
        return None
    try:
        import razorpay

        _client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
        return _client
    except Exception as exc:  # noqa: BLE001
        logger.exception("Razorpay client init failed: %s", exc)
        return None


def is_configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def create_order(
    amount_rupees: float, receipt: str, notes: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Amount is converted to paise here — the caller always works in rupees."""
    client = _get_client()
    if not client:
        return None
    try:
        return client.order.create(
            {
                "amount": int(round(amount_rupees * 100)),
                "currency": "INR",
                "receipt": receipt[:40],
                "notes": notes or {},
                "payment_capture": 1,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Razorpay order creation failed: %s", exc)
        return None


def verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not settings.RAZORPAY_KEY_SECRET:
        return False
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set — rejecting webhook")
        return False
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def fetch_payment(payment_id: str) -> Optional[Dict[str, Any]]:
    client = _get_client()
    if not client:
        return None
    try:
        return client.payment.fetch(payment_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch payment %s: %s", payment_id, exc)
        return None


def refund(payment_id: str, amount_rupees: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Omit the amount for a full refund."""
    client = _get_client()
    if not client:
        return None
    try:
        payload: Dict[str, Any] = {}
        if amount_rupees is not None:
            payload["amount"] = int(round(amount_rupees * 100))
        return client.payment.refund(payment_id, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refund failed for %s: %s", payment_id, exc)
        return None
