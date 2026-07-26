# app/core/firebase.py
"""
Firebase Admin SDK — verifies the ID token that the Flutter apps obtain
after a successful Firebase Phone Auth sign-in.

Flow:
  1. Flutter app calls FirebaseAuth.verifyPhoneNumber() -> user enters OTP
  2. App gets a Firebase ID token via user.getIdToken()
  3. App sends that token to our backend
  4. Backend verifies it here and issues our own JWT

The service account JSON is stored in one env var as a single-line string.
"""

import json
import logging
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_initialised = False
_init_error: Optional[str] = None


def _init() -> bool:
    """Lazily initialise the Firebase Admin app exactly once."""
    global _initialised, _init_error

    if _initialised:
        return True
    if _init_error:
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials

        raw = settings.FIREBASE_SERVICE_ACCOUNT_JSON.strip()
        if not raw:
            _init_error = "FIREBASE_SERVICE_ACCOUNT_JSON is empty"
            logger.error(_init_error)
            return False

        info = json.loads(raw)
        # Railway env vars turn real newlines into the two characters \ and n
        if "private_key" in info:
            info["private_key"] = info["private_key"].replace("\\n", "\n")

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(info))

        _initialised = True
        logger.info("Firebase Admin initialised for project %s", info.get("project_id"))
        return True

    except Exception as exc:  # noqa: BLE001
        _init_error = str(exc)
        logger.exception("Firebase Admin init failed: %s", exc)
        return False


def verify_id_token(id_token: str) -> Optional[Dict[str, Any]]:
    """
    Returns a dict with uid / phone / email / name, or None if the token
    is invalid, expired or revoked.
    """
    if not _init():
        return None

    try:
        from firebase_admin import auth as fb_auth

        decoded = fb_auth.verify_id_token(id_token, check_revoked=False)

        phone = decoded.get("phone_number")  # E.164, e.g. +919876543210
        if phone and phone.startswith("+91"):
            phone = phone[3:]
        elif phone and phone.startswith("+"):
            phone = phone[1:]

        return {
            "uid": decoded.get("uid"),
            "phone": phone,
            "email": decoded.get("email"),
            "name": decoded.get("name"),
            "picture": decoded.get("picture"),
            "provider": (decoded.get("firebase") or {}).get("sign_in_provider"),
        }

    except Exception as exc:  # noqa: BLE001
        logger.warning("Firebase token verification failed: %s", exc)
        return None


def is_configured() -> bool:
    return bool(settings.FIREBASE_SERVICE_ACCOUNT_JSON.strip())


def health() -> Dict[str, Any]:
    return {
        "configured": is_configured(),
        "initialised": _initialised,
        "error": _init_error,
    }
