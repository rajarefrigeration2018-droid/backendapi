# app/routers/admin_extra.py
"""
Admin APIs — part 2: coupons, payouts, notifications, reviews,
support tickets, reports, sub-admins and the audit log.
"""

import csv
import io
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core import fcm
from app.core.security import hash_password
from app.database import db
from app.dependencies import (
    Pagination,
    fail,
    get_current_admin,
    ok,
    require_permission,
    require_super_admin,
)

router = APIRouter(prefix="/admin", tags=["Admin Extra"])


# ==================================================================
# COUPONS
# ==================================================================
class CouponIn(BaseModel):
    code: str
    title: Optional[str] = None
    description: Optional[str] = None
    type: str = "percent"                  # percent | flat
    value: float
    max_discount: Optional[float] = None
    min_order: float = 0
    usage_limit: Optional[int] = None
    per_user_limit: int = 1
    applicable_service_ids: List[int] = []
    applicable_category_ids: List[int] = []
    first_order_only: bool = False
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    is_active: bool = True


@router.get("/coupons")
def list_coupons(admin=Depends(require_permission("coupons"))):
    rows = db.fetch_all(
        """
        select c.*, c.type::text as type,
               (select count(*) from coupon_redemptions r where r.coupon_id = c.id) as redemptions,
               (select coalesce(sum(discount), 0) from coupon_redemptions r
                 where r.coupon_id = c.id) as total_discount
          from coupons c order by c.id desc
        """
    )
    for r in rows:
        for k in ("value", "max_discount", "min_order", "total_discount"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return ok(rows)


@router.post("/coupons")
def create_coupon(body: CouponIn, admin=Depends(require_permission("coupons"))):
    if db.fetch_one("select 1 from coupons where upper(code) = upper(:c)", {"c": body.code}):
        fail("This coupon code already exists", "DUPLICATE_CODE", 409)
    if body.type not in ("percent", "flat"):
        fail("Type must be percent or flat", "INVALID_TYPE")

    row = db.execute(
        """
        insert into coupons (code, title, description, type, value, max_discount, min_order,
                             usage_limit, per_user_limit, applicable_service_ids,
                             applicable_category_ids, first_order_only, valid_from,
                             valid_to, is_active)
        values (upper(:code), :t, :d, cast(:ty as coupon_type), :v, :md, :mo, :ul, :pul,
                :svc, :cat, :fo, coalesce(:vf, now()), :vt, :a)
        returning *
        """,
        {
            "code": body.code, "t": body.title, "d": body.description, "ty": body.type,
            "v": body.value, "md": body.max_discount, "mo": body.min_order,
            "ul": body.usage_limit, "pul": body.per_user_limit,
            "svc": body.applicable_service_ids, "cat": body.applicable_category_ids,
            "fo": body.first_order_only, "vf": body.valid_from, "vt": body.valid_to,
            "a": body.is_active,
        },
    )
    return ok(row, "Coupon created")


@router.put("/coupons/{cid}")
def update_coupon(cid: int, body: CouponIn, admin=Depends(require_permission("coupons"))):
    row = db.execute(
        """
        update coupons
           set code=upper(:code), title=:t, description=:d, type=cast(:ty as coupon_type),
               value=:v, max_discount=:md, min_order=:mo, usage_limit=:ul,
               per_user_limit=:pul, applicable_service_ids=:svc,
               applicable_category_ids=:cat, first_order_only=:fo,
               valid_from=coalesce(:vf, valid_from), valid_to=:vt, is_active=:a
         where id = :id
        returning *
        """,
        {
            "code": body.code, "t": body.title, "d": body.description, "ty": body.type,
            "v": body.value, "md": body.max_discount, "mo": body.min_order,
            "ul": body.usage_limit, "pul": body.per_user_limit,
            "svc": body.applicable_service_ids, "cat": body.applicable_category_ids,
            "fo": body.first_order_only, "vf": body.valid_from, "vt": body.valid_to,
            "a": body.is_active, "id": cid,
        },
    )
    if not row:
        fail("Coupon not found", "NOT_FOUND", 404)
    return ok(row, "Coupon updated")


@router.delete("/coupons/{cid}")
def delete_coupon(cid: int, admin=Depends(require_permission("coupons"))):
    used = db.fetch_value(
        "select count(*) from coupon_redemptions where coupon_id = :id", {"id": cid}
    )
    if used and int(used) > 0:
        db.execute("update coupons set is_active = false where id = :id", {"id": cid})
        return ok(None, "Coupon has been used before, so it was deactivated instead of deleted.")
    db.execute("delete from coupons where id = :id", {"id": cid})
    return ok(None, "Coupon deleted")


@router.get("/coupons/{cid}/usage")
def coupon_usage(cid: int, admin=Depends(require_permission("coupons"))):
    rows = db.fetch_all(
        """
        select r.discount, r.created_at, u.name as user_name, u.phone,
               b.booking_code, b.total
          from coupon_redemptions r
          join users u on u.id = r.user_id
          left join bookings b on b.id = r.booking_id
         where r.coupon_id = :c
         order by r.created_at desc limit 200
        """,
        {"c": cid},
    )
    for r in rows:
        r["discount"] = float(r["discount"])
        if r.get("total") is not None:
            r["total"] = float(r["total"])
    return ok(rows)


# ==================================================================
# PAYOUTS & SETTLEMENTS
# ==================================================================
@router.get("/payouts")
def list_payouts(
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    admin=Depends(require_permission("payouts")),
):
    pg = Pagination(page, limit)
    where = "1=1"
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}
    if status:
        where += " and po.status = cast(:st as payout_status)"
        params["st"] = status

    rows = db.fetch_all(
        f"""
        select po.*, po.status::text as status, p.name as partner_name, p.phone,
               p.upi_id, p.bank_account_name, p.bank_account_number, p.bank_ifsc
          from payouts po join partners p on p.id = po.partner_id
         where {where}
         order by po.requested_at desc
         limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from payouts po where {where}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    for r in rows:
        r["amount"] = float(r["amount"])
    return ok(pg.envelope(rows, int(total or 0)))


class PayoutActionIn(BaseModel):
    action: str                    # approve | reject | paid
    utr: Optional[str] = None
    note: Optional[str] = None


@router.post("/payouts/{pid}/action")
def process_payout(pid: int, body: PayoutActionIn, admin=Depends(require_permission("payouts"))):
    payout = db.fetch_one(
        "select *, status::text as status from payouts where id = :id", {"id": pid}
    )
    if not payout:
        fail("Payout not found", "NOT_FOUND", 404)

    if body.action == "approve":
        if payout["status"] != "requested":
            fail("Only a requested payout can be approved", "WRONG_STATUS")
        db.execute(
            "update payouts set status = 'approved', processed_by = :a, note = :n where id = :id",
            {"a": admin["id"], "n": body.note, "id": pid},
        )
        msg = "Payout approved"

    elif body.action == "paid":
        if payout["status"] not in ("requested", "approved"):
            fail("This payout cannot be marked as paid", "WRONG_STATUS")
        if not body.utr:
            fail("UTR / reference number is required", "UTR_REQUIRED")

        db.execute(
            """
            update payouts
               set status = 'paid', utr = :utr, paid_at = now(),
                   processed_by = :a, note = :n
             where id = :id
            """,
            {"utr": body.utr, "a": admin["id"], "n": body.note, "id": pid},
        )
        # settle the earnings this payout covers, oldest first
        _settle_earnings(payout["partner_id"], float(payout["amount"]), pid)

        fcm.notify_partner(
            payout["partner_id"], "payout_paid",
            variables={"amount": f"{float(payout['amount']):.0f}", "utr": body.utr},
        )
        msg = "Payout marked as paid"

    elif body.action == "reject":
        db.execute(
            "update payouts set status = 'rejected', processed_by = :a, note = :n where id = :id",
            {"a": admin["id"], "n": body.note, "id": pid},
        )
        msg = "Payout rejected"
    else:
        fail("Action must be approve, paid or reject", "INVALID_ACTION")

    _audit(admin, f"payout_{body.action}", "payouts", pid, payout, None)
    return ok(None, msg)


@router.get("/earnings")
def list_earnings(
    partner_id: Optional[int] = None,
    settlement_status: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    page: int = 1,
    limit: int = 20,
    admin=Depends(require_permission("payouts")),
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}

    if partner_id:
        where.append("e.partner_id = :pid")
        params["pid"] = partner_id
    if settlement_status:
        where.append("e.settlement_status = cast(:ss as settlement_status)")
        params["ss"] = settlement_status
    if from_date:
        where.append("e.created_at >= :fd")
        params["fd"] = from_date
    if to_date:
        where.append("e.created_at < (:td::date + 1)")
        params["td"] = to_date

    clause = " and ".join(where)
    rows = db.fetch_all(
        f"""
        select e.*, e.settlement_status::text as settlement_status,
               p.name as partner_name, b.booking_code, b.scheduled_date
          from partner_earnings e
          join partners p on p.id = e.partner_id
          join bookings b on b.id = e.booking_id
         where {clause}
         order by e.created_at desc
         limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from partner_earnings e where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    for r in rows:
        for k in ("gross", "commission_percent", "commission_amount", "net_payable",
                  "cash_collected"):
            r[k] = float(r[k])
    return ok(pg.envelope(rows, int(total or 0)))


# ==================================================================
# NOTIFICATIONS
# ==================================================================
class BroadcastIn(BaseModel):
    audience: str                  # all_users | all_partners | user | partner
    target_id: Optional[int] = None
    title: str
    body: str
    image_url: Optional[str] = None
    deeplink: Optional[str] = None


@router.post("/notifications/send")
def send_notification(body: BroadcastIn, admin=Depends(require_permission("notifications"))):
    if body.audience in ("all_users", "all_partners"):
        sent = fcm.broadcast(
            body.audience, body.title, body.body,
            body.image_url, body.deeplink, admin["id"],
        )
        return ok({"sent": sent}, f"Sent to {sent} device(s)")

    if not body.target_id:
        fail("target_id is required for a single recipient", "TARGET_REQUIRED")

    table = "users" if body.audience == "user" else "partners"
    row = db.fetch_one(f"select fcm_token from {table} where id = :id", {"id": body.target_id})
    if not row:
        fail("Recipient not found", "NOT_FOUND", 404)

    db.execute(
        """
        insert into notifications (audience, target_id, title, body, image_url, deeplink, sent_by)
        values (:a, :t, :ti, :b, :img, :dl, :by)
        """,
        {"a": body.audience, "t": body.target_id, "ti": body.title, "b": body.body,
         "img": body.image_url, "dl": body.deeplink, "by": admin["id"]},
    )
    delivered = fcm.send_to_token(
        row["fcm_token"], body.title, body.body,
        {"deeplink": body.deeplink or ""}, image=body.image_url,
    )
    return ok({"delivered": delivered}, "Notification sent" if delivered else "Saved (device offline)")


@router.get("/notifications")
def notification_history(
    page: int = 1, limit: int = 20, admin=Depends(require_permission("notifications"))
):
    pg = Pagination(page, limit)
    rows = db.fetch_all(
        """
        select n.*, a.name as sent_by_name
          from notifications n left join admins a on a.id = n.sent_by
         where n.sent_by is not null
         order by n.created_at desc limit :l offset :o
        """,
        {"l": pg.limit, "o": pg.offset},
    )
    total = db.fetch_value("select count(*) from notifications where sent_by is not null")
    return ok(pg.envelope(rows, int(total or 0)))


class TemplateIn(BaseModel):
    title: str
    body: str
    deeplink: Optional[str] = None
    is_active: bool = True


@router.get("/notification-templates")
def list_templates(admin=Depends(require_permission("notifications"))):
    return ok(db.fetch_all("select * from notification_templates order by audience, event_key"))


@router.put("/notification-templates/{event_key}")
def update_template(
    event_key: str, body: TemplateIn, admin=Depends(require_permission("notifications"))
):
    row = db.execute(
        """
        update notification_templates
           set title = :t, body = :b, deeplink = :dl, is_active = :a
         where event_key = :k
        returning *
        """,
        {"t": body.title, "b": body.body, "dl": body.deeplink,
         "a": body.is_active, "k": event_key},
    )
    if not row:
        fail("Template not found", "NOT_FOUND", 404)
    return ok(row, "Template updated")


# ==================================================================
# REVIEWS
# ==================================================================
@router.get("/reviews")
def list_reviews(
    partner_id: Optional[int] = None,
    rating: Optional[int] = None,
    visible: Optional[bool] = None,
    page: int = 1,
    limit: int = 20,
    admin=Depends(require_permission("reviews")),
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}
    if partner_id:
        where.append("r.partner_id = :pid")
        params["pid"] = partner_id
    if rating:
        where.append("r.rating = :rt")
        params["rt"] = rating
    if visible is not None:
        where.append("r.is_visible = :v")
        params["v"] = visible

    clause = " and ".join(where)
    rows = db.fetch_all(
        f"""
        select r.*, u.name as user_name, p.name as partner_name, b.booking_code
          from reviews r
          join users u on u.id = r.user_id
          left join partners p on p.id = r.partner_id
          join bookings b on b.id = r.booking_id
         where {clause}
         order by r.created_at desc limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from reviews r where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    return ok(pg.envelope(rows, int(total or 0)))


class ReviewModerateIn(BaseModel):
    is_visible: Optional[bool] = None
    admin_reply: Optional[str] = None


@router.put("/reviews/{rid}")
def moderate_review(
    rid: int, body: ReviewModerateIn, admin=Depends(require_permission("reviews"))
):
    row = db.execute(
        """
        update reviews
           set is_visible = coalesce(:v, is_visible),
               admin_reply = coalesce(:r, admin_reply)
         where id = :id
        returning *
        """,
        {"v": body.is_visible, "r": body.admin_reply, "id": rid},
    )
    if not row:
        fail("Review not found", "NOT_FOUND", 404)

    if row["partner_id"]:
        db.execute(
            """
            update partners p
               set rating_count = sub.cnt, rating_avg = coalesce(sub.avg, 0)
              from (select count(*) as cnt, round(avg(rating)::numeric, 2) as avg
                      from reviews where partner_id = :p and is_visible) sub
             where p.id = :p
            """,
            {"p": row["partner_id"]},
        )
    return ok(row, "Review updated")


# ==================================================================
# SUPPORT TICKETS
# ==================================================================
@router.get("/tickets")
def list_tickets(
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    admin=Depends(require_permission("support")),
):
    pg = Pagination(page, limit)
    where = "1=1"
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}
    if status:
        where += " and status = :st"
        params["st"] = status

    rows = db.fetch_all(
        f"""
        select t.*, t.requester_type::text as requester_type,
               case when t.requester_type = 'user'
                    then (select name from users where id = t.requester_id)
                    else (select name from partners where id = t.requester_id) end as requester_name
          from support_tickets t
         where {where}
         order by t.created_at desc limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from support_tickets where {where}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    return ok(pg.envelope(rows, int(total or 0)))


class TicketReplyIn(BaseModel):
    message: str
    status: Optional[str] = None


@router.post("/tickets/{tid}/reply")
def reply_ticket(tid: int, body: TicketReplyIn, admin=Depends(require_permission("support"))):
    ticket = db.fetch_one("select * from support_tickets where id = :id", {"id": tid})
    if not ticket:
        fail("Ticket not found", "NOT_FOUND", 404)

    replies = ticket["replies"] or []
    replies.append(
        {
            "by": "admin",
            "admin_id": admin["id"],
            "admin_name": admin["name"],
            "message": body.message,
            "at": datetime.now().isoformat(),
        }
    )
    db.execute(
        """
        update support_tickets
           set replies = cast(:r as jsonb), status = coalesce(:s, status)
         where id = :id
        """,
        {"r": json.dumps(replies), "s": body.status or "in_progress", "id": tid},
    )
    return ok(None, "Reply sent")


# ==================================================================
# REPORTS
# ==================================================================
@router.get("/reports/revenue")
def revenue_report(
    from_date: date,
    to_date: date,
    group_by: str = Query("day", description="day | service | partner | city"),
    admin=Depends(require_permission("reports")),
):
    params = {"fd": from_date, "td": to_date}

    if group_by == "service":
        sql = """
            select bi.service_name as label, count(*) as bookings,
                   coalesce(sum(bi.line_total), 0) as revenue
              from booking_items bi join bookings b on b.id = bi.booking_id
             where b.status in ('completed','paid')
               and b.scheduled_date between :fd and :td
             group by bi.service_name order by revenue desc
        """
    elif group_by == "partner":
        sql = """
            select p.name as label, count(*) as bookings,
                   coalesce(sum(b.total), 0) as revenue,
                   coalesce(sum(e.commission_amount), 0) as commission
              from bookings b
              join partners p on p.id = b.assigned_partner_id
              left join partner_earnings e on e.booking_id = b.id
             where b.status in ('completed','paid')
               and b.scheduled_date between :fd and :td
             group by p.name order by revenue desc
        """
    elif group_by == "city":
        sql = """
            select coalesce(b.addr_snapshot->>'city', 'Unknown') as label,
                   count(*) as bookings, coalesce(sum(b.total), 0) as revenue
              from bookings b
             where b.status in ('completed','paid')
               and b.scheduled_date between :fd and :td
             group by 1 order by revenue desc
        """
    else:
        sql = """
            select scheduled_date::text as label, count(*) as bookings,
                   coalesce(sum(total), 0) as revenue
              from bookings
             where status in ('completed','paid')
               and scheduled_date between :fd and :td
             group by scheduled_date order by scheduled_date
        """

    rows = db.fetch_all(sql, params)
    for r in rows:
        r["revenue"] = float(r["revenue"])
        if "commission" in r:
            r["commission"] = float(r["commission"])

    totals = db.fetch_one(
        """
        select count(*) as bookings, coalesce(sum(total), 0) as revenue,
               coalesce(avg(total), 0) as avg_order_value
          from bookings
         where status in ('completed','paid') and scheduled_date between :fd and :td
        """,
        params,
    )
    return ok(
        {
            "group_by": group_by,
            "rows": rows,
            "totals": {
                "bookings": totals["bookings"],
                "revenue": float(totals["revenue"]),
                "avg_order_value": float(totals["avg_order_value"]),
            },
        }
    )


@router.get("/reports/export")
def export_csv(
    report: str = Query("bookings", description="bookings | earnings | users"),
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    admin=Depends(require_permission("reports")),
):
    fd = from_date or (datetime.now().date() - timedelta(days=30))
    td = to_date or datetime.now().date()

    if report == "earnings":
        rows = db.fetch_all(
            """
            select b.booking_code, b.scheduled_date, p.name as partner,
                   e.gross, e.commission_percent, e.commission_amount, e.net_payable,
                   e.settlement_status::text as settlement_status
              from partner_earnings e
              join partners p on p.id = e.partner_id
              join bookings b on b.id = e.booking_id
             where b.scheduled_date between :fd and :td
             order by b.scheduled_date
            """,
            {"fd": fd, "td": td},
        )
    elif report == "users":
        rows = db.fetch_all(
            """
            select u.id, u.name, u.phone, u.email, u.created_at::date as joined,
                   count(b.id) as bookings,
                   coalesce(sum(b.total) filter (where b.status in ('completed','paid')), 0) as spent
              from users u left join bookings b on b.user_id = u.id
             where u.created_at::date between :fd and :td
             group by u.id order by u.created_at
            """,
            {"fd": fd, "td": td},
        )
    else:
        rows = db.fetch_all(
            """
            select b.booking_code, b.scheduled_date, b.slot_label,
                   b.status::text as status, u.name as customer, u.phone,
                   b.addr_snapshot->>'city' as city, b.addr_snapshot->>'pincode' as pincode,
                   p.name as technician, b.subtotal, b.discount, b.tax, b.total,
                   b.payment_mode::text as payment_mode,
                   b.payment_status::text as payment_status,
                   (select string_agg(service_name, ' | ') from booking_items
                     where booking_id = b.id) as services
              from bookings b
              join users u on u.id = b.user_id
              left join partners p on p.id = b.assigned_partner_id
             where b.scheduled_date between :fd and :td
             order by b.scheduled_date
            """,
            {"fd": fd, "td": td},
        )

    if not rows:
        fail("No data for the selected range", "NO_DATA", 404)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for r in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    buf.seek(0)

    filename = f"mistrio_{report}_{fd}_{td}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ==================================================================
# SUB-ADMINS
# ==================================================================
class AdminCreateIn(BaseModel):
    email: str
    password: str = Field(..., min_length=8)
    name: str
    role: str = "manager"
    permissions: Dict[str, bool] = {}


@router.get("/admins")
def list_admins(admin=Depends(require_super_admin)):
    rows = db.fetch_all(
        "select id, email, name, role::text as role, permissions, is_active, "
        "last_login_at, created_at from admins order by id"
    )
    return ok(rows)


@router.post("/admins")
def create_admin(body: AdminCreateIn, admin=Depends(require_super_admin)):
    if db.fetch_one("select 1 from admins where lower(email) = lower(:e)", {"e": body.email}):
        fail("An admin with this email already exists", "DUPLICATE_EMAIL", 409)
    if body.role not in ("super_admin", "manager", "support", "accountant"):
        fail("Invalid role", "INVALID_ROLE")

    row = db.execute(
        """
        insert into admins (email, password_hash, name, role, permissions)
        values (lower(:e), :p, :n, cast(:r as admin_role), cast(:perm as jsonb))
        returning id, email, name, role::text as role, permissions, is_active
        """,
        {"e": body.email, "p": hash_password(body.password), "n": body.name,
         "r": body.role, "perm": json.dumps(body.permissions)},
    )
    _audit(admin, "create", "admins", row["id"], None, row)
    return ok(row, "Admin created")


class AdminUpdateIn(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    permissions: Optional[Dict[str, bool]] = None
    is_active: Optional[bool] = None
    new_password: Optional[str] = None


@router.put("/admins/{aid}")
def update_admin(aid: int, body: AdminUpdateIn, admin=Depends(require_super_admin)):
    if aid == admin["id"] and body.is_active is False:
        fail("You cannot deactivate your own account", "SELF_DEACTIVATE")

    row = db.execute(
        """
        update admins
           set name = coalesce(:n, name),
               role = coalesce(cast(:r as admin_role), role),
               permissions = coalesce(cast(:perm as jsonb), permissions),
               is_active = coalesce(:a, is_active),
               password_hash = coalesce(:pw, password_hash)
         where id = :id
        returning id, email, name, role::text as role, permissions, is_active
        """,
        {
            "n": body.name, "r": body.role,
            "perm": json.dumps(body.permissions) if body.permissions is not None else None,
            "a": body.is_active,
            "pw": hash_password(body.new_password) if body.new_password else None,
            "id": aid,
        },
    )
    if not row:
        fail("Admin not found", "NOT_FOUND", 404)
    _audit(admin, "update", "admins", aid, None, row)
    return ok(row, "Admin updated")


@router.delete("/admins/{aid}")
def delete_admin(aid: int, admin=Depends(require_super_admin)):
    if aid == admin["id"]:
        fail("You cannot delete your own account", "SELF_DELETE")
    db.execute("update admins set is_active = false where id = :id", {"id": aid})
    _audit(admin, "deactivate", "admins", aid, None, None)
    return ok(None, "Admin deactivated")


@router.get("/permissions")
def available_permissions(admin=Depends(get_current_admin)):
    """Drives the checkbox matrix in the admin panel."""
    return ok(
        [
            {"key": "dashboard", "label": "Dashboard & live map"},
            {"key": "bookings", "label": "Bookings — view, assign, cancel"},
            {"key": "catalog", "label": "Categories, services, banners, slots"},
            {"key": "partners", "label": "Technicians — approve, edit, suspend"},
            {"key": "users", "label": "Customers — view, wallet, block"},
            {"key": "coupons", "label": "Coupons & offers"},
            {"key": "payouts", "label": "Earnings & payouts"},
            {"key": "notifications", "label": "Push notifications"},
            {"key": "reviews", "label": "Review moderation"},
            {"key": "support", "label": "Support tickets"},
            {"key": "parts", "label": "Spare parts & orders"},
            {"key": "reports", "label": "Reports & exports"},
            {"key": "config", "label": "App settings"},
        ]
    )


# ==================================================================
# AUDIT LOG
# ==================================================================
@router.get("/audit-logs")
def audit_logs(
    entity: Optional[str] = None,
    actor_id: Optional[int] = None,
    page: int = 1,
    limit: int = 50,
    admin=Depends(require_permission("config")),
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}
    if entity:
        where.append("l.entity = :e")
        params["e"] = entity
    if actor_id:
        where.append("l.actor_id = :a")
        params["a"] = actor_id

    clause = " and ".join(where)
    rows = db.fetch_all(
        f"""
        select l.*, l.actor_type::text as actor_type, a.name as actor_name
          from audit_logs l left join admins a on a.id = l.actor_id
         where {clause}
         order by l.created_at desc limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from audit_logs l where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    return ok(pg.envelope(rows, int(total or 0)))


# ==================================================================
# MAINTENANCE / CRON
# ==================================================================
@router.post("/cron/expire-offers")
def cron_expire_offers(admin=Depends(require_permission("bookings"))):
    """
    Expires stale job offers and re-offers those bookings.
    Hit this every minute from an external cron service.
    """
    from app.services.assignment import expire_stale_offers

    count = expire_stale_offers()
    return ok({"rechecked": count}, f"{count} booking(s) re-offered")


# ==================================================================
# HELPERS
# ==================================================================
def _settle_earnings(partner_id: int, amount: float, payout_id: int) -> None:
    """Marks earnings as settled, oldest first, until the payout is covered."""
    remaining = amount
    rows = db.fetch_all(
        """
        select id, net_payable from partner_earnings
         where partner_id = :p and settlement_status in ('pending','approved')
         order by created_at
        """,
        {"p": partner_id},
    )
    for r in rows:
        if remaining <= 0:
            break
        db.execute(
            """
            update partner_earnings
               set settlement_status = 'settled', settled_at = now(), payout_id = :po
             where id = :id
            """,
            {"po": payout_id, "id": r["id"]},
        )
        remaining -= float(r["net_payable"])


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
