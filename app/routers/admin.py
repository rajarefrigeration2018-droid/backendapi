# app/routers/admin.py
"""
Admin APIs — part 1: dashboard, bookings, partners, users.

Every endpoint is guarded by a granular permission checked against the
admin's `permissions` jsonb column. A super_admin passes everything.
"""

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core import fcm
from app.database import db
from app.dependencies import Pagination, fail, ok, require_permission
from app.services import assignment, pricing

router = APIRouter(prefix="/admin", tags=["Admin"])


# ==================================================================
# DASHBOARD
# ==================================================================
@router.get("/dashboard")
def dashboard(admin=Depends(require_permission("dashboard"))):
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    revenue = db.fetch_one(
        """
        select
          coalesce(sum(total) filter (where scheduled_date = :today), 0) as today,
          coalesce(sum(total) filter (where scheduled_date >= :week), 0)  as week,
          coalesce(sum(total) filter (where scheduled_date >= :month), 0) as month,
          coalesce(sum(total), 0) as all_time
        from bookings
        where status in ('completed','paid')
        """,
        {"today": today, "week": week_start, "month": month_start},
    )

    counts = db.fetch_one(
        """
        select
          count(*) filter (where status = 'pending')            as pending,
          count(*) filter (where status = 'confirmed')          as confirmed,
          count(*) filter (where status = 'assigned')           as assigned,
          count(*) filter (where status in ('partner_on_the_way','arrived','in_progress'))
                                                                as in_progress,
          count(*) filter (where status in ('completed','paid')) as completed,
          count(*) filter (where status in ('cancelled','rejected')) as cancelled,
          count(*) filter (where scheduled_date = :today)       as today_total
        from bookings
        """,
        {"today": today},
    )

    unassigned = db.fetch_value(
        """
        select count(*) from bookings
         where status = 'confirmed' and assigned_partner_id is null
        """
    )

    people = db.fetch_one(
        """
        select
          (select count(*) from users where not is_blocked)                as users,
          (select count(*) from users
            where created_at >= :month and not is_blocked)                 as new_users,
          (select count(*) from partners where status = 'approved')        as partners,
          (select count(*) from partners where status = 'pending')         as pending_partners,
          (select count(*) from partners
            where is_online and status = 'approved')                       as online_partners
        """,
        {"month": month_start},
    )

    money = db.fetch_one(
        """
        select
          (select coalesce(sum(net_payable), 0) from partner_earnings
            where settlement_status in ('pending','approved'))     as unsettled_earnings,
          (select count(*) from payouts where status = 'requested') as payout_requests,
          (select coalesce(sum(amount), 0) from payouts
            where status = 'requested')                            as payout_amount
        """
    )

    top_services = db.fetch_all(
        """
        select bi.service_name, count(*) as bookings,
               coalesce(sum(bi.line_total), 0) as revenue
          from booking_items bi
          join bookings b on b.id = bi.booking_id
         where b.status in ('completed','paid') and b.scheduled_date >= :month
         group by bi.service_name
         order by bookings desc limit 5
        """,
        {"month": month_start},
    )

    top_partners = db.fetch_all(
        """
        select p.id, p.name, p.rating_avg, count(e.id) as jobs,
               coalesce(sum(e.net_payable), 0) as earned
          from partners p
          join partner_earnings e on e.partner_id = p.id
         where e.created_at >= :month
         group by p.id
         order by jobs desc limit 5
        """,
        {"month": month_start},
    )

    low_stock = db.fetch_all(
        """
        select id, name, stock_qty, min_stock_alert from parts
         where is_active and stock_qty <= min_stock_alert
         order by stock_qty asc limit 10
        """
    )

    daily = db.fetch_all(
        """
        select scheduled_date::text as day, count(*) as bookings,
               coalesce(sum(total) filter (where status in ('completed','paid')), 0) as revenue
          from bookings
         where scheduled_date >= :from_date
         group by scheduled_date order by scheduled_date
        """,
        {"from_date": today - timedelta(days=29)},
    )

    return ok(
        {
            "revenue": {k: float(v) for k, v in revenue.items()},
            "bookings": counts,
            "unassigned": int(unassigned or 0),
            "people": people,
            "money": {k: float(v) if k != "payout_requests" else v for k, v in money.items()},
            "top_services": [
                {**t, "revenue": float(t["revenue"])} for t in top_services
            ],
            "top_partners": [
                {**t, "earned": float(t["earned"]), "rating_avg": float(t["rating_avg"] or 0)}
                for t in top_partners
            ],
            "low_stock": low_stock,
            "daily_chart": [
                {**d, "revenue": float(d["revenue"])} for d in daily
            ],
        }
    )


@router.get("/live-map")
def live_map(admin=Depends(require_permission("dashboard"))):
    """Online technicians and today's active jobs, for the dashboard map."""
    partners = db.fetch_all(
        """
        select id, name, photo, current_lat as lat, current_lng as lng,
               location_updated_at, rating_avg,
               (select count(*) from bookings b
                 where b.assigned_partner_id = p.id
                   and b.status in ('assigned','partner_on_the_way','arrived','in_progress')
               ) as active_jobs
          from partners p
         where is_online and status = 'approved' and current_lat is not null
        """
    )
    jobs = db.fetch_all(
        """
        select id, booking_code, status::text as status, addr_snapshot,
               assigned_partner_id
          from bookings
         where status in ('confirmed','assigned','partner_on_the_way','arrived','in_progress')
           and scheduled_date = current_date
        """
    )
    for p in partners:
        p["rating_avg"] = float(p["rating_avg"] or 0)
    return ok({"partners": partners, "jobs": jobs})


# ==================================================================
# BOOKINGS
# ==================================================================
@router.get("/bookings")
def list_bookings(
    status: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    partner_id: Optional[int] = None,
    user_id: Optional[int] = None,
    city: Optional[str] = None,
    unassigned: bool = False,
    search: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    admin=Depends(require_permission("bookings")),
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}

    if status:
        where.append("b.status = cast(:st as booking_status)")
        params["st"] = status
    if from_date:
        where.append("b.scheduled_date >= :fd")
        params["fd"] = from_date
    if to_date:
        where.append("b.scheduled_date <= :td")
        params["td"] = to_date
    if partner_id:
        where.append("b.assigned_partner_id = :pid")
        params["pid"] = partner_id
    if user_id:
        where.append("b.user_id = :uid")
        params["uid"] = user_id
    if city:
        where.append("b.addr_snapshot->>'city' ilike :city")
        params["city"] = f"%{city}%"
    if unassigned:
        where.append("b.assigned_partner_id is null and b.status = 'confirmed'")
    if search:
        where.append("(b.booking_code ilike :q or u.phone ilike :q or u.name ilike :q)")
        params["q"] = f"%{search}%"

    clause = " and ".join(where)

    rows = db.fetch_all(
        f"""
        select b.id, b.booking_code, b.status::text as status, b.scheduled_date,
               b.slot_label, b.total, b.payment_mode::text as payment_mode,
               b.payment_status::text as payment_status, b.created_at,
               b.addr_snapshot, b.assigned_partner_id,
               u.id as user_id, u.name as user_name, u.phone as user_phone,
               p.name as partner_name, p.phone as partner_phone,
               (select string_agg(service_name, ', ') from booking_items
                 where booking_id = b.id) as services
          from bookings b
          join users u on u.id = b.user_id
          left join partners p on p.id = b.assigned_partner_id
         where {clause}
         order by b.created_at desc
         limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"""
        select count(*) from bookings b join users u on u.id = b.user_id where {clause}
        """,
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    for r in rows:
        r["total"] = float(r["total"])

    return ok(pg.envelope(rows, int(total or 0)))


@router.get("/bookings/{bid}")
def booking_detail(bid: int, admin=Depends(require_permission("bookings"))):
    booking = db.fetch_one(
        """
        select b.*, b.status::text as status,
               b.payment_mode::text as payment_mode,
               b.payment_status::text as payment_status,
               u.name as user_name, u.phone as user_phone, u.email as user_email,
               p.name as partner_name, p.phone as partner_phone, p.photo as partner_photo
          from bookings b
          join users u on u.id = b.user_id
          left join partners p on p.id = b.assigned_partner_id
         where b.id = :id
        """,
        {"id": bid},
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)

    items = db.fetch_all("select * from booking_items where booking_id = :b", {"b": bid})
    extras = db.fetch_all(
        "select * from booking_extra_charges where booking_id = :b order by id", {"b": bid}
    )
    timeline = db.fetch_all(
        """
        select to_status::text as status, actor::text as actor, actor_id, note, created_at
          from booking_status_history where booking_id = :b order by created_at
        """,
        {"b": bid},
    )
    offers = db.fetch_all(
        """
        select o.*, p.name as partner_name
          from booking_offers o join partners p on p.id = o.partner_id
         where o.booking_id = :b order by o.offered_at
        """,
        {"b": bid},
    )
    earning = db.fetch_one("select * from partner_earnings where booking_id = :b", {"b": bid})
    payment = db.fetch_one(
        "select * from payments where booking_id = :b order by id desc limit 1", {"b": bid}
    )
    review = db.fetch_one("select * from reviews where booking_id = :b", {"b": bid})

    for i in items:
        i["unit_price"] = float(i["unit_price"])
        i["line_total"] = float(i["line_total"])
    for e in extras:
        e["amount"] = float(e["amount"])

    return ok(
        {
            "booking": _money(booking),
            "items": items,
            "extra_charges": extras,
            "timeline": timeline,
            "offers": offers,
            "earning": _money(earning) if earning else None,
            "payment": _money(payment) if payment else None,
            "review": review,
            "start_otp": booking["otp_start"],
        }
    )


class AssignIn(BaseModel):
    partner_id: int


@router.post("/bookings/{bid}/assign")
def assign_partner(bid: int, body: AssignIn, admin=Depends(require_permission("bookings"))):
    result = assignment.manual_assign(bid, body.partner_id, admin["id"])
    if not result["success"]:
        fail(result["message"], "ASSIGN_FAILED")
    return ok(None, result["message"])


@router.post("/bookings/{bid}/auto-assign")
def retry_auto_assign(bid: int, admin=Depends(require_permission("bookings"))):
    result = assignment.offer_to_partners(bid)
    if result["offered"] == 0:
        fail(
            f"No technician available right now ({result.get('reason')}).",
            "NO_PARTNERS",
        )
    return ok(result, f"Job offered to {result['offered']} technician(s).")


@router.get("/bookings/{bid}/candidates")
def assignment_candidates(bid: int, admin=Depends(require_permission("bookings"))):
    """Ranked list an admin can pick from when assigning manually."""
    rows = assignment.find_candidates(bid, radius_km=9999)
    for r in rows:
        r["distance_km"] = round(float(r["distance_km"]), 2)
        r["rating_avg"] = float(r["rating_avg"] or 0)
        r.pop("fcm_token", None)
    return ok(rows)


class StatusChangeIn(BaseModel):
    status: str
    note: Optional[str] = None


@router.post("/bookings/{bid}/status")
def force_status(bid: int, body: StatusChangeIn, admin=Depends(require_permission("bookings"))):
    """Admin override — use sparingly. Always recorded in the timeline."""
    valid = {
        "pending", "confirmed", "assigned", "partner_on_the_way", "arrived",
        "in_progress", "completed", "paid", "cancelled", "rejected", "rescheduled",
    }
    if body.status not in valid:
        fail("Invalid status", "INVALID_STATUS")

    current = db.fetch_value("select status::text from bookings where id = :id", {"id": bid})
    if not current:
        fail("Booking not found", "NOT_FOUND", 404)

    db.execute(
        "update bookings set status = cast(:s as booking_status) where id = :id",
        {"s": body.status, "id": bid},
    )
    db.execute(
        """
        insert into booking_status_history
          (booking_id, from_status, to_status, actor, actor_id, note)
        values (:b, cast(:f as booking_status), cast(:t as booking_status),
                'admin', :a, :n)
        """,
        {"b": bid, "f": current, "t": body.status, "a": admin["id"],
         "n": body.note or "Changed by admin"},
    )
    _audit(admin, "force_status", "bookings", bid, {"status": current}, {"status": body.status})
    return ok(None, f"Status changed to {body.status}")


class AdminChargeIn(BaseModel):
    label: str
    amount: float


@router.post("/bookings/{bid}/charges")
def add_charge(bid: int, body: AdminChargeIn, admin=Depends(require_permission("bookings"))):
    """Admin-added charges are auto-approved (no customer prompt)."""
    db.execute(
        """
        insert into booking_extra_charges (booking_id, label, amount, approved_by_user, approved_at)
        values (:b, :l, :a, true, now())
        """,
        {"b": bid, "l": body.label, "a": body.amount},
    )
    updated = pricing.recalculate_booking(bid)
    return ok({"new_total": float(updated["total"])}, "Charge added")


class CancelIn(BaseModel):
    reason: str
    refund_to_wallet: bool = True


@router.post("/bookings/{bid}/cancel")
def admin_cancel(bid: int, body: CancelIn, admin=Depends(require_permission("bookings"))):
    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id", {"id": bid}
    )
    if not booking:
        fail("Booking not found", "NOT_FOUND", 404)
    if booking["status"] in ("cancelled", "rejected"):
        fail("Already cancelled", "ALREADY_CANCELLED")

    db.execute(
        """
        update bookings
           set status = 'cancelled', cancelled_at = now(), cancelled_by = 'admin',
               cancel_reason = :r
         where id = :id
        """,
        {"r": body.reason, "id": bid},
    )
    db.execute(
        """
        insert into booking_status_history
          (booking_id, from_status, to_status, actor, actor_id, note)
        values (:b, cast(:f as booking_status), 'cancelled', 'admin', :a, :n)
        """,
        {"b": bid, "f": booking["status"], "a": admin["id"], "n": body.reason},
    )
    pricing.release_coupon(bid)

    refund = 0.0
    if body.refund_to_wallet and booking["payment_status"] == "paid":
        refund = float(booking["total"])
        updated = db.execute(
            "update users set wallet_balance = wallet_balance + :a where id = :u "
            "returning wallet_balance",
            {"a": refund, "u": booking["user_id"]},
        )
        db.execute(
            """
            insert into wallet_transactions
              (owner_type, owner_id, direction, amount, balance_after, reason,
               ref_type, ref_id, created_by)
            values ('user', :u, 'credit', :a, :b, :r, 'booking', :ref, :admin)
            """,
            {"u": booking["user_id"], "a": refund, "b": updated["wallet_balance"],
             "r": f"Refund for {booking['booking_code']}", "ref": bid, "admin": admin["id"]},
        )

    if booking["assigned_partner_id"]:
        fcm.notify_partner(
            booking["assigned_partner_id"], "booking_cancelled",
            variables={"code": booking["booking_code"]},
            data={"booking_id": str(bid)},
        )

    _audit(admin, "cancel", "bookings", bid, booking, None)
    return ok({"refunded": refund}, "Booking cancelled")


# ==================================================================
# PARTNERS
# ==================================================================
@router.get("/partners")
def list_partners(
    status: Optional[str] = None,
    is_online: Optional[bool] = None,
    pincode: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    admin=Depends(require_permission("partners")),
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}

    if status:
        where.append("p.status = cast(:st as partner_status)")
        params["st"] = status
    if is_online is not None:
        where.append("p.is_online = :on")
        params["on"] = is_online
    if pincode:
        where.append(":pin = any(p.service_area_pincodes)")
        params["pin"] = pincode
    if search:
        where.append("(p.name ilike :q or p.phone ilike :q)")
        params["q"] = f"%{search}%"

    clause = " and ".join(where)
    rows = db.fetch_all(
        f"""
        select p.id, p.name, p.phone, p.photo, p.status::text as status,
               p.is_online, p.skills, p.service_area_pincodes, p.rating_avg,
               p.rating_count, p.jobs_completed, p.commission_percent_override,
               p.created_at, p.approved_at,
               (select coalesce(sum(net_payable), 0) from partner_earnings e
                 where e.partner_id = p.id) as total_earned,
               (select count(*) from partner_documents d
                 where d.partner_id = p.id) as doc_count
          from partners p
         where {clause}
         order by p.created_at desc
         limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from partners p where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    for r in rows:
        r["rating_avg"] = float(r["rating_avg"] or 0)
        r["total_earned"] = float(r["total_earned"])

    return ok(pg.envelope(rows, int(total or 0)))


@router.get("/partners/{pid}")
def partner_detail(pid: int, admin=Depends(require_permission("partners"))):
    partner = db.fetch_one(
        "select *, status::text as status from partners where id = :id", {"id": pid}
    )
    if not partner:
        fail("Partner not found", "NOT_FOUND", 404)

    docs = db.fetch_all(
        "select * from partner_documents where partner_id = :p order by id", {"p": pid}
    )
    skills = db.fetch_all(
        "select id, name from categories where id = any(:s)",
        {"s": partner["skills"] or []},
    )
    recent_jobs = db.fetch_all(
        """
        select b.id, b.booking_code, b.status::text as status, b.scheduled_date,
               b.total, e.net_payable
          from bookings b
          left join partner_earnings e on e.booking_id = b.id
         where b.assigned_partner_id = :p
         order by b.scheduled_date desc limit 20
        """,
        {"p": pid},
    )
    earnings = db.fetch_one(
        """
        select coalesce(sum(net_payable), 0) as total,
               coalesce(sum(net_payable) filter (where settlement_status = 'settled'), 0) as settled,
               coalesce(sum(net_payable) filter (where settlement_status <> 'settled'), 0) as pending,
               coalesce(sum(cash_collected), 0) as cash_collected,
               count(*) as jobs
          from partner_earnings where partner_id = :p
        """,
        {"p": pid},
    )
    reviews = db.fetch_all(
        """
        select r.rating, r.comment, r.created_at, u.name as user_name
          from reviews r join users u on u.id = r.user_id
         where r.partner_id = :p and r.is_visible
         order by r.created_at desc limit 10
        """,
        {"p": pid},
    )

    partner.pop("fcm_token", None)
    for j in recent_jobs:
        j["total"] = float(j["total"])
        j["net_payable"] = float(j["net_payable"]) if j["net_payable"] else None

    return ok(
        {
            "partner": _money(partner),
            "documents": docs,
            "skills": skills,
            "recent_jobs": recent_jobs,
            "earnings": {k: float(v) for k, v in earnings.items()},
            "reviews": reviews,
        }
    )


class PartnerApprovalIn(BaseModel):
    approve: bool
    reason: Optional[str] = None


@router.post("/partners/{pid}/approval")
def approve_partner(
    pid: int, body: PartnerApprovalIn, admin=Depends(require_permission("partners"))
):
    partner = db.fetch_one(
        "select *, status::text as status from partners where id = :id", {"id": pid}
    )
    if not partner:
        fail("Partner not found", "NOT_FOUND", 404)

    if body.approve:
        if not partner.get("name"):
            fail("This technician has not completed registration yet.", "INCOMPLETE_PROFILE")
        db.execute(
            """
            update partners
               set status = 'approved', approved_by = :a, approved_at = now(),
                   reject_reason = null
             where id = :id
            """,
            {"a": admin["id"], "id": pid},
        )
        fcm.notify_partner(pid, "partner_approved")
        msg = "Technician approved"
    else:
        db.execute(
            "update partners set status = 'rejected', reject_reason = :r, is_online = false "
            "where id = :id",
            {"r": body.reason, "id": pid},
        )
        fcm.notify_partner(pid, "partner_rejected")
        msg = "Technician rejected"

    _audit(admin, "approval", "partners", pid, {"status": partner["status"]},
           {"status": "approved" if body.approve else "rejected"})
    return ok(None, msg)


class PartnerUpdateIn(BaseModel):
    skills: Optional[List[int]] = None
    service_area_pincodes: Optional[List[str]] = None
    commission_percent_override: Optional[float] = None
    status: Optional[str] = None


@router.put("/partners/{pid}")
def update_partner(pid: int, body: PartnerUpdateIn, admin=Depends(require_permission("partners"))):
    before = db.fetch_one(
        "select *, status::text as status from partners where id = :id", {"id": pid}
    )
    if not before:
        fail("Partner not found", "NOT_FOUND", 404)

    row = db.execute(
        """
        update partners
           set skills = coalesce(:sk, skills),
               service_area_pincodes = coalesce(:pin, service_area_pincodes),
               commission_percent_override = coalesce(:com, commission_percent_override),
               status = coalesce(cast(:st as partner_status), status),
               is_online = case when :st = 'suspended' then false else is_online end
         where id = :id
        returning id, name, status::text as status
        """,
        {
            "sk": body.skills, "pin": body.service_area_pincodes,
            "com": body.commission_percent_override, "st": body.status, "id": pid,
        },
    )
    _audit(admin, "update", "partners", pid, before, row)
    return ok(row, "Technician updated")


@router.post("/partners/{pid}/documents/{doc_id}/verify")
def verify_document(pid: int, doc_id: int, admin=Depends(require_permission("partners"))):
    db.execute(
        """
        update partner_documents
           set verified = true, verified_by = :a, verified_at = now()
         where id = :d and partner_id = :p
        """,
        {"a": admin["id"], "d": doc_id, "p": pid},
    )
    return ok(None, "Document verified")


# ==================================================================
# USERS
# ==================================================================
@router.get("/users")
def list_users(
    search: Optional[str] = None,
    blocked: Optional[bool] = None,
    page: int = 1,
    limit: int = 20,
    admin=Depends(require_permission("users")),
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}

    if search:
        where.append("(u.name ilike :q or u.phone ilike :q or u.email ilike :q)")
        params["q"] = f"%{search}%"
    if blocked is not None:
        where.append("u.is_blocked = :b")
        params["b"] = blocked

    clause = " and ".join(where)
    rows = db.fetch_all(
        f"""
        select u.id, u.name, u.phone, u.email, u.wallet_balance, u.is_blocked,
               u.referral_code, u.created_at, u.last_login_at,
               (select count(*) from bookings b where b.user_id = u.id) as bookings,
               (select coalesce(sum(total), 0) from bookings b
                 where b.user_id = u.id and b.status in ('completed','paid')) as spent
          from users u
         where {clause}
         order by u.created_at desc
         limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from users u where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    for r in rows:
        r["wallet_balance"] = float(r["wallet_balance"])
        r["spent"] = float(r["spent"])

    return ok(pg.envelope(rows, int(total or 0)))


@router.get("/users/{uid}")
def user_detail(uid: int, admin=Depends(require_permission("users"))):
    user = db.fetch_one("select * from users where id = :id", {"id": uid})
    if not user:
        fail("User not found", "NOT_FOUND", 404)

    addresses = db.fetch_all(
        "select * from user_addresses where user_id = :u order by is_default desc", {"u": uid}
    )
    bookings = db.fetch_all(
        """
        select b.id, b.booking_code, b.status::text as status, b.scheduled_date,
               b.total, p.name as partner_name,
               (select string_agg(service_name, ', ') from booking_items
                 where booking_id = b.id) as services
          from bookings b
          left join partners p on p.id = b.assigned_partner_id
         where b.user_id = :u
         order by b.created_at desc limit 30
        """,
        {"u": uid},
    )
    wallet = db.fetch_all(
        """
        select direction::text as direction, amount, balance_after, reason, created_at
          from wallet_transactions
         where owner_type = 'user' and owner_id = :u
         order by created_at desc limit 50
        """,
        {"u": uid},
    )
    stats = db.fetch_one(
        """
        select count(*) as total_bookings,
               count(*) filter (where status in ('completed','paid')) as completed,
               count(*) filter (where status in ('cancelled','rejected')) as cancelled,
               coalesce(sum(total) filter (where status in ('completed','paid')), 0) as spent
          from bookings where user_id = :u
        """,
        {"u": uid},
    )

    user.pop("fcm_token", None)
    for b in bookings:
        b["total"] = float(b["total"])
    for w in wallet:
        w["amount"] = float(w["amount"])
        w["balance_after"] = float(w["balance_after"])

    return ok(
        {
            "user": _money(user),
            "addresses": addresses,
            "bookings": bookings,
            "wallet_transactions": wallet,
            "stats": {k: float(v) if k == "spent" else v for k, v in stats.items()},
        }
    )


class WalletAdjustIn(BaseModel):
    amount: float = Field(..., gt=0)
    direction: str = "credit"      # credit | debit
    reason: str


@router.post("/users/{uid}/wallet")
def adjust_wallet(uid: int, body: WalletAdjustIn, admin=Depends(require_permission("users"))):
    if body.direction not in ("credit", "debit"):
        fail("Direction must be credit or debit", "INVALID_DIRECTION")

    user = db.fetch_one("select wallet_balance from users where id = :id", {"id": uid})
    if not user:
        fail("User not found", "NOT_FOUND", 404)

    if body.direction == "debit" and float(user["wallet_balance"]) < body.amount:
        fail("Insufficient wallet balance", "INSUFFICIENT_BALANCE")

    delta = body.amount if body.direction == "credit" else -body.amount
    updated = db.execute(
        "update users set wallet_balance = wallet_balance + :d where id = :id "
        "returning wallet_balance",
        {"d": delta, "id": uid},
    )
    db.execute(
        """
        insert into wallet_transactions
          (owner_type, owner_id, direction, amount, balance_after, reason, created_by)
        values ('user', :u, cast(:dir as txn_direction), :a, :b, :r, :admin)
        """,
        {"u": uid, "dir": body.direction, "a": body.amount,
         "b": updated["wallet_balance"], "r": body.reason, "admin": admin["id"]},
    )
    _audit(admin, f"wallet_{body.direction}", "users", uid, None, {"amount": body.amount})
    return ok(
        {"new_balance": float(updated["wallet_balance"])},
        f"₹{body.amount:.0f} {body.direction}ed",
    )


class BlockIn(BaseModel):
    block: bool
    reason: Optional[str] = None


@router.post("/users/{uid}/block")
def block_user(uid: int, body: BlockIn, admin=Depends(require_permission("users"))):
    db.execute(
        "update users set is_blocked = :b, block_reason = :r where id = :id",
        {"b": body.block, "r": body.reason if body.block else None, "id": uid},
    )
    _audit(admin, "block" if body.block else "unblock", "users", uid, None,
           {"reason": body.reason})
    return ok(None, "User blocked" if body.block else "User unblocked")


# ==================================================================
# HELPERS
# ==================================================================
def _money(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    keys = (
        "subtotal", "visit_charge", "discount", "tax", "total", "extra_charges_total",
        "cancellation_fee", "wallet_balance", "amount", "gross", "commission_amount",
        "commission_percent", "net_payable", "cash_collected", "rating_avg",
    )
    out = dict(row)
    for k in keys:
        if k in out and out[k] is not None:
            out[k] = float(out[k])
    return out


def _audit(admin: Dict[str, Any], action: str, entity: str, entity_id: Any, before, after) -> None:
    db.execute(
        """
        insert into audit_logs (actor_type, actor_id, action, entity, entity_id, before, after)
        values ('admin', :aid, :act, :ent, :eid, cast(:b as jsonb), cast(:a as jsonb))
        """,
        {
            "aid": admin["id"], "act": action, "ent": entity, "eid": entity_id,
            "b": json.dumps(before, default=str) if before else None,
            "a": json.dumps(after, default=str) if after else None,
        },
    )
