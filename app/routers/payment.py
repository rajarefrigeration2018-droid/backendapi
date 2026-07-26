# app/routers/payment.py
"""
Payments, wallet, invoices and file uploads.

Razorpay flow:
  1. POST /payments/order        -> server creates the order (amount from DB)
  2. App opens the Razorpay checkout with the returned order_id
  3. POST /payments/verify       -> signature checked, booking marked paid
  4. POST /payments/webhook      -> Razorpay's own confirmation, used to
                                    reconcile anything step 3 missed

Replay protection: `payments.consumed` is flipped exactly once inside a
conditional UPDATE, so the same successful payment can never settle two
bookings.
"""

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field

from app.core import razorpay_client as rp
from app.core import storage
from app.database import db
from app.dependencies import (
    fail,
    get_current_partner,
    get_current_user,
    ok,
    require_permission,
)
from app.services import invoice as invoice_service
from app.services import assignment, pricing

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Payments"])


# ==================================================================
# SCHEMAS
# ==================================================================
class OrderIn(BaseModel):
    booking_id: Optional[int] = None
    part_order_id: Optional[int] = None
    wallet_topup: Optional[float] = None


class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


# ==================================================================
# CREATE ORDER
# ==================================================================
@router.post("/payments/order")
def create_order(body: OrderIn, user=Depends(get_current_user)):
    if not rp.is_configured():
        fail("Online payment is not available right now.", "GATEWAY_UNAVAILABLE", 503)

    if body.booking_id:
        booking = db.fetch_one(
            """
            select *, status::text as status, payment_status::text as payment_status
              from bookings where id = :id and user_id = :u
            """,
            {"id": body.booking_id, "u": user["id"]},
        )
        if not booking:
            fail("Booking not found", "NOT_FOUND", 404)
        if booking["payment_status"] == "paid":
            fail("This booking is already paid.", "ALREADY_PAID")
        if booking["status"] in ("cancelled", "rejected"):
            fail("This booking is cancelled.", "CANCELLED")

        amount = float(booking["total"])
        already = _wallet_applied(body.booking_id)
        amount = round(max(0.0, amount - already), 2)
        if amount <= 0:
            fail("Nothing left to pay on this booking.", "NOTHING_DUE")

        purpose, receipt = "booking", booking["booking_code"]
        notes = {"booking_id": str(booking["id"]), "code": booking["booking_code"]}

    elif body.part_order_id:
        order = db.fetch_one(
            """
            select *, payment_status::text as payment_status
              from part_orders where id = :id and user_id = :u
            """,
            {"id": body.part_order_id, "u": user["id"]},
        )
        if not order:
            fail("Order not found", "NOT_FOUND", 404)
        if order["payment_status"] == "paid":
            fail("This order is already paid.", "ALREADY_PAID")

        amount = float(order["total"])
        purpose, receipt = "parts", order["order_code"]
        notes = {"part_order_id": str(order["id"])}

    elif body.wallet_topup:
        cfg = pricing.get_config("min_wallet_topup", "max_wallet_topup")
        lo = pricing._num(cfg.get("min_wallet_topup"), 100)
        hi = pricing._num(cfg.get("max_wallet_topup"), 50000)
        if body.wallet_topup < lo:
            fail(f"Minimum top-up is Rs. {lo:.0f}", "BELOW_MINIMUM")
        if body.wallet_topup > hi:
            fail(f"Maximum top-up is Rs. {hi:.0f}", "ABOVE_MAXIMUM")

        amount = round(float(body.wallet_topup), 2)
        purpose, receipt = "wallet_topup", f"WALLET-{user['id']}"
        notes = {"user_id": str(user["id"]), "type": "wallet_topup"}

    else:
        fail("Specify booking_id, part_order_id or wallet_topup", "INVALID_REQUEST")

    rzp_order = rp.create_order(amount, receipt, notes)
    if not rzp_order:
        fail("Could not start the payment. Please try again.", "ORDER_FAILED", 502)

    db.execute(
        """
        insert into payments
          (booking_id, part_order_id, user_id, purpose, gateway, gateway_order_id,
           amount, status, raw_response)
        values (:b, :po, :u, :pur, 'razorpay', :oid, :amt, 'pending', cast(:raw as jsonb))
        """,
        {
            "b": body.booking_id, "po": body.part_order_id, "u": user["id"],
            "pur": purpose, "oid": rzp_order["id"], "amt": amount,
            "raw": json.dumps(rzp_order, default=str),
        },
    )

    from app.config import settings

    return ok(
        {
            "order_id": rzp_order["id"],
            "amount": amount,
            "amount_paise": rzp_order["amount"],
            "currency": "INR",
            "key_id": settings.RAZORPAY_KEY_ID,
            "name": pricing.get_config("brand_name").get("brand_name", "Mistrio"),
            "description": receipt,
            "prefill": {
                "name": user.get("name") or "",
                "contact": user["phone"],
                "email": user.get("email") or "",
            },
        }
    )


# ==================================================================
# VERIFY
# ==================================================================
@router.post("/payments/verify")
def verify_payment(body: VerifyIn, user=Depends(get_current_user)):
    if not rp.verify_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        db.execute(
            "update payments set status = 'failed' where gateway_order_id = :o",
            {"o": body.razorpay_order_id},
        )
        fail("Payment verification failed.", "INVALID_SIGNATURE", 400)

    # consume the order exactly once — this is the replay guard
    payment = db.execute(
        """
        update payments
           set consumed = true, status = 'paid',
               gateway_payment_id = :pid, gateway_signature = :sig
         where gateway_order_id = :oid
           and user_id = :u
           and consumed = false
        returning *
        """,
        {
            "pid": body.razorpay_payment_id, "sig": body.razorpay_signature,
            "oid": body.razorpay_order_id, "u": user["id"],
        },
    )
    if not payment:
        existing = db.fetch_one(
            "select *, status::text as status from payments where gateway_order_id = :o",
            {"o": body.razorpay_order_id},
        )
        if existing and existing["consumed"]:
            return ok({"already_processed": True}, "This payment was already processed.")
        fail("Payment record not found.", "PAYMENT_NOT_FOUND", 404)

    result = _apply_payment(payment)
    return ok(result, "Payment successful")


# ==================================================================
# WEBHOOK
# ==================================================================
@router.post("/payments/webhook")
async def razorpay_webhook(request: Request):
    """
    Configure in Razorpay Dashboard -> Settings -> Webhooks:
      URL:    https://your-api/api/payments/webhook
      Events: payment.captured, payment.failed, refund.processed
    """
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")

    if not rp.verify_webhook_signature(raw, signature):
        logger.warning("Webhook rejected: bad signature")
        fail("Invalid signature", "INVALID_SIGNATURE", 400)

    payload = json.loads(raw)
    event = payload.get("event")
    entity = (payload.get("payload", {}).get("payment", {}) or {}).get("entity", {})
    order_id = entity.get("order_id")

    logger.info("Razorpay webhook: %s for order %s", event, order_id)

    if not order_id:
        return ok(None, "Ignored")

    if event == "payment.captured":
        payment = db.execute(
            """
            update payments
               set consumed = true, status = 'paid',
                   gateway_payment_id = :pid,
                   raw_response = cast(:raw as jsonb)
             where gateway_order_id = :oid and consumed = false
            returning *
            """,
            {"pid": entity.get("id"), "oid": order_id, "raw": json.dumps(entity, default=str)},
        )
        if payment:
            _apply_payment(payment)
            logger.info("Webhook settled order %s", order_id)

    elif event == "payment.failed":
        db.execute(
            "update payments set status = 'failed', raw_response = cast(:raw as jsonb) "
            "where gateway_order_id = :oid and consumed = false",
            {"oid": order_id, "raw": json.dumps(entity, default=str)},
        )

    return ok(None, "Webhook processed")


# ==================================================================
# WALLET
# ==================================================================
@router.get("/wallet")
def wallet(user=Depends(get_current_user)):
    txns = db.fetch_all(
        """
        select direction::text as direction, amount, balance_after, reason, created_at
          from wallet_transactions
         where owner_type = 'user' and owner_id = :u
         order by created_at desc limit 100
        """,
        {"u": user["id"]},
    )
    for t in txns:
        t["amount"] = float(t["amount"])
        t["balance_after"] = float(t["balance_after"])

    cfg = pricing.get_config("min_wallet_topup", "max_wallet_topup")
    return ok(
        {
            "balance": float(user["wallet_balance"]),
            "transactions": txns,
            "min_topup": pricing._num(cfg.get("min_wallet_topup"), 100),
            "max_topup": pricing._num(cfg.get("max_wallet_topup"), 50000),
        }
    )


@router.get("/partner/wallet")
def partner_wallet(partner=Depends(get_current_partner)):
    txns = db.fetch_all(
        """
        select direction::text as direction, amount, balance_after, reason, created_at
          from wallet_transactions
         where owner_type = 'partner' and owner_id = :p
         order by created_at desc limit 100
        """,
        {"p": partner["id"]},
    )
    for t in txns:
        t["amount"] = float(t["amount"])
        t["balance_after"] = float(t["balance_after"])
    return ok({"balance": float(partner["wallet_balance"]), "transactions": txns})


# ==================================================================
# INVOICE
# ==================================================================
@router.get("/bookings/{bid}/invoice")
def get_invoice(bid: int, user=Depends(get_current_user)):
    booking = db.fetch_one(
        "select id, status::text as status from bookings where id = :id and user_id = :u",
        {"id": bid, "u": user["id"]},
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)
    if booking["status"] not in ("completed", "paid"):
        fail("The invoice is available once the job is completed.", "NOT_COMPLETED")

    url = invoice_service.get_or_create(bid)
    if not url:
        fail("Could not generate the invoice. Please try again.", "INVOICE_FAILED", 500)
    return ok({"invoice_url": url})


@router.post("/admin/bookings/{bid}/invoice")
def regenerate_invoice(bid: int, admin=Depends(require_permission("bookings"))):
    url = invoice_service.generate(bid)
    if not url:
        fail("Could not generate the invoice.", "INVOICE_FAILED", 500)
    return ok({"invoice_url": url}, "Invoice regenerated")


# ==================================================================
# FILE UPLOAD
# ==================================================================
@router.post("/upload")
async def upload_file(
    folder: str = Form(...),
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """Customer-side uploads: profile photo, review images."""
    return await _handle_upload(folder, file, allowed={"profiles", "reviews"})


@router.post("/partner/upload")
async def partner_upload(
    folder: str = Form(...),
    file: UploadFile = File(...),
    partner=Depends(get_current_partner),
):
    """Technician-side uploads: profile photo, ID documents, job photos."""
    return await _handle_upload(folder, file, allowed={"profiles", "documents", "jobs"})


@router.post("/admin/upload")
async def admin_upload(
    folder: str = Form(...),
    file: UploadFile = File(...),
    admin=Depends(require_permission("catalog")),
):
    return await _handle_upload(
        folder, file, allowed={"banners", "services", "parts", "profiles"}
    )


# ==================================================================
# HELPERS
# ==================================================================
async def _handle_upload(folder: str, file: UploadFile, allowed: set) -> Dict[str, Any]:
    if not storage.is_configured():
        fail("File upload is not configured.", "STORAGE_UNAVAILABLE", 503)
    if folder not in allowed:
        fail("You cannot upload to this folder.", "FOLDER_NOT_ALLOWED", 403)

    data = await file.read()
    valid, message = storage.validate(
        folder, file.content_type or "application/octet-stream", len(data)
    )
    if not valid:
        fail(message, "INVALID_FILE")

    url = storage.upload(folder, data, file.filename or "upload.jpg", file.content_type)
    if not url:
        fail("Upload failed. Please try again.", "UPLOAD_FAILED", 500)
    return ok({"url": url, "size": len(data)}, "Uploaded")


def _wallet_applied(booking_id: int) -> float:
    return pricing._num(
        db.fetch_value(
            """
            select coalesce(sum(amount), 0) from wallet_transactions
             where ref_type = 'booking' and ref_id = :b and direction = 'debit'
            """,
            {"b": booking_id},
        )
    )


def _apply_payment(payment: Dict[str, Any]) -> Dict[str, Any]:
    """Settles whatever the payment was for. Safe to call once per payment row."""
    purpose = payment["purpose"]
    amount = float(payment["amount"])

    if purpose == "wallet_topup":
        updated = db.execute(
            "update users set wallet_balance = wallet_balance + :a where id = :u "
            "returning wallet_balance",
            {"a": amount, "u": payment["user_id"]},
        )
        db.execute(
            """
            insert into wallet_transactions
              (owner_type, owner_id, direction, amount, balance_after, reason, ref_type, ref_id)
            values ('user', :u, 'credit', :a, :b, 'Wallet top-up', 'payment', :ref)
            """,
            {"u": payment["user_id"], "a": amount,
             "b": updated["wallet_balance"], "ref": payment["id"]},
        )
        return {"type": "wallet_topup", "new_balance": float(updated["wallet_balance"])}

    if purpose == "parts" and payment["part_order_id"]:
        db.execute(
            "update part_orders set payment_status = 'paid' where id = :id",
            {"id": payment["part_order_id"]},
        )
        return {"type": "parts", "order_id": payment["part_order_id"]}

    # ---------- booking ----------
    bid = payment["booking_id"]
    if not bid:
        return {"type": "unknown"}

    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id", {"id": bid}
    )
    db.execute(
        "update bookings set payment_status = 'paid' where id = :id", {"id": bid}
    )

    # a booking paid up front moves from pending to confirmed and enters
    # the assignment pool
    if booking and booking["status"] == "pending":
        db.execute("update bookings set status = 'confirmed' where id = :id", {"id": bid})
        db.execute(
            """
            insert into booking_status_history
              (booking_id, from_status, to_status, actor, note)
            values (:b, 'pending', 'confirmed', 'system', 'Payment received')
            """,
            {"b": bid},
        )
        try:
            assignment.offer_to_partners(bid)
        except Exception:  # noqa: BLE001
            logger.exception("Auto-assign failed after payment for booking %s", bid)

    # a booking paid after completion is fully closed
    elif booking and booking["status"] == "completed":
        db.execute("update bookings set status = 'paid' where id = :id", {"id": bid})
        try:
            invoice_service.generate(bid)
        except Exception:  # noqa: BLE001
            logger.exception("Invoice generation failed for booking %s", bid)

    return {"type": "booking", "booking_id": bid, "amount": amount}
