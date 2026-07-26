# app/core/security.py
"""
Authentication & authorisation for Mistrio.

Three separate token audiences so a user token can never be used on a
partner or admin endpoint:
    aud = "user" | "partner" | "admin"
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
from jose import JWTError, jwt

from app.config import settings

USER_AUD = "user"
PARTNER_AUD = "partner"
ADMIN_AUD = "admin"


# ------------------------------------------------------------------
# Passwords (admins only — users & partners use OTP)
# ------------------------------------------------------------------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------
# OTP
# ------------------------------------------------------------------
def generate_otp(length: Optional[int] = None) -> str:
    n = length or settings.OTP_LENGTH
    return "".join(str(secrets.randbelow(10)) for _ in range(n))


def hash_otp(otp: str) -> str:
    """OTPs are stored hashed so a DB leak cannot be used to log in."""
    return hash_password(otp)


def verify_otp(plain: str, hashed: str) -> bool:
    if settings.OTP_MASTER_CODE and plain == settings.OTP_MASTER_CODE:
        return True
    return verify_password(plain, hashed)


def generate_numeric_code(length: int = 4) -> str:
    """Used for booking start OTP shown by the customer to the mechanic."""
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


# ------------------------------------------------------------------
# JWT
# ------------------------------------------------------------------
def create_access_token(
    subject_id: int,
    audience: str,
    extra: Optional[Dict[str, Any]] = None,
    expire_minutes: Optional[int] = None,
) -> str:
    if expire_minutes is None:
        expire_minutes = (
            settings.ADMIN_TOKEN_EXPIRE_MINUTES
            if audience == ADMIN_AUD
            else settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": str(subject_id),
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
        "jti": secrets.token_hex(8),
    }
    if extra:
        payload.update(extra)

    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, expected_audience: str) -> Optional[Dict[str, Any]]:
    """Returns the payload, or None if the token is invalid/expired/wrong audience."""
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            audience=expected_audience,
        )
    except JWTError:
        return None


def create_refresh_token(subject_id: int, audience: str) -> str:
    return create_access_token(
        subject_id, audience, extra={"type": "refresh"}, expire_minutes=60 * 24 * 90
    )


# ------------------------------------------------------------------
# Admin permissions
# ------------------------------------------------------------------
def has_permission(admin: Dict[str, Any], permission: str) -> bool:
    """
    admin["permissions"] is a jsonb object, e.g.
        {"all": true}
        {"bookings": true, "partners": true, "config": false}
    """
    if admin.get("role") == "super_admin":
        return True
    perms = admin.get("permissions") or {}
    if perms.get("all") is True:
        return True
    return bool(perms.get(permission))
