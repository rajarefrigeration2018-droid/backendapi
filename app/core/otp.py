# app/core/otp.py
"""
OTP generation, delivery and verification.

FALLBACK ONLY. Real OTP delivery is done by Firebase Phone Auth in the
Flutter apps; the backend just verifies the resulting Firebase ID token
(see app/core/firebase.py). Keep this module for local testing without
a Firebase project.

Security:
  - OTPs are stored hashed
  - max attempts enforced
  - rate limit per phone
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from app.config import settings
from app.core.security import generate_otp, hash_otp, verify_otp
from app.database import db

logger = logging.getLogger(__name__)

# how many OTP requests allowed per phone in the window
RATE_LIMIT_COUNT = 5
RATE_LIMIT_WINDOW_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 30


# ------------------------------------------------------------------
# Sending
# ------------------------------------------------------------------
def _deliver(phone: str, otp: str) -> bool:
    """
    NOTE: Real OTP delivery is handled by Firebase Phone Auth on the client.
    This backend OTP path exists only as a testing fallback and always
    runs in "mock" mode — the code is printed to the server log.
    """
    logger.warning("=" * 50)
    logger.warning("MOCK OTP for %s  ->  %s", phone, otp)
    logger.warning("=" * 50)
    return True


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------
def request_otp(phone: str, audience: str) -> Tuple[bool, str, Optional[str]]:
    """
    Returns (success, message, debug_otp).
    debug_otp is only non-None in development so the app can auto-fill it.
    """
    # --- resend cooldown ---
    last = db.fetch_one(
        """
        select created_at from otp_requests
         where phone = :p and audience = :a
         order by created_at desc limit 1
        """,
        {"p": phone, "a": audience},
    )
    if last:
        age = (datetime.now(timezone.utc) - last["created_at"]).total_seconds()
        if age < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - age)
            return False, f"Please wait {wait} seconds before requesting a new OTP.", None

    # --- rate limit ---
    recent = db.fetch_value(
        """
        select count(*) from otp_requests
         where phone = :p and created_at > now() - (:mins || ' minutes')::interval
        """,
        {"p": phone, "mins": RATE_LIMIT_WINDOW_MINUTES},
    )
    if recent and int(recent) >= RATE_LIMIT_COUNT:
        return False, "Too many OTP requests. Please try again after some time.", None

    otp = generate_otp()
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.OTP_EXPIRE_SECONDS)

    db.execute(
        """
        insert into otp_requests (phone, audience, otp_hash, expires_at)
        values (:p, :a, :h, :e)
        """,
        {"p": phone, "a": audience, "h": hash_otp(otp), "e": expires},
    )

    if not _deliver(phone, otp):
        return False, "Could not send OTP right now. Please try again.", None

    debug_otp = otp if settings.is_dev else None
    return True, f"OTP sent to {phone}", debug_otp


def check_otp(phone: str, audience: str, otp: str) -> Tuple[bool, str]:
    """Verifies the latest unverified OTP for this phone + audience."""
    row = db.fetch_one(
        """
        select * from otp_requests
         where phone = :p and audience = :a and verified = false
         order by created_at desc limit 1
        """,
        {"p": phone, "a": audience},
    )

    if not row:
        return False, "No OTP found. Please request a new one."

    if row["expires_at"] < datetime.now(timezone.utc):
        return False, "OTP expired. Please request a new one."

    if row["attempts"] >= settings.OTP_MAX_ATTEMPTS:
        return False, "Too many wrong attempts. Please request a new OTP."

    if not verify_otp(otp, row["otp_hash"]):
        db.execute(
            "update otp_requests set attempts = attempts + 1 where id = :id",
            {"id": row["id"]},
        )
        left = settings.OTP_MAX_ATTEMPTS - (row["attempts"] + 1)
        return False, f"Wrong OTP. {left} attempt(s) left."

    db.execute("update otp_requests set verified = true where id = :id", {"id": row["id"]})
    return True, "OTP verified"


def cleanup_old_otps() -> int:
    """Optional housekeeping, safe to call from a cron endpoint."""
    db.execute("delete from otp_requests where created_at < now() - interval '1 day'")
    return 1
