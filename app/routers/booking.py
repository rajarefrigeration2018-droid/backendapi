# app/routers/booking.py
"""
Booking engine — the heart of Mistrio.

User-facing:
  addresses CRUD
  POST /price-preview        server-calculated cart total (app never does math)
  POST /bookings             create a booking
  GET  /bookings             my bookings (tabbed)
  GET  /bookings/{id}        full detail with timeline
  POST /bookings/{id}/cancel
  POST /bookings/{id}/reschedule
  POST /bookings/{id}/extra-charges/{cid}/respond   approve or reject
  POST /bookings/{id}/review
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.security import generate_numeric_code
from app.database import db
from app.dependencies import Pagination, fail, get_current_user, ok
from app.services import pricing

router = APIRouter(tags=["Booking"])

ACTIVE_STATUSES = (
    "pending", "confirmed", "assigned", "partner_on_the_way", "arrived", "in_progress",
)


# ==================================================================
# SCHEMAS
# ==================================================================
class AddressIn(BaseModel):
    label: str = "Home"
    house: Optional[str] = None
    area: Optional[str] = None
    landmark: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: str = Field(..., min_length=6, max_length=6)
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_default: bool = False


class CartItem(BaseModel):
    service_id: int
    option_id: Optional[int] = None
    qty: int = 1


class PricePreviewIn(BaseModel):
    items: List[CartItem]
    coupon_code: Optional[str] = None
    use_wallet: bool = False


class BookingCreateIn(BaseModel):
    items: List[CartItem]
    address_id: int
    scheduled_date: date
    slot_id: int
    payment_mode: str = "cod"          # cod | online | wallet
    coupon_code: Optional[str] = None
    use_wallet: bool = False
    user_notes: Optional[str] = None


class CancelIn(BaseModel):
    reason: Optional[str] = None


class RescheduleIn(BaseModel):
    scheduled_date: date
    slot_id: int


class ExtraChargeRespondIn(BaseModel):
    approve: bool


class ReviewIn(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None
    images: List[str] = []


# ==================================================================
# ADDRESSES
# ==================================================================
@router.get("/addresses")
def list_addresses(user=Depends(get_current_user)):
    rows = db.fetch_all(
        "select * from user_addresses where user_id = :u order by is_default desc, id desc",
        {"u": user["id"]},
    )
    return ok(rows)


@router.post("/addresses")
def create_address(body: AddressIn, user=Depends(get_current_user)):
    area = db.fetch_one(
        "select city, state, is_active from service_areas where pincode = :p",
        {"p": body.pincode},
    )
    if not area or not area["is_active"]:
        fail("Abhi hum is pincode par service nahi dete.", "NOT_SERVICEABLE")

    if body.is_default:
        db.execute(
            "update user_addresses set is_default = false where user_id = :u", {"u": user["id"]}
        )

    has_any = db.fetch_value(
        "select count(*) from user_addresses where user_id = :u", {"u": user["id"]}
    )
    make_default = body.is_default or not has_any or int(has_any) == 0

    row = db.execute(
        """
        insert into user_addresses (user_id, label, house, area, landmark, city, state,
                                    pincode, lat, lng, is_default)
        values (:u, :l, :h, :ar, :lm, :c, :s, :p, :lat, :lng, :d)
        returning *
        """,
        {
            "u": user["id"], "l": body.label, "h": body.house, "ar": body.area,
            "lm": body.landmark, "c": body.city or area["city"],
            "s": body.state or area["state"], "p": body.pincode,
            "lat": body.lat, "lng": body.lng, "d": make_default,
        },
    )
    return ok(row, "Address saved")


@router.put("/addresses/{aid}")
def update_address(aid: int, body: AddressIn, user=Depends(get_current_user)):
    owned = db.fetch_one(
        "select id from user_addresses where id = :id and user_id = :u",
        {"id": aid, "u": user["id"]},
    )
    if not owned:
        fail("Address not found", "NOT_FOUND", 404)

    if body.is_default:
        db.execute(
            "update user_addresses set is_default = false where user_id = :u", {"u": user["id"]}
        )

    row = db.execute(
        """
        update user_addresses
           set label=:l, house=:h, area=:ar, landmark=:lm, city=:c, state=:s,
               pincode=:p, lat=:lat, lng=:lng, is_default=:d
         where id = :id and user_id = :u
        returning *
        """,
        {
            "l": body.label, "h": body.house, "ar": body.area, "lm": body.landmark,
            "c": body.city, "s": body.state, "p": body.pincode, "lat": body.lat,
            "lng": body.lng, "d": body.is_default, "id": aid, "u": user["id"],
        },
    )
    return ok(row, "Address updated")


@router.delete("/addresses/{aid}")
def delete_address(aid: int, user=Depends(get_current_user)):
    active = db.fetch_one(
        f"""
        select id from bookings
         where address_id = :a and user_id = :u
           and status in {ACTIVE_STATUSES}
         limit 1
        """,
        {"a": aid, "u": user["id"]},
    )
    if active:
        fail("Is address par ek active booking hai.", "ADDRESS_IN_USE")

    db.execute(
        "delete from user_addresses where id = :id and user_id = :u",
        {"id": aid, "u": user["id"]},
    )
    return ok(None, "Address deleted")


# ==================================================================
# PRICE PREVIEW
# ==================================================================
@router.post("/price-preview")
def price_preview(body: PricePreviewIn, user=Depends(get_current_user)):
    """
    Called every time the cart changes or a coupon is typed.
    The app renders exactly what this returns and never calculates anything.
    """
    try:
        breakup = pricing.calculate(
            items=[i.model_dump() for i in body.items],
            user_id=user["id"],
            coupon_code=body.coupon_code,
            use_wallet=body.use_wallet,
        )
    except ValueError as exc:
        fail(str(exc), "INVALID_CART")

    return ok(breakup.as_dict())


# ==================================================================
# CREATE BOOKING
# ==================================================================
@router.post("/bookings")
def create_booking(body: BookingCreateIn, user=Depends(get_current_user)):
    # ---------- address ----------
    address = db.fetch_one(
        "select * from user_addresses where id = :a and user_id = :u",
        {"a": body.address_id, "u": user["id"]},
    )
    if not address:
        fail("Address not found", "ADDRESS_NOT_FOUND", 404)

    area = db.fetch_one(
        "select is_active from service_areas where pincode = :p", {"p": address["pincode"]}
    )
    if not area or not area["is_active"]:
        fail("Abhi hum is pincode par service nahi dete.", "NOT_SERVICEABLE")

    # ---------- date ----------
    today = datetime.now().date()
    if body.scheduled_date < today:
        fail("Purani date par booking nahi ho sakti.", "PAST_DATE")
    max_days = int(pricing._num(pricing.get_config("max_advance_days").get("max_advance_days"), 30))
    if body.scheduled_date > today + timedelta(days=max_days):
        fail(f"{max_days} din se aage ki booking nahi ho sakti.", "TOO_FAR")

    # ---------- slot ----------
    slot = db.fetch_one(
        "select * from time_slots where id = :id and is_active", {"id": body.slot_id}
    )
    if not slot:
        fail("Ye slot available nahi hai.", "SLOT_NOT_FOUND", 404)

    if body.scheduled_date == today and slot["start_time"] <= datetime.now().time():
        fail("Ye slot nikal chuka hai. Koi aur slot chunein.", "SLOT_PASSED")

    taken = db.fetch_value(
        """
        select count(*) from bookings
         where scheduled_date = :d and slot_id = :s
           and status not in ('cancelled','rejected')
        """,
        {"d": body.scheduled_date, "s": body.slot_id},
    )
    if taken and int(taken) >= slot["max_bookings"]:
        fail("Ye slot full ho gaya hai. Koi aur slot chunein.", "SLOT_FULL")

    # ---------- payment mode allowed? ----------
    cfg = pricing.get_config("enable_online_payment", "enable_cod", "enable_wallet")
    mode_key = {
        "online": "enable_online_payment",
        "cod": "enable_cod",
        "wallet": "enable_wallet",
    }.get(body.payment_mode)
    if not mode_key or not cfg.get(mode_key, True):
        fail("Ye payment method abhi available nahi hai.", "PAYMENT_MODE_DISABLED")

    # ---------- price ----------
    try:
        breakup = pricing.calculate(
            items=[i.model_dump() for i in body.items],
            user_id=user["id"],
            coupon_code=body.coupon_code,
            use_wallet=body.use_wallet,
        )
    except ValueError as exc:
        fail(str(exc), "INVALID_CART")

    if body.coupon_code and not breakup.coupon_applied:
        fail(breakup.coupon_message or "Coupon apply nahi hua", "COUPON_INVALID")

    # ---------- wallet ----------
    wallet_used = 0.0
    if body.use_wallet and breakup.wallet_usable > 0:
        wallet_used = breakup.wallet_usable

    if body.payment_mode == "wallet" and wallet_used < breakup.total:
        fail("Wallet mein paisa kam hai.", "INSUFFICIENT_WALLET")

    # ---------- write ----------
    code = db.fetch_value("select gen_booking_code()")
    snapshot = {
        k: address[k]
        for k in ("label", "house", "area", "landmark", "city", "state", "pincode", "lat", "lng")
    }
    otp = generate_numeric_code(4)

    booking = db.execute(
        """
        insert into bookings
          (booking_code, user_id, address_id, addr_snapshot, status, scheduled_date,
           slot_id, slot_label, payment_mode, payment_status, subtotal, visit_charge,
           discount, tax, total, coupon_id, coupon_code, otp_start, user_notes)
        values
          (:code, :uid, :aid, cast(:snap as jsonb), 'pending', :d, :sid, :slabel,
           cast(:pm as payment_mode), 'pending', :sub, :vc, :disc, :tax, :total,
           :cid, :ccode, :otp, :notes)
        returning *
        """,
        {
            "code": code, "uid": user["id"], "aid": address["id"],
            "snap": json.dumps(snapshot, default=str),
            "d": body.scheduled_date, "sid": slot["id"], "slabel": slot["label"],
            "pm": body.payment_mode, "sub": breakup.subtotal, "vc": breakup.visit_charge,
            "disc": breakup.discount, "tax": breakup.tax, "total": breakup.total,
            "cid": breakup.coupon_id, "ccode": breakup.coupon_code,
            "otp": otp, "notes": body.user_notes,
        },
    )

    for item in breakup.items:
        db.execute(
            """
            insert into booking_items
              (booking_id, service_id, option_id, service_name, option_name,
               qty, unit_price, line_total)
            values (:b, :s, :o, :sn, :on, :q, :up, :lt)
            """,
            {
                "b": booking["id"], "s": item["service_id"], "o": item["option_id"],
                "sn": item["service_name"], "on": item["option_name"],
                "q": item["qty"], "up": item["unit_price"], "lt": item["line_total"],
            },
        )

    if breakup.coupon_id:
        pricing.consume_coupon(breakup.coupon_id, user["id"], breakup.discount, booking["id"])

    # ---------- wallet debit ----------
    if wallet_used > 0:
        _debit_wallet(user["id"], wallet_used, f"Booking {code}", booking["id"])

    # ---------- confirm immediately for cod/wallet ----------
    fully_paid_by_wallet = wallet_used >= breakup.total
    if body.payment_mode in ("cod", "wallet") or fully_paid_by_wallet:
        booking = _set_status(
            booking["id"], "confirmed", "user", user["id"], "Booking placed"
        )
        if fully_paid_by_wallet:
            db.execute(
                "update bookings set payment_status = 'paid' where id = :id",
                {"id": booking["id"]},
            )
        # TODO(next batch): trigger auto-assignment to nearby partners here

    return ok(
        {
            "booking": _clean(booking),
            "booking_code": code,
            "needs_payment": body.payment_mode == "online" and not fully_paid_by_wallet,
            "payable_now": round(max(0.0, breakup.total - wallet_used), 2),
            "wallet_used": round(wallet_used, 2),
        },
        "Booking confirm ho gayi!",
    )


# ==================================================================
# LIST & DETAIL
# ==================================================================
@router.get("/bookings")
def my_bookings(
    tab: str = Query("all", description="all | upcoming | completed | cancelled"),
    page: int = 1,
    limit: int = 20,
    user=Depends(get_current_user),
):
    pg = Pagination(page, limit)

    where = "b.user_id = :u"
    params: Dict[str, Any] = {"u": user["id"], "l": pg.limit, "o": pg.offset}

    if tab == "upcoming":
        where += f" and b.status in {ACTIVE_STATUSES}"
    elif tab == "completed":
        where += " and b.status in ('completed','paid')"
    elif tab == "cancelled":
        where += " and b.status in ('cancelled','rejected')"

    rows = db.fetch_all(
        f"""
        select b.id, b.booking_code, b.status::text as status, b.scheduled_date,
               b.slot_label, b.total, b.payment_mode::text as payment_mode,
               b.payment_status::text as payment_status, b.created_at,
               b.addr_snapshot, p.name as partner_name, p.photo as partner_photo,
               (select string_agg(bi.service_name, ', ') from booking_items bi
                 where bi.booking_id = b.id) as services,
               (select count(*) from reviews r where r.booking_id = b.id) as has_review
          from bookings b
          left join partners p on p.id = b.assigned_partner_id
         where {where}
         order by b.created_at desc
         limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from bookings b where {where}", {"u": user["id"]}
    )

    for r in rows:
        r["total"] = float(r["total"])
        r["has_review"] = bool(r["has_review"])

    return ok(pg.envelope(rows, int(total or 0)))


@router.get("/bookings/{bid}")
def booking_detail(bid: int, user=Depends(get_current_user)):
    booking = db.fetch_one(
        """
        select b.*, b.status::text as status,
               b.payment_mode::text as payment_mode,
               b.payment_status::text as payment_status,
               p.name as partner_name, p.phone as partner_phone, p.photo as partner_photo,
               p.rating_avg as partner_rating, p.jobs_completed as partner_jobs,
               p.current_lat as partner_lat, p.current_lng as partner_lng
          from bookings b
          left join partners p on p.id = b.assigned_partner_id
         where b.id = :id and b.user_id = :u
        """,
        {"id": bid, "u": user["id"]},
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)

    items = db.fetch_all(
        "select * from booking_items where booking_id = :b", {"b": bid}
    )
    extras = db.fetch_all(
        "select id, label, amount, approved_by_user, rejected, created_at "
        "from booking_extra_charges where booking_id = :b order by id",
        {"b": bid},
    )
    timeline = db.fetch_all(
        "select to_status::text as status, actor::text as actor, note, created_at "
        "from booking_status_history where booking_id = :b order by created_at",
        {"b": bid},
    )
    review = db.fetch_one("select * from reviews where booking_id = :b", {"b": bid})

    for i in items:
        i["unit_price"] = float(i["unit_price"])
        i["line_total"] = float(i["line_total"])
    for e in extras:
        e["amount"] = float(e["amount"])

    cancel_info = _cancellation_info(booking)

    # only reveal the start OTP when the mechanic is actually on site
    show_otp = booking["status"] in ("assigned", "partner_on_the_way", "arrived")

    return ok(
        {
            "booking": _clean(booking),
            "items": items,
            "extra_charges": extras,
            "pending_approval": [e for e in extras if not e["approved_by_user"] and not e["rejected"]],
            "timeline": timeline,
            "review": review,
            "start_otp": booking["otp_start"] if show_otp else None,
            "can_cancel": cancel_info["can_cancel"],
            "cancellation_fee": cancel_info["fee"],
            "cancellation_note": cancel_info["note"],
            "can_reschedule": booking["status"] in ("pending", "confirmed", "assigned"),
            "can_review": booking["status"] in ("completed", "paid") and not review,
            "partner": (
                {
                    "name": booking["partner_name"],
                    "phone": booking["partner_phone"],
                    "photo": booking["partner_photo"],
                    "rating": float(booking["partner_rating"] or 0),
                    "jobs_completed": booking["partner_jobs"],
                    "lat": booking["partner_lat"],
                    "lng": booking["partner_lng"],
                }
                if booking["assigned_partner_id"]
                else None
            ),
        }
    )


# ==================================================================
# CANCEL
# ==================================================================
@router.post("/bookings/{bid}/cancel")
def cancel_booking(bid: int, body: CancelIn, user=Depends(get_current_user)):
    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id and user_id = :u",
        {"id": bid, "u": user["id"]},
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)

    info = _cancellation_info(booking)
    if not info["can_cancel"]:
        fail(info["note"] or "Ye booking cancel nahi ho sakti.", "CANNOT_CANCEL")

    fee = info["fee"]

    db.execute(
        """
        update bookings
           set status = 'cancelled', cancelled_at = now(), cancelled_by = 'user',
               cancel_reason = :r, cancellation_fee = :f
         where id = :id
        """,
        {"r": body.reason, "f": fee, "id": bid},
    )
    _history(bid, booking["status"], "cancelled", "user", user["id"], body.reason)

    pricing.release_coupon(bid)

    # refund whatever was already paid, minus the fee, into the wallet
    refund = 0.0
    if booking["payment_status"] == "paid":
        refund = max(0.0, float(booking["total"]) - fee)
        if refund > 0:
            _credit_wallet(user["id"], refund, f"Refund for {booking['booking_code']}", bid)

    return ok(
        {"cancellation_fee": fee, "refund_to_wallet": round(refund, 2)},
        "Booking cancel ho gayi." + (f" ₹{fee:.0f} cancellation charge laga." if fee else ""),
    )


# ==================================================================
# RESCHEDULE
# ==================================================================
@router.post("/bookings/{bid}/reschedule")
def reschedule_booking(bid: int, body: RescheduleIn, user=Depends(get_current_user)):
    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id and user_id = :u",
        {"id": bid, "u": user["id"]},
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)
    if booking["status"] not in ("pending", "confirmed", "assigned"):
        fail("Ab reschedule nahi ho sakta.", "CANNOT_RESCHEDULE")

    if body.scheduled_date < datetime.now().date():
        fail("Purani date nahi chun sakte.", "PAST_DATE")

    slot = db.fetch_one(
        "select * from time_slots where id = :id and is_active", {"id": body.slot_id}
    )
    if not slot:
        fail("Slot not found", "SLOT_NOT_FOUND", 404)

    taken = db.fetch_value(
        """
        select count(*) from bookings
         where scheduled_date = :d and slot_id = :s
           and status not in ('cancelled','rejected') and id <> :id
        """,
        {"d": body.scheduled_date, "s": body.slot_id, "id": bid},
    )
    if taken and int(taken) >= slot["max_bookings"]:
        fail("Ye slot full hai.", "SLOT_FULL")

    # reassignment needed — drop the partner and go back to the pool
    db.execute(
        """
        update bookings
           set scheduled_date = :d, slot_id = :s, slot_label = :l,
               status = 'confirmed', assigned_partner_id = null, assigned_at = null
         where id = :id
        """,
        {"d": body.scheduled_date, "s": slot["id"], "l": slot["label"], "id": bid},
    )
    _history(
        bid, booking["status"], "confirmed", "user", user["id"],
        f"Rescheduled to {body.scheduled_date} {slot['label']}",
    )

    return ok(None, "Booking reschedule ho gayi.")


# ==================================================================
# EXTRA CHARGE APPROVAL
# ==================================================================
@router.post("/bookings/{bid}/extra-charges/{cid}/respond")
def respond_extra_charge(
    bid: int, cid: int, body: ExtraChargeRespondIn, user=Depends(get_current_user)
):
    booking = db.fetch_one(
        "select id from bookings where id = :id and user_id = :u", {"id": bid, "u": user["id"]}
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)

    charge = db.fetch_one(
        "select * from booking_extra_charges where id = :c and booking_id = :b",
        {"c": cid, "b": bid},
    )
    if not charge:
        fail("Charge not found", "NOT_FOUND", 404)
    if charge["approved_by_user"] or charge["rejected"]:
        fail("Is charge par aap pehle hi jawab de chuke hain.", "ALREADY_RESPONDED")

    db.execute(
        """
        update booking_extra_charges
           set approved_by_user = :a, rejected = :r, approved_at = now()
         where id = :id
        """,
        {"a": body.approve, "r": not body.approve, "id": cid},
    )

    updated = pricing.recalculate_booking(bid)
    return ok(
        {"new_total": float(updated["total"])},
        "Extra charge approve ho gaya." if body.approve else "Extra charge reject kar diya.",
    )


# ==================================================================
# REVIEW
# ==================================================================
@router.post("/bookings/{bid}/review")
def add_review(bid: int, body: ReviewIn, user=Depends(get_current_user)):
    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id and user_id = :u",
        {"id": bid, "u": user["id"]},
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)
    if booking["status"] not in ("completed", "paid"):
        fail("Kaam poora hone ke baad hi review de sakte hain.", "NOT_COMPLETED")
    if db.fetch_one("select 1 from reviews where booking_id = :b", {"b": bid}):
        fail("Aap pehle hi review de chuke hain.", "ALREADY_REVIEWED")

    review = db.execute(
        """
        insert into reviews (booking_id, user_id, partner_id, rating, comment, images)
        values (:b, :u, :p, :r, :c, :i) returning *
        """,
        {
            "b": bid, "u": user["id"], "p": booking["assigned_partner_id"],
            "r": body.rating, "c": body.comment, "i": body.images,
        },
    )

    if booking["assigned_partner_id"]:
        db.execute(
            """
            update partners p
               set rating_count = sub.cnt, rating_avg = sub.avg
              from (select count(*) as cnt, round(avg(rating)::numeric, 2) as avg
                      from reviews where partner_id = :p and is_visible) sub
             where p.id = :p
            """,
            {"p": booking["assigned_partner_id"]},
        )

    return ok(review, "Review ke liye dhanyavaad!")


# ==================================================================
# HELPERS
# ==================================================================
def _clean(row: Dict[str, Any]) -> Dict[str, Any]:
    """Decimal -> float for clean JSON."""
    money = (
        "subtotal", "visit_charge", "discount", "tax", "total",
        "extra_charges_total", "cancellation_fee",
    )
    out = dict(row)
    for k in money:
        if k in out and out[k] is not None:
            out[k] = float(out[k])
    out.pop("otp_start", None)     # never leak the OTP in a generic payload
    return out


def _history(
    booking_id: int, from_status: Optional[str], to_status: str,
    actor: str, actor_id: Optional[int], note: Optional[str] = None,
) -> None:
    db.execute(
        """
        insert into booking_status_history
          (booking_id, from_status, to_status, actor, actor_id, note)
        values (:b, cast(:f as booking_status), cast(:t as booking_status),
                cast(:ac as actor_type), :aid, :n)
        """,
        {"b": booking_id, "f": from_status, "t": to_status,
         "ac": actor, "aid": actor_id, "n": note},
    )


def _set_status(
    booking_id: int, status: str, actor: str, actor_id: Optional[int], note: Optional[str] = None
) -> Dict[str, Any]:
    current = db.fetch_value(
        "select status::text from bookings where id = :id", {"id": booking_id}
    )
    row = db.execute(
        "update bookings set status = cast(:s as booking_status) where id = :id returning *",
        {"s": status, "id": booking_id},
    )
    _history(booking_id, current, status, actor, actor_id, note)
    return row


def _cancellation_info(booking: Dict[str, Any]) -> Dict[str, Any]:
    """Free-cancel window and penalty both come from app_config."""
    status = booking["status"]

    if status in ("completed", "paid"):
        return {"can_cancel": False, "fee": 0.0, "note": "Kaam poora ho chuka hai."}
    if status in ("cancelled", "rejected"):
        return {"can_cancel": False, "fee": 0.0, "note": "Ye booking pehle hi cancel hai."}
    if status == "in_progress":
        return {"can_cancel": False, "fee": 0.0, "note": "Kaam shuru ho chuka hai."}

    cfg = pricing.get_config("cancel_free_window_min", "cancel_penalty_amount")
    window = int(pricing._num(cfg.get("cancel_free_window_min"), 60))
    penalty = pricing._num(cfg.get("cancel_penalty_amount"), 0)

    age_min = (datetime.now(timezone.utc) - booking["created_at"]).total_seconds() / 60

    # free inside the window, or if nobody has been assigned yet
    if age_min <= window or not booking.get("assigned_partner_id"):
        return {"can_cancel": True, "fee": 0.0, "note": None}

    return {
        "can_cancel": True,
        "fee": penalty,
        "note": f"Mechanic assign ho chuka hai, isliye ₹{penalty:.0f} cancellation charge lagega.",
    }


def _credit_wallet(user_id: int, amount: float, reason: str, ref_id: Optional[int]) -> None:
    updated = db.execute(
        "update users set wallet_balance = wallet_balance + :a where id = :id "
        "returning wallet_balance",
        {"a": amount, "id": user_id},
    )
    db.execute(
        """
        insert into wallet_transactions
          (owner_type, owner_id, direction, amount, balance_after, reason, ref_type, ref_id)
        values ('user', :u, 'credit', :a, :b, :r, 'booking', :ref)
        """,
        {"u": user_id, "a": amount, "b": updated["wallet_balance"], "r": reason, "ref": ref_id},
    )


def _debit_wallet(user_id: int, amount: float, reason: str, ref_id: Optional[int]) -> None:
    updated = db.execute(
        "update users set wallet_balance = wallet_balance - :a where id = :id "
        "returning wallet_balance",
        {"a": amount, "id": user_id},
    )
    db.execute(
        """
        insert into wallet_transactions
          (owner_type, owner_id, direction, amount, balance_after, reason, ref_type, ref_id)
        values ('user', :u, 'debit', :a, :b, :r, 'booking', :ref)
        """,
        {"u": user_id, "a": amount, "b": updated["wallet_balance"], "r": reason, "ref": ref_id},
    )
