# app/routers/catalog.py
"""
Catalog — categories, services, options, FAQs, banners, time slots, brands.

Public endpoints feed the User App home/browse screens.
Admin endpoints give full CRUD so nothing ever needs a code change.
"""

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.database import db
from app.dependencies import fail, ok, require_permission

router = APIRouter(tags=["Catalog"])

PERM = "catalog"


# ==================================================================
# PUBLIC — HOME
# ==================================================================
@router.get("/home")
def home_screen(pincode: Optional[str] = None):
    """One call that fills the entire user-app home screen."""
    banners = db.fetch_all(
        """
        select id, title, image_url, screen, target_type, target_id, target_url
          from banners
         where is_active
           and screen = 'home'
           and (start_at is null or start_at <= now())
           and (end_at   is null or end_at   >= now())
         order by display_order, id
        """
    )
    categories = db.fetch_all(
        """
        select id, name, name_hi, icon_url, banner_url, description
          from categories where is_active
         order by display_order, id
        """
    )
    popular = db.fetch_all(
        """
        select s.id, s.name, s.name_hi, s.image_url, s.base_price, s.strike_price,
               s.price_type, s.duration_minutes, s.category_id, c.name as category_name,
               coalesce(round(avg(r.rating)::numeric, 1), 0) as rating,
               count(r.id) as rating_count
          from services s
          join categories c on c.id = s.category_id
          left join booking_items bi on bi.service_id = s.id
          left join reviews r on r.booking_id = bi.booking_id and r.is_visible
         where s.is_active and c.is_active
         group by s.id, c.name
         order by s.display_order, s.id
         limit 8
        """
    )
    offers = db.fetch_all(
        """
        select code, title, description, type, value, max_discount, min_order, valid_to
          from coupons
         where is_active
           and valid_from <= now()
           and (valid_to is null or valid_to >= now())
           and (usage_limit is null or used_count < usage_limit)
         order by id desc limit 5
        """
    )

    serviceable = None
    if pincode:
        row = db.fetch_one(
            "select city, is_active from service_areas where pincode = :p", {"p": pincode}
        )
        serviceable = {
            "serviceable": bool(row and row["is_active"]),
            "city": row["city"] if row else None,
        }

    return ok(
        {
            "banners": banners,
            "categories": categories,
            "popular_services": _cast_prices(popular),
            "offers": offers,
            "serviceability": serviceable,
        }
    )


# ==================================================================
# PUBLIC — CATEGORIES & SERVICES
# ==================================================================
@router.get("/categories")
def list_categories():
    rows = db.fetch_all(
        """
        select c.*, (select count(*) from services s
                      where s.category_id = c.id and s.is_active) as service_count
          from categories c
         where c.is_active
         order by c.display_order, c.id
        """
    )
    return ok(rows)


@router.get("/services")
def list_services(
    category_id: Optional[int] = None,
    search: Optional[str] = None,
    limit: int = Query(50, le=100),
):
    sql = """
        select s.id, s.category_id, s.name, s.name_hi, s.short_desc, s.image_url,
               s.base_price, s.strike_price, s.price_type, s.duration_minutes,
               s.visit_charge, s.warranty_days, s.warranty_text, s.tags,
               c.name as category_name
          from services s
          join categories c on c.id = s.category_id
         where s.is_active and c.is_active
    """
    params: Dict[str, Any] = {"limit": limit}
    if category_id:
        sql += " and s.category_id = :cid"
        params["cid"] = category_id
    if search:
        sql += " and (s.name ilike :q or s.name_hi ilike :q or :raw = any(s.tags))"
        params["q"] = f"%{search}%"
        params["raw"] = search
    sql += " order by s.display_order, s.id limit :limit"

    return ok(_cast_prices(db.fetch_all(sql, params)))


@router.get("/services/{service_id}")
def service_detail(service_id: int):
    svc = db.fetch_one(
        """
        select s.*, c.name as category_name, c.name_hi as category_name_hi
          from services s join categories c on c.id = s.category_id
         where s.id = :id and s.is_active
        """,
        {"id": service_id},
    )
    if not svc:
        fail("Service not found", "SERVICE_NOT_FOUND", 404)

    options = db.fetch_all(
        "select id, name, extra_price from service_options "
        "where service_id = :id and is_active order by display_order, id",
        {"id": service_id},
    )
    faqs = db.fetch_all(
        "select question, answer from service_faqs where service_id = :id "
        "order by display_order, id",
        {"id": service_id},
    )
    reviews = db.fetch_all(
        """
        select r.rating, r.comment, r.images, r.created_at, u.name as user_name,
               p.name as partner_name
          from reviews r
          join bookings b on b.id = r.booking_id
          join booking_items bi on bi.booking_id = b.id
          join users u on u.id = r.user_id
          left join partners p on p.id = r.partner_id
         where bi.service_id = :id and r.is_visible
         order by r.created_at desc limit 10
        """,
        {"id": service_id},
    )
    stats = db.fetch_one(
        """
        select coalesce(round(avg(r.rating)::numeric,1),0) as rating_avg,
               count(r.id) as rating_count
          from reviews r
          join booking_items bi on bi.booking_id = r.booking_id
         where bi.service_id = :id and r.is_visible
        """,
        {"id": service_id},
    )

    svc = _cast_prices([svc])[0]
    for o in options:
        o["extra_price"] = float(o["extra_price"])

    return ok(
        {
            **svc,
            "options": options,
            "faqs": faqs,
            "reviews": reviews,
            "rating_avg": float(stats["rating_avg"]),
            "rating_count": stats["rating_count"],
        }
    )


# ==================================================================
# PUBLIC — SLOTS
# ==================================================================
@router.get("/slots")
def available_slots(for_date: date = Query(..., alias="date")):
    """Returns slots with remaining capacity for the chosen date."""
    slots = db.fetch_all(
        "select id, label, start_time, end_time, max_bookings from time_slots "
        "where is_active order by display_order, start_time"
    )
    booked = db.fetch_all(
        """
        select slot_id, count(*) as cnt from bookings
         where scheduled_date = :d
           and status not in ('cancelled','rejected')
         group by slot_id
        """,
        {"d": for_date},
    )
    used = {b["slot_id"]: b["cnt"] for b in booked}
    now = datetime.now()

    out: List[Dict[str, Any]] = []
    for s in slots:
        taken = used.get(s["id"], 0)
        remaining = max(0, s["max_bookings"] - taken)
        past = for_date == now.date() and s["start_time"] <= now.time()
        out.append(
            {
                "id": s["id"],
                "label": s["label"],
                "start_time": str(s["start_time"]),
                "end_time": str(s["end_time"]),
                "remaining": remaining,
                "available": remaining > 0 and not past,
                "reason": "Slot full" if remaining == 0 else ("Time passed" if past else None),
            }
        )
    return ok({"date": str(for_date), "slots": out})


@router.get("/banners")
def list_banners(screen: str = "home"):
    rows = db.fetch_all(
        """
        select id, title, image_url, screen, target_type, target_id, target_url
          from banners
         where is_active and screen = :s
           and (start_at is null or start_at <= now())
           and (end_at   is null or end_at   >= now())
         order by display_order, id
        """,
        {"s": screen},
    )
    return ok(rows)


@router.get("/brands")
def list_brands():
    return ok(db.fetch_all("select * from brands where is_active order by name"))


# ==================================================================
# ADMIN — CATEGORIES
# ==================================================================
class CategoryIn(BaseModel):
    name: str
    name_hi: Optional[str] = None
    icon_url: Optional[str] = None
    banner_url: Optional[str] = None
    description: Optional[str] = None
    display_order: int = 0
    is_active: bool = True


@router.get("/admin/categories", dependencies=[Depends(require_permission(PERM))])
def admin_list_categories():
    return ok(db.fetch_all("select * from categories order by display_order, id"))


@router.post("/admin/categories")
def admin_create_category(body: CategoryIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        """
        insert into categories (name, name_hi, icon_url, banner_url, description,
                                display_order, is_active)
        values (:n, :nh, :i, :b, :d, :o, :a) returning *
        """,
        {
            "n": body.name, "nh": body.name_hi, "i": body.icon_url,
            "b": body.banner_url, "d": body.description,
            "o": body.display_order, "a": body.is_active,
        },
    )
    _audit(admin, "create", "categories", row["id"], None, row)
    return ok(row, "Category created")


@router.put("/admin/categories/{cid}")
def admin_update_category(cid: int, body: CategoryIn, admin=Depends(require_permission(PERM))):
    before = db.fetch_one("select * from categories where id = :id", {"id": cid})
    if not before:
        fail("Category not found", "NOT_FOUND", 404)
    row = db.execute(
        """
        update categories set name=:n, name_hi=:nh, icon_url=:i, banner_url=:b,
               description=:d, display_order=:o, is_active=:a
         where id = :id returning *
        """,
        {
            "n": body.name, "nh": body.name_hi, "i": body.icon_url,
            "b": body.banner_url, "d": body.description,
            "o": body.display_order, "a": body.is_active, "id": cid,
        },
    )
    _audit(admin, "update", "categories", cid, before, row)
    return ok(row, "Category updated")


@router.delete("/admin/categories/{cid}")
def admin_delete_category(cid: int, admin=Depends(require_permission(PERM))):
    count = db.fetch_value("select count(*) from services where category_id = :id", {"id": cid})
    if count and int(count) > 0:
        fail(
            f"This category has {count} services. Remove them or deactivate the category instead.",
            "HAS_SERVICES",
        )
    before = db.fetch_one("select * from categories where id = :id", {"id": cid})
    db.execute("delete from categories where id = :id", {"id": cid})
    _audit(admin, "delete", "categories", cid, before, None)
    return ok(None, "Category deleted")


# ==================================================================
# ADMIN — SERVICES
# ==================================================================
class ServiceIn(BaseModel):
    category_id: int
    name: str
    name_hi: Optional[str] = None
    short_desc: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    gallery: List[str] = []
    base_price: float = 0
    strike_price: Optional[float] = None
    price_type: str = "fixed"          # fixed | starting_from | inspection_based
    duration_minutes: int = 60
    visit_charge: float = 0
    warranty_days: int = 0
    warranty_text: Optional[str] = None
    includes: List[str] = []
    excludes: List[str] = []
    tags: List[str] = []
    display_order: int = 0
    is_active: bool = True


@router.get("/admin/services", dependencies=[Depends(require_permission(PERM))])
def admin_list_services(category_id: Optional[int] = None):
    sql = """
        select s.*, c.name as category_name
          from services s join categories c on c.id = s.category_id
    """
    params: Dict[str, Any] = {}
    if category_id:
        sql += " where s.category_id = :cid"
        params["cid"] = category_id
    sql += " order by s.category_id, s.display_order, s.id"
    return ok(db.fetch_all(sql, params))


@router.post("/admin/services")
def admin_create_service(body: ServiceIn, admin=Depends(require_permission(PERM))):
    if not db.fetch_one("select 1 from categories where id = :id", {"id": body.category_id}):
        fail("Category not found", "CATEGORY_NOT_FOUND", 404)
    row = db.execute(_SERVICE_INSERT, _service_params(body))
    _audit(admin, "create", "services", row["id"], None, row)
    return ok(row, "Service created")


@router.put("/admin/services/{sid}")
def admin_update_service(sid: int, body: ServiceIn, admin=Depends(require_permission(PERM))):
    before = db.fetch_one("select * from services where id = :id", {"id": sid})
    if not before:
        fail("Service not found", "NOT_FOUND", 404)
    row = db.execute(_SERVICE_UPDATE, {**_service_params(body), "id": sid})
    _audit(admin, "update", "services", sid, before, row)
    return ok(row, "Service updated")


@router.delete("/admin/services/{sid}")
def admin_delete_service(sid: int, admin=Depends(require_permission(PERM))):
    used = db.fetch_value("select count(*) from booking_items where service_id = :id", {"id": sid})
    before = db.fetch_one("select * from services where id = :id", {"id": sid})
    if used and int(used) > 0:
        db.execute("update services set is_active = false where id = :id", {"id": sid})
        _audit(admin, "deactivate", "services", sid, before, None)
        return ok(None, "This service is used in past bookings, so it was deactivated instead of deleted.")
    db.execute("delete from services where id = :id", {"id": sid})
    _audit(admin, "delete", "services", sid, before, None)
    return ok(None, "Service deleted")


# ---- service options ----
class OptionIn(BaseModel):
    name: str
    extra_price: float = 0
    display_order: int = 0
    is_active: bool = True


@router.get("/admin/services/{sid}/options", dependencies=[Depends(require_permission(PERM))])
def admin_list_options(sid: int):
    return ok(
        db.fetch_all(
            "select * from service_options where service_id = :id order by display_order, id",
            {"id": sid},
        )
    )


@router.post("/admin/services/{sid}/options")
def admin_create_option(sid: int, body: OptionIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        """
        insert into service_options (service_id, name, extra_price, display_order, is_active)
        values (:s, :n, :p, :o, :a) returning *
        """,
        {"s": sid, "n": body.name, "p": body.extra_price,
         "o": body.display_order, "a": body.is_active},
    )
    return ok(row, "Option added")


@router.put("/admin/options/{oid}")
def admin_update_option(oid: int, body: OptionIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        """
        update service_options set name=:n, extra_price=:p, display_order=:o, is_active=:a
         where id = :id returning *
        """,
        {"n": body.name, "p": body.extra_price, "o": body.display_order,
         "a": body.is_active, "id": oid},
    )
    if not row:
        fail("Option not found", "NOT_FOUND", 404)
    return ok(row, "Option updated")


@router.delete("/admin/options/{oid}")
def admin_delete_option(oid: int, admin=Depends(require_permission(PERM))):
    db.execute("delete from service_options where id = :id", {"id": oid})
    return ok(None, "Option deleted")


# ---- FAQs ----
class FaqIn(BaseModel):
    question: str
    answer: str
    display_order: int = 0


@router.post("/admin/services/{sid}/faqs")
def admin_create_faq(sid: int, body: FaqIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        "insert into service_faqs (service_id, question, answer, display_order) "
        "values (:s, :q, :a, :o) returning *",
        {"s": sid, "q": body.question, "a": body.answer, "o": body.display_order},
    )
    return ok(row, "FAQ added")


@router.delete("/admin/faqs/{fid}")
def admin_delete_faq(fid: int, admin=Depends(require_permission(PERM))):
    db.execute("delete from service_faqs where id = :id", {"id": fid})
    return ok(None, "FAQ deleted")


# ==================================================================
# ADMIN — BANNERS
# ==================================================================
class BannerIn(BaseModel):
    title: Optional[str] = None
    image_url: str
    screen: str = "home"
    target_type: Optional[str] = None      # service | category | url | none
    target_id: Optional[int] = None
    target_url: Optional[str] = None
    display_order: int = 0
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    is_active: bool = True


@router.get("/admin/banners", dependencies=[Depends(require_permission(PERM))])
def admin_list_banners():
    return ok(db.fetch_all("select * from banners order by screen, display_order, id"))


@router.post("/admin/banners")
def admin_create_banner(body: BannerIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        """
        insert into banners (title, image_url, screen, target_type, target_id, target_url,
                             display_order, start_at, end_at, is_active)
        values (:t, :img, :s, :tt, :tid, :turl, :o, :sa, :ea, :a) returning *
        """,
        {
            "t": body.title, "img": body.image_url, "s": body.screen,
            "tt": body.target_type, "tid": body.target_id, "turl": body.target_url,
            "o": body.display_order, "sa": body.start_at, "ea": body.end_at,
            "a": body.is_active,
        },
    )
    return ok(row, "Banner created")


@router.put("/admin/banners/{bid}")
def admin_update_banner(bid: int, body: BannerIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        """
        update banners set title=:t, image_url=:img, screen=:s, target_type=:tt,
               target_id=:tid, target_url=:turl, display_order=:o,
               start_at=:sa, end_at=:ea, is_active=:a
         where id = :id returning *
        """,
        {
            "t": body.title, "img": body.image_url, "s": body.screen,
            "tt": body.target_type, "tid": body.target_id, "turl": body.target_url,
            "o": body.display_order, "sa": body.start_at, "ea": body.end_at,
            "a": body.is_active, "id": bid,
        },
    )
    if not row:
        fail("Banner not found", "NOT_FOUND", 404)
    return ok(row, "Banner updated")


@router.delete("/admin/banners/{bid}")
def admin_delete_banner(bid: int, admin=Depends(require_permission(PERM))):
    db.execute("delete from banners where id = :id", {"id": bid})
    return ok(None, "Banner deleted")


# ==================================================================
# ADMIN — TIME SLOTS
# ==================================================================
class SlotIn(BaseModel):
    label: str
    start_time: str          # "10:00"
    end_time: str            # "12:00"
    max_bookings: int = 20
    display_order: int = 0
    is_active: bool = True


@router.get("/admin/slots", dependencies=[Depends(require_permission(PERM))])
def admin_list_slots():
    return ok(db.fetch_all("select * from time_slots order by display_order, start_time"))


@router.post("/admin/slots")
def admin_create_slot(body: SlotIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        """
        insert into time_slots (label, start_time, end_time, max_bookings,
                                display_order, is_active)
        values (:l, cast(:st as time), cast(:et as time), :m, :o, :a) returning *
        """,
        {"l": body.label, "st": body.start_time, "et": body.end_time,
         "m": body.max_bookings, "o": body.display_order, "a": body.is_active},
    )
    return ok(row, "Slot created")


@router.put("/admin/slots/{sid}")
def admin_update_slot(sid: int, body: SlotIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        """
        update time_slots set label=:l, start_time=cast(:st as time),
               end_time=cast(:et as time), max_bookings=:m,
               display_order=:o, is_active=:a
         where id = :id returning *
        """,
        {"l": body.label, "st": body.start_time, "et": body.end_time,
         "m": body.max_bookings, "o": body.display_order, "a": body.is_active, "id": sid},
    )
    if not row:
        fail("Slot not found", "NOT_FOUND", 404)
    return ok(row, "Slot updated")


@router.delete("/admin/slots/{sid}")
def admin_delete_slot(sid: int, admin=Depends(require_permission(PERM))):
    db.execute("update time_slots set is_active = false where id = :id", {"id": sid})
    return ok(None, "Slot deactivated")


# ==================================================================
# ADMIN — SERVICE AREAS
# ==================================================================
class AreaIn(BaseModel):
    pincode: str
    city: str
    state: str
    is_active: bool = True


@router.post("/admin/service-areas")
def admin_create_area(body: AreaIn, admin=Depends(require_permission(PERM))):
    existing = db.fetch_one("select id from service_areas where pincode = :p", {"p": body.pincode})
    if existing:
        row = db.execute(
            "update service_areas set city=:c, state=:s, is_active=:a where pincode=:p returning *",
            {"c": body.city, "s": body.state, "a": body.is_active, "p": body.pincode},
        )
        return ok(row, "Service area updated")
    row = db.execute(
        "insert into service_areas (pincode, city, state, is_active) "
        "values (:p, :c, :s, :a) returning *",
        {"p": body.pincode, "c": body.city, "s": body.state, "a": body.is_active},
    )
    return ok(row, "Service area added")


@router.post("/admin/service-areas/bulk")
def admin_bulk_areas(
    pincodes: List[str], city: str, state: str, admin=Depends(require_permission(PERM))
):
    """Add many pincodes for one city at once."""
    added = 0
    for p in pincodes:
        p = p.strip()
        if len(p) != 6 or not p.isdigit():
            continue
        db.execute(
            """
            insert into service_areas (pincode, city, state, is_active)
            values (:p, :c, :s, true)
            on conflict (pincode) do update set city=:c, state=:s, is_active=true
            """,
            {"p": p, "c": city, "s": state},
        )
        added += 1
    return ok({"added": added}, f"{added} pincodes added")


@router.delete("/admin/service-areas/{aid}")
def admin_delete_area(aid: int, admin=Depends(require_permission(PERM))):
    db.execute("delete from service_areas where id = :id", {"id": aid})
    return ok(None, "Service area removed")


# ==================================================================
# INTERNAL HELPERS
# ==================================================================
_SERVICE_COLS = """
    category_id=:cid, name=:n, name_hi=:nh, short_desc=:sd, description=:d,
    image_url=:img, gallery=:g, base_price=:bp, strike_price=:sp,
    price_type=cast(:pt as price_type), duration_minutes=:dur, visit_charge=:vc,
    warranty_days=:wd, warranty_text=:wt, includes=:inc, excludes=:exc,
    tags=:tags, display_order=:o, is_active=:a
"""

_SERVICE_INSERT = """
    insert into services (category_id, name, name_hi, short_desc, description, image_url,
        gallery, base_price, strike_price, price_type, duration_minutes, visit_charge,
        warranty_days, warranty_text, includes, excludes, tags, display_order, is_active)
    values (:cid, :n, :nh, :sd, :d, :img, :g, :bp, :sp, cast(:pt as price_type), :dur,
            :vc, :wd, :wt, :inc, :exc, :tags, :o, :a)
    returning *
"""

_SERVICE_UPDATE = f"update services set {_SERVICE_COLS} where id = :id returning *"


def _service_params(body: "ServiceIn") -> Dict[str, Any]:
    return {
        "cid": body.category_id, "n": body.name, "nh": body.name_hi,
        "sd": body.short_desc, "d": body.description, "img": body.image_url,
        "g": body.gallery, "bp": body.base_price, "sp": body.strike_price,
        "pt": body.price_type, "dur": body.duration_minutes, "vc": body.visit_charge,
        "wd": body.warranty_days, "wt": body.warranty_text, "inc": body.includes,
        "exc": body.excludes, "tags": body.tags, "o": body.display_order,
        "a": body.is_active,
    }


def _cast_prices(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Decimal -> float so JSON output is clean for Flutter."""
    money = ("base_price", "strike_price", "visit_charge", "extra_price", "rating")
    for r in rows:
        for k in money:
            if k in r and r[k] is not None:
                r[k] = float(r[k])
    return rows


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
