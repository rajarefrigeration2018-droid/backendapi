# app/dependencies.py
"""
Reusable FastAPI dependencies: auth guards + standard response envelope.
"""

from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import (
    ADMIN_AUD,
    PARTNER_AUD,
    USER_AUD,
    decode_token,
    has_permission,
)
from app.database import db

bearer = HTTPBearer(auto_error=False)


# ------------------------------------------------------------------
# Response envelope
# ------------------------------------------------------------------
def ok(data: Any = None, message: str = "Success") -> Dict[str, Any]:
    return {"success": True, "message": message, "data": data, "error_code": None}


def fail(message: str, error_code: str = "ERROR", status_code: int = 400):
    raise HTTPException(
        status_code=status_code,
        detail={"success": False, "message": message, "data": None, "error_code": error_code},
    )


# ------------------------------------------------------------------
# Token extraction
# ------------------------------------------------------------------
def _token_or_401(cred: Optional[HTTPAuthorizationCredentials]) -> str:
    if cred is None or not cred.credentials:
        fail("Authentication required", "NO_TOKEN", status.HTTP_401_UNAUTHORIZED)
    return cred.credentials


# ------------------------------------------------------------------
# Current user
# ------------------------------------------------------------------
def get_current_user(
    cred: HTTPAuthorizationCredentials = Depends(bearer),
) -> Dict[str, Any]:
    payload = decode_token(_token_or_401(cred), USER_AUD)
    if not payload:
        fail("Session expired. Please login again.", "INVALID_TOKEN", 401)

    user = db.fetch_one("select * from users where id = :id", {"id": int(payload["sub"])})
    if not user:
        fail("User not found", "USER_NOT_FOUND", 401)
    if user["is_blocked"]:
        fail(user.get("block_reason") or "Your account has been blocked.", "USER_BLOCKED", 403)
    return user


def get_optional_user(
    cred: HTTPAuthorizationCredentials = Depends(bearer),
) -> Optional[Dict[str, Any]]:
    """For public endpoints that personalise output when logged in."""
    if cred is None or not cred.credentials:
        return None
    payload = decode_token(cred.credentials, USER_AUD)
    if not payload:
        return None
    return db.fetch_one("select * from users where id = :id", {"id": int(payload["sub"])})


# ------------------------------------------------------------------
# Current partner (mechanic)
# ------------------------------------------------------------------
def get_current_partner(
    cred: HTTPAuthorizationCredentials = Depends(bearer),
) -> Dict[str, Any]:
    payload = decode_token(_token_or_401(cred), PARTNER_AUD)
    if not payload:
        fail("Session expired. Please login again.", "INVALID_TOKEN", 401)

    partner = db.fetch_one("select * from partners where id = :id", {"id": int(payload["sub"])})
    if not partner:
        fail("Partner not found", "PARTNER_NOT_FOUND", 401)
    if partner["status"] == "suspended":
        fail("Your account is suspended. Contact support.", "PARTNER_SUSPENDED", 403)
    return partner


def get_approved_partner(
    partner: Dict[str, Any] = Depends(get_current_partner),
) -> Dict[str, Any]:
    """For endpoints only approved mechanics may touch (jobs, earnings)."""
    if partner["status"] != "approved":
        fail("Your profile is under review.", "PARTNER_NOT_APPROVED", 403)
    return partner


# ------------------------------------------------------------------
# Current admin
# ------------------------------------------------------------------
def get_current_admin(
    cred: HTTPAuthorizationCredentials = Depends(bearer),
) -> Dict[str, Any]:
    payload = decode_token(_token_or_401(cred), ADMIN_AUD)
    if not payload:
        fail("Session expired. Please login again.", "INVALID_TOKEN", 401)

    admin = db.fetch_one("select * from admins where id = :id", {"id": int(payload["sub"])})
    if not admin:
        fail("Admin not found", "ADMIN_NOT_FOUND", 401)
    if not admin["is_active"]:
        fail("Admin account disabled", "ADMIN_DISABLED", 403)
    return admin


def require_permission(permission: str):
    """
    Usage:
        @router.post("/services", dependencies=[Depends(require_permission("catalog"))])
    """

    def checker(admin: Dict[str, Any] = Depends(get_current_admin)) -> Dict[str, Any]:
        if not has_permission(admin, permission):
            fail(f"You do not have '{permission}' permission.", "NO_PERMISSION", 403)
        return admin

    return checker


def require_super_admin(admin: Dict[str, Any] = Depends(get_current_admin)) -> Dict[str, Any]:
    if admin["role"] != "super_admin":
        fail("Super admin only.", "SUPER_ADMIN_ONLY", 403)
    return admin


# ------------------------------------------------------------------
# Pagination
# ------------------------------------------------------------------
class Pagination:
    def __init__(self, page: int = 1, limit: int = 20):
        self.page = max(1, page)
        self.limit = min(max(1, limit), 100)
        self.offset = (self.page - 1) * self.limit

    def envelope(self, items: list, total: int) -> Dict[str, Any]:
        return {
            "items": items,
            "page": self.page,
            "limit": self.limit,
            "total": total,
            "total_pages": (total + self.limit - 1) // self.limit if self.limit else 0,
        }
