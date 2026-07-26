# app/routers/auth.py
"""
Authentication for all three apps.

PRIMARY (production) — Firebase Phone Auth:
  The Flutter app runs the OTP flow with Firebase, gets an ID token, and
  posts it here. We verify it with the Firebase Admin SDK and issue our JWT.

  USER    : POST /auth/user/firebase
  PARTNER : POST /auth/partner/firebase

FALLBACK (local testing without Firebase) — backend OTP:
  USER    : /auth/user/send-otp    + /auth/user/verify-otp
  PARTNER : /auth/partner/send-otp + /auth/partner/verify-otp
  These are disabled automatically when AUTH_PROVIDER=firebase unless
  OTP_MASTER_CODE is set.

ADMIN : email + password -> /auth/admin/login
"""

import random
import string
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.core.firebase import health as firebase_health
from app.core.firebase import verify_id_token
from app.core.otp import check_otp, request_otp
from app.core.security import (
    ADMIN_AUD,
    PARTNER_AUD,
    USER_AUD,
    create_access_token,
    verify_password,
)
from app.database import db
from app.dependencies import (
    fail,
    get_current_admin,
    get_current_partner,
    get_current_user,
    ok,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ==================================================================
# SCHEMAS
# ==================================================================
class PhoneIn(BaseModel):
    phone: str = Field(..., min_length=10, max_length=10)

    @field_validator("phone")
    @classmethod
    def only_digits(cls, v: str) -> str:
        v = v.strip().replace(" ", "").replace("-", "")
        if v.startswith("+91"):
            v = v[3:]
        if v.startswith("91") and len(v) == 12:
            v = v[2:]
        if not v.isdigit() or len(v) != 10:
            raise ValueError("Enter a valid 10-digit mobile number")
        if v[0] not in "6789":
            raise ValueError("Enter a valid Indian mobile number")
        return v


class VerifyIn(PhoneIn):
    otp: str = Field(..., min_length=4, max_length=8)
    fcm_token: Optional[str] = None
    referral_code: Optional[str] = None


class FirebaseLoginIn(BaseModel):
    id_token: str = Field(..., description="Firebase ID token from the client SDK")
    fcm_token: Optional[str] = None
    referral_code: Optional[str] = None


class AdminLoginIn(BaseModel):
    email: str
    password: str


class ProfileIn(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    profile_image: Optional[str] = None
    fcm_token: Optional[str] = None


class PartnerRegisterIn(BaseModel):
    name: str
    photo: Optional[str] = None
    skills: list[int] = []
    service_area_pincodes: list[str] = []
    upi_id: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None


# ==================================================================
# HELPERS
# ==================================================================
def _new_referral_code(name: Optional[str]) -> str:
    base = "".join(c for c in (name or "MST").upper() if c.isalpha())[:4] or "MST"
    for _ in range(10):
        code = base + "".join(random.choices(string.digits, k=4))
        if not db.fetch_one("select 1 from users where referral_code = :c", {"c": code}):
            return code
    return "MST" + "".join(random.choices(string.digits + string.ascii_uppercase, k=6))


def _apply_referral(new_user_id: int, code: str) -> None:
    """Credit both wallets using the reward amounts stored in app_config."""
    referrer = db.fetch_one(
        "select id, name, wallet_balance from users where referral_code = :c", {"c": code.upper()}
    )
    if not referrer or referrer["id"] == new_user_id:
        return

    cfg = {
        r["key"]: r["value"]
        for r in db.fetch_all(
            "select key, value from app_config "
            "where key in ('referral_reward_user','referral_reward_friend')"
        )
    }
    reward_referrer = float(cfg.get("referral_reward_user", 0) or 0)
    reward_friend = float(cfg.get("referral_reward_friend", 0) or 0)

    db.execute(
        "update users set referred_by = :ref where id = :id",
        {"ref": referrer["id"], "id": new_user_id},
    )

    for owner_id, amount, reason in (
        (referrer["id"], reward_referrer, "Referral bonus"),
        (new_user_id, reward_friend, "Welcome bonus"),
    ):
        if amount <= 0:
            continue
        updated = db.execute(
            "update users set wallet_balance = wallet_balance + :a "
            "where id = :id returning wallet_balance",
            {"a": amount, "id": owner_id},
        )
        db.execute(
            """
            insert into wallet_transactions
              (owner_type, owner_id, direction, amount, balance_after, reason, ref_type, ref_id)
            values ('user', :oid, 'credit', :amt, :bal, :reason, 'referral', :ref)
            """,
            {
                "oid": owner_id,
                "amt": amount,
                "bal": updated["wallet_balance"],
                "reason": reason,
                "ref": new_user_id,
            },
        )


def _public_user(u: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": u["id"],
        "phone": u["phone"],
        "name": u.get("name"),
        "email": u.get("email"),
        "profile_image": u.get("profile_image"),
        "referral_code": u.get("referral_code"),
        "wallet_balance": float(u.get("wallet_balance") or 0),
        "profile_complete": bool(u.get("name")),
    }


def _public_partner(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": p["id"],
        "phone": p["phone"],
        "name": p.get("name"),
        "photo": p.get("photo"),
        "status": p["status"],
        "reject_reason": p.get("reject_reason"),
        "skills": p.get("skills") or [],
        "service_area_pincodes": p.get("service_area_pincodes") or [],
        "is_online": p["is_online"],
        "rating_avg": float(p.get("rating_avg") or 0),
        "rating_count": p.get("rating_count") or 0,
        "jobs_completed": p.get("jobs_completed") or 0,
        "wallet_balance": float(p.get("wallet_balance") or 0),
        "upi_id": p.get("upi_id"),
        "profile_complete": bool(p.get("name")),
    }


def _guard_fallback_otp() -> None:
    """Backend OTP is only allowed when Firebase auth is off, or while testing."""
    if settings.use_firebase_auth and not settings.OTP_MASTER_CODE:
        fail(
            "This build uses Firebase Phone Auth. Use /auth/user/firebase instead.",
            "USE_FIREBASE",
            400,
        )


# ==================================================================
# FIREBASE PHONE AUTH  (primary)
# ==================================================================
@router.get("/firebase/health")
def firebase_status():
    return ok(firebase_health())


@router.post("/user/firebase")
def user_firebase_login(body: FirebaseLoginIn):
    """
    Flutter side:
        final cred = await FirebaseAuth.instance.signInWithCredential(...);
        final idToken = await cred.user!.getIdToken();
        POST /api/auth/user/firebase  { "id_token": idToken }
    """
    info = verify_id_token(body.id_token)
    if not info:
        fail("Login failed. Please try again.", "INVALID_FIREBASE_TOKEN", 401)
    if not info.get("phone"):
        fail("Phone number not found in this login.", "NO_PHONE", 400)

    phone, uid = info["phone"], info["uid"]

    user = db.fetch_one(
        "select * from users where firebase_uid = :u or phone = :p",
        {"u": uid, "p": phone},
    )
    is_new = user is None

    if is_new:
        user = db.execute(
            """
            insert into users (phone, firebase_uid, name, email, profile_image,
                               referral_code, fcm_token, last_login_at)
            values (:p, :u, :n, :e, :img, :rc, :f, now())
            returning *
            """,
            {
                "p": phone, "u": uid, "n": info.get("name"), "e": info.get("email"),
                "img": info.get("picture"), "rc": _new_referral_code(info.get("name")),
                "f": body.fcm_token,
            },
        )
        if body.referral_code:
            _apply_referral(user["id"], body.referral_code)
            user = db.fetch_one("select * from users where id = :id", {"id": user["id"]})
    else:
        if user["is_blocked"]:
            fail(user.get("block_reason") or "Your account has been blocked.", "USER_BLOCKED", 403)
        user = db.execute(
            """
            update users
               set firebase_uid = :u,
                   fcm_token    = coalesce(:f, fcm_token),
                   last_login_at = now()
             where id = :id
            returning *
            """,
            {"u": uid, "f": body.fcm_token, "id": user["id"]},
        )

    token = create_access_token(user["id"], USER_AUD)
    return ok(
        {"token": token, "is_new_user": is_new, "user": _public_user(user)},
        "Welcome to Mistrio!" if is_new else "Welcome back!",
    )


@router.post("/partner/firebase")
def partner_firebase_login(body: FirebaseLoginIn):
    info = verify_id_token(body.id_token)
    if not info:
        fail("Login failed. Please try again.", "INVALID_FIREBASE_TOKEN", 401)
    if not info.get("phone"):
        fail("Phone number not found in this login.", "NO_PHONE", 400)

    phone, uid = info["phone"], info["uid"]

    partner = db.fetch_one(
        "select * from partners where firebase_uid = :u or phone = :p",
        {"u": uid, "p": phone},
    )
    is_new = partner is None

    if is_new:
        partner = db.execute(
            """
            insert into partners (phone, firebase_uid, photo, fcm_token)
            values (:p, :u, :img, :f) returning *
            """,
            {"p": phone, "u": uid, "img": info.get("picture"), "f": body.fcm_token},
        )
    else:
        if partner["status"] == "suspended":
            fail("Your account is suspended. Contact support.", "PARTNER_SUSPENDED", 403)
        partner = db.execute(
            """
            update partners
               set firebase_uid = :u, fcm_token = coalesce(:f, fcm_token)
             where id = :id returning *
            """,
            {"u": uid, "f": body.fcm_token, "id": partner["id"]},
        )

    token = create_access_token(partner["id"], PARTNER_AUD)
    return ok(
        {
            "token": token,
            "is_new_partner": is_new,
            "needs_registration": not partner.get("name"),
            "partner": _public_partner(partner),
        },
        "Login successful",
    )


# ==================================================================
# USER  (fallback OTP)
# ==================================================================
@router.post("/user/send-otp")
def user_send_otp(body: PhoneIn):
    _guard_fallback_otp()
    success, message, debug_otp = request_otp(body.phone, USER_AUD)
    if not success:
        fail(message, "OTP_SEND_FAILED", 429)
    return ok({"phone": body.phone, "debug_otp": debug_otp}, message)


@router.post("/user/verify-otp")
def user_verify_otp(body: VerifyIn):
    _guard_fallback_otp()
    valid, message = check_otp(body.phone, USER_AUD, body.otp)
    if not valid:
        fail(message, "OTP_INVALID", 401)

    user = db.fetch_one("select * from users where phone = :p", {"p": body.phone})
    is_new = user is None

    if is_new:
        user = db.execute(
            """
            insert into users (phone, referral_code, fcm_token, last_login_at)
            values (:p, :rc, :f, now())
            returning *
            """,
            {"p": body.phone, "rc": _new_referral_code(None), "f": body.fcm_token},
        )
        if body.referral_code:
            _apply_referral(user["id"], body.referral_code)
            user = db.fetch_one("select * from users where id = :id", {"id": user["id"]})
    else:
        if user["is_blocked"]:
            fail(user.get("block_reason") or "Your account has been blocked.", "USER_BLOCKED", 403)
        user = db.execute(
            "update users set fcm_token = coalesce(:f, fcm_token), last_login_at = now() "
            "where id = :id returning *",
            {"f": body.fcm_token, "id": user["id"]},
        )

    token = create_access_token(user["id"], USER_AUD)
    return ok(
        {"token": token, "is_new_user": is_new, "user": _public_user(user)},
        "Welcome to Mistrio!" if is_new else "Welcome back!",
    )


@router.get("/user/me")
def user_me(user: Dict[str, Any] = Depends(get_current_user)):
    return ok(_public_user(user))


@router.put("/user/me")
def user_update_profile(body: ProfileIn, user: Dict[str, Any] = Depends(get_current_user)):
    updated = db.execute(
        """
        update users
           set name          = coalesce(:name, name),
               email         = coalesce(:email, email),
               profile_image = coalesce(:img, profile_image),
               fcm_token     = coalesce(:fcm, fcm_token)
         where id = :id
        returning *
        """,
        {
            "name": body.name,
            "email": body.email,
            "img": body.profile_image,
            "fcm": body.fcm_token,
            "id": user["id"],
        },
    )
    return ok(_public_user(updated), "Profile updated")


@router.delete("/user/me")
def user_delete_account(user: Dict[str, Any] = Depends(get_current_user)):
    """Play Store requires an in-app delete option. We soft-delete."""
    open_booking = db.fetch_one(
        """
        select id from bookings
         where user_id = :id
           and status in ('pending','confirmed','assigned','partner_on_the_way',
                          'arrived','in_progress')
         limit 1
        """,
        {"id": user["id"]},
    )
    if open_booking:
        fail("You have an active booking. Please complete or cancel it first.", "ACTIVE_BOOKING")

    db.execute(
        """
        update users
           set is_blocked = true,
               block_reason = 'Account deleted by user',
               phone = phone || '_deleted_' || id,
               fcm_token = null,
               email = null
         where id = :id
        """,
        {"id": user["id"]},
    )
    return ok(None, "Account deleted")


# ==================================================================
# PARTNER  (fallback OTP + registration)
# ==================================================================
@router.post("/partner/send-otp")
def partner_send_otp(body: PhoneIn):
    _guard_fallback_otp()
    success, message, debug_otp = request_otp(body.phone, PARTNER_AUD)
    if not success:
        fail(message, "OTP_SEND_FAILED", 429)
    return ok({"phone": body.phone, "debug_otp": debug_otp}, message)


@router.post("/partner/verify-otp")
def partner_verify_otp(body: VerifyIn):
    _guard_fallback_otp()
    valid, message = check_otp(body.phone, PARTNER_AUD, body.otp)
    if not valid:
        fail(message, "OTP_INVALID", 401)

    partner = db.fetch_one("select * from partners where phone = :p", {"p": body.phone})
    is_new = partner is None

    if is_new:
        partner = db.execute(
            "insert into partners (phone, fcm_token) values (:p, :f) returning *",
            {"p": body.phone, "f": body.fcm_token},
        )
    else:
        if partner["status"] == "suspended":
            fail("Your account is suspended. Contact support.", "PARTNER_SUSPENDED", 403)
        partner = db.execute(
            "update partners set fcm_token = coalesce(:f, fcm_token) where id = :id returning *",
            {"f": body.fcm_token, "id": partner["id"]},
        )

    token = create_access_token(partner["id"], PARTNER_AUD)
    return ok(
        {
            "token": token,
            "is_new_partner": is_new,
            "needs_registration": not partner.get("name"),
            "partner": _public_partner(partner),
        },
        "Login successful",
    )


@router.post("/partner/register")
def partner_register(
    body: PartnerRegisterIn, partner: Dict[str, Any] = Depends(get_current_partner)
):
    """Called once after first login. Sets status to 'pending' for admin review."""
    if partner["status"] == "approved":
        fail("You are already approved. Use the profile screen to edit.", "ALREADY_APPROVED")

    updated = db.execute(
        """
        update partners
           set name = :name,
               photo = coalesce(:photo, photo),
               skills = :skills,
               service_area_pincodes = :pins,
               upi_id = coalesce(:upi, upi_id),
               bank_account_name = coalesce(:ban, bank_account_name),
               bank_account_number = coalesce(:bacc, bank_account_number),
               bank_ifsc = coalesce(:ifsc, bank_ifsc),
               status = 'pending',
               reject_reason = null
         where id = :id
        returning *
        """,
        {
            "name": body.name,
            "photo": body.photo,
            "skills": body.skills,
            "pins": body.service_area_pincodes,
            "upi": body.upi_id,
            "ban": body.bank_account_name,
            "bacc": body.bank_account_number,
            "ifsc": body.bank_ifsc,
            "id": partner["id"],
        },
    )
    return ok(_public_partner(updated), "Profile submitted. It is under admin review.")


@router.post("/partner/documents")
def partner_upload_document(
    doc_type: str,
    file_url: str,
    partner: Dict[str, Any] = Depends(get_current_partner),
):
    """file_url comes from the /upload endpoint (added in a later batch)."""
    doc = db.execute(
        """
        insert into partner_documents (partner_id, doc_type, file_url)
        values (:pid, :dt, :url) returning *
        """,
        {"pid": partner["id"], "dt": doc_type, "url": file_url},
    )
    return ok(doc, "Document uploaded")


@router.get("/partner/me")
def partner_me(partner: Dict[str, Any] = Depends(get_current_partner)):
    docs = db.fetch_all(
        "select id, doc_type, file_url, verified from partner_documents where partner_id = :id",
        {"id": partner["id"]},
    )
    return ok({**_public_partner(partner), "documents": docs})


# ==================================================================
# ADMIN
# ==================================================================
@router.post("/admin/login")
def admin_login(body: AdminLoginIn):
    admin = db.fetch_one(
        "select * from admins where lower(email) = lower(:e)", {"e": body.email.strip()}
    )
    if not admin or not verify_password(body.password, admin["password_hash"]):
        fail("Invalid email or password", "INVALID_CREDENTIALS", 401)
    if not admin["is_active"]:
        fail("This admin account is disabled", "ADMIN_DISABLED", 403)

    db.execute("update admins set last_login_at = now() where id = :id", {"id": admin["id"]})
    token = create_access_token(admin["id"], ADMIN_AUD, extra={"role": admin["role"]})

    return ok(
        {
            "token": token,
            "admin": {
                "id": admin["id"],
                "name": admin["name"],
                "email": admin["email"],
                "role": admin["role"],
                "permissions": admin["permissions"],
            },
        },
        "Login successful",
    )


@router.get("/admin/me")
def admin_me(admin: Dict[str, Any] = Depends(get_current_admin)):
    return ok(
        {
            "id": admin["id"],
            "name": admin["name"],
            "email": admin["email"],
            "role": admin["role"],
            "permissions": admin["permissions"],
        }
    )


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)


@router.post("/admin/change-password")
def admin_change_password(
    body: ChangePasswordIn, admin: Dict[str, Any] = Depends(get_current_admin)
):
    from app.core.security import hash_password

    if not verify_password(body.old_password, admin["password_hash"]):
        fail("Current password is wrong", "WRONG_PASSWORD", 401)

    db.execute(
        "update admins set password_hash = :h where id = :id",
        {"h": hash_password(body.new_password), "id": admin["id"]},
    )
    return ok(None, "Password changed. Please login again.")
