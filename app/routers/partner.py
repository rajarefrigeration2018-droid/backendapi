# app/routers/partner.py
"""
Partner (mechanic) APIs.

Job lifecycle the app walks through:
    offered -> accept -> on_the_way -> arrived -> [verify OTP] -> in_progress
    -> [add extra charges] -> complete -> earnings credited
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core import fcm
from app.database import db
from app.dependencies import Pagination, fail, get_approved_partner, get_current_partner, ok
from app.services import assignment, pricing

router = APIRouter(prefix="/partner", tags=["Partner"])


# ==================================================================
# SCHEMAS
# ==================================================================
class OnlineIn(BaseModel):
    is_online: bool
    lat: Optional[float] = None
    lng: Optional[float] = None


class LocationIn(BaseModel):
    lat: float
    lng: float


class RejectIn(BaseModel):
    reason: Optional[str] = None


class OtpVerifyIn(BaseModel):
    otp: str = Field(..., min_length=4, max_length=6)


class ExtraChargeIn(BaseModel):
    label: str
    amount: float = Field(..., gt=0)


class CompleteIn(BaseModel):
    before_photos: List[str] = []
    after_photos: List[str] = []
    notes: Optional[str] = None
    cash_collected: float = 0


class PayoutRequestIn(BaseModel):
    amount: float = Field(..., gt=0)
    method: str = "upi"


class ProfileUpdateIn(BaseModel):
    name: Optional[str] = None
    photo: Optional[str] = None
    skills: Optional[List[int]] = None
    service_area_pincodes: Optional[List[str]] = None
    upi_id: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc: Optional[str] = None
    fcm_token: Optional[str] = None


# ==================================================================
# STATUS / AVAILABILITY
# ==================================================================
@router.post("/online")
def toggle_online(body: OnlineIn, partner=Depends(get_approved_partner)):
    row = db.execute(
        """
        update partners
           set is_online = :on,
               current_lat = coalesce(:lat, current_lat),
               current_lng = coalesce(:lng, current_lng),
               location_updated_at = case when :lat is not null then now()
                                          else location_updated_at end
         where id = :id
        returning is_online
        """,
        {"on": body.is_online, "lat": body.lat, "lng": body.lng, "id": partner["id"]},
    )
    return ok(
        {"is_online": row["is_online"]},
        "You are online — new jobs will come in." if row["is_online"] else "You are offline.",
    )


@router.post("/location")
def ping_location(body: LocationIn, partner=Depends(get_approved_partner)):
    """Called every N seconds by the app while the mechanic is online."""
    db.execute(
        """
        update partners
           set current_lat = :lat, current_lng = :lng, location_updated_at = now()
         where id = :id
        """,
        {"lat": body.lat, "lng": body.lng, "id": partner["id"]},
    )
    return ok(None, "Location updated")


# ==================================================================
# DASHBOARD
# ==================================================================
@router.get("/dashboard")
def dashboard(partner=Depends(get_approved_partner)):
    today = datetime.now().date()

    today_stats = db.fetch_one(
        """
        select count(*) filter (where b.status in ('completed','paid')) as completed,
               count(*) filter (where b.status in ('assigned','partner_on_the_way',
                                                  'arrived','in_progress')) as active,
               coalesce(sum(e.net_payable) filter (where b.status in ('completed','paid')), 0)
                 as earnings
          from bookings b
          left join partner_earnings e on e.booking_id = b.id
         where b.assigned_partner_id = :p and b.scheduled_date = :d
        """,
        {"p": partner["id"], "d": today},
    )

    month_earnings = db.fetch_value(
        """
        select coalesce(sum(net_payable), 0) from partner_earnings
         where partner_id = :p and date_trunc('month', created_at) = date_trunc('month', now())
        """,
        {"p": partner["id"]},
    )

    pending_settlement = db.fetch_value(
        """
        select coalesce(sum(net_payable), 0) from partner_earnings
         where partner_id = :p and settlement_status in ('pending','approved')
        """,
        {"p": partner["id"]},
    )

    jobs = _job_rows(
        "b.assigned_partner_id = :p and b.scheduled_date = :d "
        "and b.status not in ('cancelled','rejected')",
        {"p": partner["id"], "d": today},
    )

    pending_offers = db.fetch_all(
        """
        select o.booking_id, o.offered_at, o.distance_km, b.booking_code, b.total,
               b.slot_label, b.scheduled_date, b.addr_snapshot,
               (select string_agg(service_name, ', ') from booking_items
                 where booking_id = b.id) as services
          from booking_offers o
          join bookings b on b.id = o.booking_id
         where o.partner_id = :p and o.response is null
           and b.status = 'confirmed' and b.assigned_partner_id is null
         order by o.offered_at desc
        """,
        {"p": partner["id"]},
    )
    for o in pending_offers:
        o["total"] = float(o["total"])

    return ok(
        {
            "is_online": partner["is_online"],
            "today": {
                "completed": today_stats["completed"],
                "active": today_stats["active"],
                "earnings": float(today_stats["earnings"]),
            },
            "month_earnings": float(month_earnings or 0),
            "pending_settlement": float(pending_settlement or 0),
            "wallet_balance": float(partner["wallet_balance"] or 0),
            "rating": float(partner["rating_avg"] or 0),
            "rating_count": partner["rating_count"],
            "jobs_completed": partner["jobs_completed"],
            "today_jobs": jobs,
            "pending_offers": pending_offers,
        }
    )


# ==================================================================
# JOB OFFERS
# ==================================================================
@router.get("/offers")
def my_offers(partner=Depends(get_approved_partner)):
    cfg = pricing.get_config("job_offer_timeout_sec", "default_commission_percent")
    timeout = int(pricing._num(cfg.get("job_offer_timeout_sec"), 60))
    commission = pricing._num(
        partner.get("commission_percent_override")
        or cfg.get("default_commission_percent"), 20
    )

    rows = db.fetch_all(
        """
        select o.booking_id, o.offered_at, o.distance_km,
               b.booking_code, b.total, b.slot_label, b.scheduled_date,
               b.addr_snapshot, b.payment_mode::text as payment_mode,
               (select string_agg(service_name, ', ') from booking_items
                 where booking_id = b.id) as services,
               extract(epoch from (now() - o.offered_at)) as age_sec
          from booking_offers o
          join bookings b on b.id = o.booking_id
         where o.partner_id = :p and o.response is null
           and b.status = 'confirmed' and b.assigned_partner_id is null
         order by o.offered_at desc
        """,
        {"p": partner["id"]},
    )

    out = []
    for r in rows:
        remaining = timeout - int(r["age_sec"])
        if remaining <= 0:
            continue
        gross = float(r["total"])
        snap = r["addr_snapshot"] or {}
        out.append(
            {
                "booking_id": r["booking_id"],
                "booking_code": r["booking_code"],
                "services": r["services"],
                "scheduled_date": str(r["scheduled_date"]),
                "slot_label": r["slot_label"],
                "payment_mode": r["payment_mode"],
                "distance_km": float(r["distance_km"]) if r["distance_km"] else None,
                # approximate location only — full address after accepting
                "area": snap.get("area") or snap.get("city"),
                "pincode": snap.get("pincode"),
                "gross": gross,
                "your_payout": round(gross * (100 - commission) / 100, 2),
                "seconds_left": remaining,
            }
        )
    return ok(out)


@router.post("/jobs/{bid}/accept")
def accept_job(bid: int, partner=Depends(get_approved_partner)):
    result = assignment.accept_job(bid, partner["id"])
    if not result["success"]:
        fail(result["message"], "ACCEPT_FAILED", 409)
    return ok(_job_detail(bid, partner["id"]), result["message"])


@router.post("/jobs/{bid}/reject")
def reject_job(bid: int, body: RejectIn, partner=Depends(get_approved_partner)):
    result = assignment.reject_job(bid, partner["id"], body.reason)
    return ok(None, result["message"])


# ==================================================================
# MY JOBS
# ==================================================================
@router.get("/jobs")
def my_jobs(
    tab: str = Query("today", description="today | upcoming | completed"),
    page: int = 1,
    limit: int = 20,
    partner=Depends(get_approved_partner),
):
    pg = Pagination(page, limit)
    today = datetime.now().date()
    params: Dict[str, Any] = {"p": partner["id"], "d": today}

    if tab == "today":
        where = ("b.assigned_partner_id = :p and b.scheduled_date = :d "
                 "and b.status not in ('cancelled','rejected')")
    elif tab == "upcoming":
        where = ("b.assigned_partner_id = :p and b.scheduled_date > :d "
                 "and b.status not in ('cancelled','rejected')")
    else:
        where = "b.assigned_partner_id = :p and b.status in ('completed','paid')"

    return ok(_job_rows(where, params, pg))


@router.get("/jobs/{bid}")
def job_detail(bid: int, partner=Depends(get_approved_partner)):
    return ok(_job_detail(bid, partner["id"]))


# ==================================================================
# JOB STATUS FLOW
# ==================================================================
@router.post("/jobs/{bid}/on-the-way")
def mark_on_the_way(bid: int, partner=Depends(get_approved_partner)):
    booking = _owned_job(bid, partner["id"], allowed=("assigned",))
    _advance(bid, booking["status"], "partner_on_the_way", partner["id"])
    fcm.notify_user(
        booking["user_id"], "partner_on_the_way",
        variables={"partner": partner["name"], "code": booking["booking_code"]},
        data={"booking_id": str(bid)},
    )
    return ok(None, "The customer has been notified that you are on the way.")


@router.post("/jobs/{bid}/arrived")
def mark_arrived(bid: int, partner=Depends(get_approved_partner)):
    booking = _owned_job(bid, partner["id"], allowed=("assigned", "partner_on_the_way"))
    _advance(bid, booking["status"], "arrived", partner["id"])
    return ok(
        {"needs_otp": True},
        "Ask the customer for the 4-digit OTP to start the job.",
    )


@router.post("/jobs/{bid}/start")
def start_job(bid: int, body: OtpVerifyIn, partner=Depends(get_approved_partner)):
    """OTP proves the mechanic is physically at the customer's place."""
    booking = _owned_job(bid, partner["id"], allowed=("arrived", "partner_on_the_way"))

    if body.otp.strip() != (booking["otp_start"] or ""):
        fail("Incorrect OTP. Please ask the customer again.", "WRONG_OTP", 401)

    db.execute(
        "update bookings set otp_verified_at = now(), started_at = now() where id = :id",
        {"id": bid},
    )
    _advance(bid, booking["status"], "in_progress", partner["id"], "OTP verified")
    return ok(None, "Job started. All the best!")


@router.post("/jobs/{bid}/extra-charges")
def add_extra_charge(bid: int, body: ExtraChargeIn, partner=Depends(get_approved_partner)):
    """
    Parts or gas used beyond the booked service.
    The customer must approve it before it hits the bill.
    """
    booking = _owned_job(bid, partner["id"], allowed=("in_progress", "arrived"))

    cap = pricing._num(
        pricing.get_config("max_extra_charge").get("max_extra_charge"), 5000
    )
    if body.amount > cap:
        fail(f"Charges above ₹{cap:.0f} need admin approval.", "AMOUNT_TOO_HIGH")

    charge = db.execute(
        """
        insert into booking_extra_charges (booking_id, label, amount, added_by)
        values (:b, :l, :a, :p) returning *
        """,
        {"b": bid, "l": body.label, "a": body.amount, "p": partner["id"]},
    )

    fcm.notify_user(
        booking["user_id"], "extra_charge_added",
        variables={
            "label": body.label,
            "amount": f"{body.amount:.0f}",
            "code": booking["booking_code"],
        },
        data={"booking_id": str(bid), "type": "extra_charge"},
    )

    return ok(
        {"id": charge["id"], "amount": float(charge["amount"])},
        "Sent to the customer for approval.",
    )


@router.post("/jobs/{bid}/complete")
def complete_job(bid: int, body: CompleteIn, partner=Depends(get_approved_partner)):
    booking = _owned_job(bid, partner["id"], allowed=("in_progress",))

    require_photos = pricing.get_config("require_completion_photos").get(
        "require_completion_photos", True
    )
    if require_photos and not body.after_photos:
        fail("After-work photos are required.", "PHOTOS_REQUIRED")

    pending = db.fetch_value(
        """
        select count(*) from booking_extra_charges
         where booking_id = :b and approved_by_user = false and rejected = false
        """,
        {"b": bid},
    )
    if pending and int(pending) > 0:
        fail(
            "The customer has not responded to the extra charge yet. Please clear it first.",
            "EXTRA_CHARGE_PENDING",
        )

    final = pricing.recalculate_booking(bid)
    total = float(final["total"])

    db.execute(
        """
        update bookings
           set before_photos = :bp, after_photos = :ap,
               partner_notes = :n, completed_at = now()
         where id = :id
        """,
        {"bp": body.before_photos, "ap": body.after_photos, "n": body.notes, "id": bid},
    )

    # COD gets marked paid on collection; online was already paid
    is_cod = booking["payment_mode"] == "cod"
    if is_cod and body.cash_collected > 0:
        db.execute("update bookings set payment_status = 'paid' where id = :id", {"id": bid})

    new_status = "paid" if (not is_cod or body.cash_collected > 0) else "completed"
    _advance(bid, booking["status"], new_status, partner["id"], "Job completed")

    earning = _create_earning(bid, partner, total, body.cash_collected if is_cod else 0)

    db.execute(
        "update partners set jobs_completed = jobs_completed + 1 where id = :id",
        {"id": partner["id"]},
    )

    fcm.notify_user(
        booking["user_id"], "job_completed",
        variables={
            "service": db.fetch_value(
                "select string_agg(service_name, ', ') from booking_items where booking_id = :b",
                {"b": bid},
            ) or "Service",
            "code": booking["booking_code"],
        },
        data={"booking_id": str(bid), "type": "completed"},
    )

    return ok(
        {
            "total": total,
            "gross": float(earning["gross"]),
            "commission": float(earning["commission_amount"]),
            "your_earning": float(earning["net_payable"]),
        },
        "Well done! Job completed.",
    )


# ==================================================================
# EARNINGS & PAYOUT
# ==================================================================
@router.get("/earnings")
def earnings(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    page: int = 1,
    limit: int = 20,
    partner=Depends(get_approved_partner),
):
    pg = Pagination(page, limit)
    where = "e.partner_id = :p"
    params: Dict[str, Any] = {"p": partner["id"], "l": pg.limit, "o": pg.offset}
    if from_date:
        where += " and e.created_at >= :f"
        params["f"] = from_date
    if to_date:
        where += " and e.created_at < (:t::date + 1)"
        params["t"] = to_date

    rows = db.fetch_all(
        f"""
        select e.*, b.booking_code, b.scheduled_date,
               (select string_agg(service_name, ', ') from booking_items
                 where booking_id = b.id) as services
          from partner_earnings e
          join bookings b on b.id = e.booking_id
         where {where}
         order by e.created_at desc
         limit :l offset :o
        """,
        params,
    )
    for r in rows:
        for k in ("gross", "commission_amount", "net_payable", "cash_collected",
                  "commission_percent"):
            r[k] = float(r[k])

    summary = db.fetch_one(
        f"""
        select coalesce(sum(net_payable), 0) as total,
               coalesce(sum(net_payable) filter (where settlement_status = 'settled'), 0)
                 as settled,
               coalesce(sum(net_payable) filter (where settlement_status <> 'settled'), 0)
                 as pending,
               coalesce(sum(cash_collected), 0) as cash_in_hand,
               count(*) as jobs
          from partner_earnings e where {where.replace(' and e.created_at >= :f','').replace(" and e.created_at < (:t::date + 1)",'')}
        """,
        {"p": partner["id"]},
    )

    return ok(
        {
            **pg.envelope(rows, int(db.fetch_value(
                f"select count(*) from partner_earnings e where {where}",
                {k: v for k, v in params.items() if k not in ("l", "o")},
            ) or 0)),
            "summary": {k: float(v) for k, v in summary.items()},
        }
    )


@router.post("/payouts")
def request_payout(body: PayoutRequestIn, partner=Depends(get_approved_partner)):
    available = pricing._num(
        db.fetch_value(
            """
            select coalesce(sum(net_payable), 0) - coalesce(sum(cash_collected), 0)
              from partner_earnings
             where partner_id = :p and settlement_status in ('pending','approved')
            """,
            {"p": partner["id"]},
        )
    )
    min_payout = pricing._num(
        pricing.get_config("min_payout_amount").get("min_payout_amount"), 500
    )

    if body.amount < min_payout:
        fail(f"The minimum payout request is ₹{min_payout:.0f}.", "BELOW_MINIMUM")
    if body.amount > available:
        fail(f"You only have ₹{available:.0f} available.", "INSUFFICIENT_BALANCE")
    if not partner.get("upi_id") and not partner.get("bank_account_number"):
        fail("Please add your UPI ID or bank details in your profile first.", "NO_PAYMENT_DETAILS")

    payout = db.execute(
        """
        insert into payouts (partner_id, amount, method)
        values (:p, :a, :m) returning *
        """,
        {"p": partner["id"], "a": body.amount, "m": body.method},
    )
    return ok(
        {"id": payout["id"], "amount": float(payout["amount"])},
        "Payout requested. It will be reviewed by the admin.",
    )


@router.get("/payouts")
def my_payouts(partner=Depends(get_approved_partner)):
    rows = db.fetch_all(
        "select *, status::text as status from payouts where partner_id = :p "
        "order by requested_at desc limit 50",
        {"p": partner["id"]},
    )
    for r in rows:
        r["amount"] = float(r["amount"])
    return ok(rows)


# ==================================================================
# PROFILE
# ==================================================================
@router.put("/profile")
def update_profile(body: ProfileUpdateIn, partner=Depends(get_current_partner)):
    updated = db.execute(
        """
        update partners
           set name = coalesce(:n, name),
               photo = coalesce(:ph, photo),
               skills = coalesce(:sk, skills),
               service_area_pincodes = coalesce(:pin, service_area_pincodes),
               upi_id = coalesce(:upi, upi_id),
               bank_account_name = coalesce(:ban, bank_account_name),
               bank_account_number = coalesce(:bacc, bank_account_number),
               bank_ifsc = coalesce(:ifsc, bank_ifsc),
               fcm_token = coalesce(:fcm, fcm_token)
         where id = :id
        returning *
        """,
        {
            "n": body.name, "ph": body.photo, "sk": body.skills,
            "pin": body.service_area_pincodes, "upi": body.upi_id,
            "ban": body.bank_account_name, "bacc": body.bank_account_number,
            "ifsc": body.bank_ifsc, "fcm": body.fcm_token, "id": partner["id"],
        },
    )
    return ok({"id": updated["id"], "name": updated["name"]}, "Profile updated")


@router.get("/reviews")
def my_reviews(partner=Depends(get_approved_partner)):
    rows = db.fetch_all(
        """
        select r.rating, r.comment, r.created_at, u.name as user_name,
               b.booking_code
          from reviews r
          join users u on u.id = r.user_id
          join bookings b on b.id = r.booking_id
         where r.partner_id = :p and r.is_visible
         order by r.created_at desc limit 50
        """,
        {"p": partner["id"]},
    )
    return ok(rows)


# ==================================================================
# HELPERS
# ==================================================================
def _owned_job(bid: int, partner_id: int, allowed: tuple) -> Dict[str, Any]:
    booking = db.fetch_one(
        """
        select *, status::text as status, payment_mode::text as payment_mode
          from bookings where id = :id and assigned_partner_id = :p
        """,
        {"id": bid, "p": partner_id},
    )
    if not booking:
        fail("This job is not assigned to you.", "NOT_YOUR_JOB", 403)
    if booking["status"] not in allowed:
        fail(
            f"This action is not allowed for the current job status ({booking['status']}).",
            "WRONG_STATUS",
        )
    return booking


def _advance(
    bid: int, from_status: str, to_status: str, partner_id: int, note: Optional[str] = None
) -> None:
    db.execute(
        "update bookings set status = cast(:s as booking_status) where id = :id",
        {"s": to_status, "id": bid},
    )
    db.execute(
        """
        insert into booking_status_history
          (booking_id, from_status, to_status, actor, actor_id, note)
        values (:b, cast(:f as booking_status), cast(:t as booking_status),
                'partner', :p, :n)
        """,
        {"b": bid, "f": from_status, "t": to_status, "p": partner_id, "n": note},
    )


def _create_earning(
    bid: int, partner: Dict[str, Any], gross: float, cash_collected: float
) -> Dict[str, Any]:
    """Commission percent is frozen at completion time so later config changes
    never rewrite history."""
    commission_pct = pricing._num(
        partner.get("commission_percent_override")
        or pricing.get_config("default_commission_percent").get("default_commission_percent"),
        20,
    )
    commission_amt = round(gross * commission_pct / 100, 2)
    net = round(gross - commission_amt, 2)

    existing = db.fetch_one("select * from partner_earnings where booking_id = :b", {"b": bid})
    if existing:
        return existing

    return db.execute(
        """
        insert into partner_earnings
          (partner_id, booking_id, gross, commission_percent, commission_amount,
           net_payable, cash_collected)
        values (:p, :b, :g, :cp, :ca, :n, :cash)
        returning *
        """,
        {
            "p": partner["id"], "b": bid, "g": gross, "cp": commission_pct,
            "ca": commission_amt, "n": net, "cash": cash_collected,
        },
    )


def _job_rows(where: str, params: Dict[str, Any], pg: Optional[Pagination] = None):
    limit_sql = ""
    if pg:
        limit_sql = " limit :l offset :o"
        params = {**params, "l": pg.limit, "o": pg.offset}

    rows = db.fetch_all(
        f"""
        select b.id, b.booking_code, b.status::text as status, b.scheduled_date,
               b.slot_label, b.total, b.payment_mode::text as payment_mode,
               b.payment_status::text as payment_status, b.addr_snapshot,
               u.name as customer_name, u.phone as customer_phone,
               (select string_agg(service_name, ', ') from booking_items
                 where booking_id = b.id) as services,
               e.net_payable
          from bookings b
          join users u on u.id = b.user_id
          left join partner_earnings e on e.booking_id = b.id
         where {where}
         order by b.scheduled_date desc, b.id desc{limit_sql}
        """,
        params,
    )
    for r in rows:
        r["total"] = float(r["total"])
        r["net_payable"] = float(r["net_payable"]) if r["net_payable"] else None

    if pg:
        total = db.fetch_value(
            f"select count(*) from bookings b where {where}",
            {k: v for k, v in params.items() if k not in ("l", "o")},
        )
        return pg.envelope(rows, int(total or 0))
    return rows


def _job_detail(bid: int, partner_id: int) -> Dict[str, Any]:
    booking = db.fetch_one(
        """
        select b.*, b.status::text as status, b.payment_mode::text as payment_mode,
               b.payment_status::text as payment_status,
               u.name as customer_name, u.phone as customer_phone
          from bookings b join users u on u.id = b.user_id
         where b.id = :id and b.assigned_partner_id = :p
        """,
        {"id": bid, "p": partner_id},
    )
    if not booking:
        fail("Job not found", "NOT_FOUND", 404)

    items = db.fetch_all("select * from booking_items where booking_id = :b", {"b": bid})
    extras = db.fetch_all(
        "select * from booking_extra_charges where booking_id = :b order by id", {"b": bid}
    )
    for i in items:
        i["unit_price"] = float(i["unit_price"])
        i["line_total"] = float(i["line_total"])
    for e in extras:
        e["amount"] = float(e["amount"])

    snap = booking["addr_snapshot"] or {}
    address_parts = [snap.get("house"), snap.get("area"), snap.get("landmark"),
                     snap.get("city"), snap.get("pincode")]
    full_address = ", ".join(p for p in address_parts if p)

    return {
        "id": booking["id"],
        "booking_code": booking["booking_code"],
        "status": booking["status"],
        "scheduled_date": str(booking["scheduled_date"]),
        "slot_label": booking["slot_label"],
        "customer_name": booking["customer_name"],
        "customer_phone": booking["customer_phone"],
        "address": full_address,
        "lat": snap.get("lat"),
        "lng": snap.get("lng"),
        "landmark": snap.get("landmark"),
        "items": items,
        "extra_charges": extras,
        "subtotal": float(booking["subtotal"]),
        "extra_charges_total": float(booking["extra_charges_total"]),
        "total": float(booking["total"]),
        "payment_mode": booking["payment_mode"],
        "payment_status": booking["payment_status"],
        "collect_cash": (
            booking["payment_mode"] == "cod" and booking["payment_status"] != "paid"
        ),
        "user_notes": booking["user_notes"],
        "next_action": _next_action(booking["status"]),
    }


def _next_action(status: str) -> Optional[str]:
    return {
        "assigned": "on_the_way",
        "partner_on_the_way": "arrived",
        "arrived": "start",
        "in_progress": "complete",
    }.get(status)
