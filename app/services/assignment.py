# app/services/assignment.py
"""
Auto-assignment — matches a confirmed booking to the right mechanic.

Strategy (all numbers come from app_config, nothing hardcoded):

  1. Find approved + online partners whose `skills` include the booking's
     service categories AND whose `service_area_pincodes` include the
     booking pincode.
  2. Rank them: distance ascending, rating descending, current workload
     ascending.
  3. Push the job to the top N partners simultaneously.
  4. First to accept wins (guarded by a conditional UPDATE so two
     simultaneous accepts cannot both succeed).
  5. If nobody accepts within the timeout, widen the radius and offer to
     the next batch.
  6. After all rounds, the booking sits in the manual-assignment queue for
     an admin to handle.

Called from:
  - booking creation (payment done / COD confirmed)
  - the retry endpoint (POST /admin/bookings/{id}/auto-assign)
  - reschedule
"""

import logging
from typing import Any, Dict, List, Optional

from app.core import fcm
from app.database import db
from app.services.pricing import _num, get_config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Candidate search
# ------------------------------------------------------------------
def find_candidates(booking_id: int, radius_km: Optional[float] = None) -> List[Dict[str, Any]]:
    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id", {"id": booking_id}
    )
    if not booking:
        return []

    snap = booking["addr_snapshot"] or {}
    pincode = snap.get("pincode")
    lat, lng = snap.get("lat"), snap.get("lng")

    category_ids = [
        r["category_id"]
        for r in db.fetch_all(
            """
            select distinct s.category_id
              from booking_items bi join services s on s.id = bi.service_id
             where bi.booking_id = :b
            """,
            {"b": booking_id},
        )
    ]
    if not category_ids:
        return []

    cfg = get_config("job_offer_radius_km")
    radius = radius_km if radius_km is not None else _num(cfg.get("job_offer_radius_km"), 8)

    # Haversine distance in SQL. Partners without a location get a large
    # distance so they rank last but are still eligible.
    sql = """
        select p.id, p.name, p.phone, p.photo, p.fcm_token, p.rating_avg,
               p.jobs_completed, p.current_lat, p.current_lng,
               case
                 when p.current_lat is null or :lat is null then 9999
                 else 6371 * acos(
                        least(1, greatest(-1,
                          cos(radians(:lat)) * cos(radians(p.current_lat)) *
                          cos(radians(p.current_lng) - radians(:lng)) +
                          sin(radians(:lat)) * sin(radians(p.current_lat))
                        ))
                      )
               end as distance_km,
               (select count(*) from bookings b2
                 where b2.assigned_partner_id = p.id
                   and b2.status in ('assigned','partner_on_the_way','arrived','in_progress')
               ) as active_jobs
          from partners p
         where p.status = 'approved'
           and p.is_online = true
           and p.skills && cast(:cats as bigint[])
           and (:pincode = any(p.service_area_pincodes)
                or cardinality(p.service_area_pincodes) = 0)
           and not exists (
                 select 1 from booking_offers o
                  where o.booking_id = :bid and o.partner_id = p.id
                    and o.response in ('rejected','expired')
               )
    """
    params: Dict[str, Any] = {
        "lat": lat, "lng": lng, "cats": category_ids,
        "pincode": pincode, "bid": booking_id,
    }

    rows = db.fetch_all(
        sql + " order by distance_km asc, p.rating_avg desc, active_jobs asc limit 30", params
    )

    # only enforce the radius when we actually know both locations
    if lat is not None:
        rows = [r for r in rows if float(r["distance_km"]) <= radius or r["distance_km"] == 9999]

    return rows


# ------------------------------------------------------------------
# Offering
# ------------------------------------------------------------------
def offer_to_partners(booking_id: int, radius_km: Optional[float] = None) -> Dict[str, Any]:
    """Push the job to the next batch of eligible partners."""
    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id", {"id": booking_id}
    )
    if not booking:
        return {"offered": 0, "reason": "booking_not_found"}
    if booking["status"] != "confirmed":
        return {"offered": 0, "reason": f"status_is_{booking['status']}"}
    if booking["assigned_partner_id"]:
        return {"offered": 0, "reason": "already_assigned"}

    cfg = get_config("job_offer_batch_size", "job_offer_timeout_sec", "default_commission_percent")
    batch = int(_num(cfg.get("job_offer_batch_size"), 3))
    timeout = int(_num(cfg.get("job_offer_timeout_sec"), 60))
    commission = _num(cfg.get("default_commission_percent"), 20)

    candidates = find_candidates(booking_id, radius_km)[:batch]
    if not candidates:
        logger.info("Booking %s: no candidates found", booking_id)
        return {"offered": 0, "reason": "no_partners_available"}

    snap = booking["addr_snapshot"] or {}
    services = db.fetch_value(
        "select string_agg(service_name, ', ') from booking_items where booking_id = :b",
        {"b": booking_id},
    )
    gross = float(booking["total"])
    payout = round(gross * (100 - commission) / 100, 2)

    offered = 0
    for c in candidates:
        db.execute(
            """
            insert into booking_offers (booking_id, partner_id, distance_km)
            values (:b, :p, :d)
            on conflict (booking_id, partner_id) do update
              set offered_at = now(), response = null, responded_at = null
            """,
            {"b": booking_id, "p": c["id"],
             "d": None if c["distance_km"] == 9999 else round(float(c["distance_km"]), 2)},
        )

        fcm.notify_partner(
            c["id"],
            "new_job_offer",
            variables={
                "service": services or "Service",
                "area": snap.get("area") or snap.get("city") or "",
                "payout": f"{payout:.0f}",
                "code": booking["booking_code"],
            },
            data={
                "booking_id": str(booking_id),
                "type": "job_offer",
                "timeout": str(timeout),
            },
            urgent=True,
        )
        offered += 1

    logger.info("Booking %s: offered to %s partners", booking_id, offered)
    return {"offered": offered, "timeout_sec": timeout, "partner_ids": [c["id"] for c in candidates]}


# ------------------------------------------------------------------
# Accepting
# ------------------------------------------------------------------
def accept_job(booking_id: int, partner_id: int) -> Dict[str, Any]:
    """
    Race-safe accept. The conditional UPDATE means only the first partner
    to hit this can win — the second gets zero rows back.
    """
    offer = db.fetch_one(
        "select * from booking_offers where booking_id = :b and partner_id = :p",
        {"b": booking_id, "p": partner_id},
    )
    if not offer:
        return {"success": False, "message": "This job was not offered to you."}
    if offer["response"] in ("rejected", "expired"):
        return {"success": False, "message": "This offer is no longer valid."}

    won = db.execute(
        """
        update bookings
           set assigned_partner_id = :p, assigned_at = now(), status = 'assigned'
         where id = :b
           and status = 'confirmed'
           and assigned_partner_id is null
        returning *
        """,
        {"p": partner_id, "b": booking_id},
    )

    if not won:
        db.execute(
            "update booking_offers set response = 'expired', responded_at = now() "
            "where booking_id = :b and partner_id = :p",
            {"b": booking_id, "p": partner_id},
        )
        return {"success": False, "message": "Another technician has taken this job."}

    db.execute(
        "update booking_offers set response = 'accepted', responded_at = now() "
        "where booking_id = :b and partner_id = :p",
        {"b": booking_id, "p": partner_id},
    )
    # close the losing offers
    db.execute(
        """
        update booking_offers set response = 'expired', responded_at = now()
         where booking_id = :b and partner_id <> :p and response is null
        """,
        {"b": booking_id, "p": partner_id},
    )

    db.execute(
        """
        insert into booking_status_history
          (booking_id, from_status, to_status, actor, actor_id, note)
        values (:b, 'confirmed', 'assigned', 'partner', :p, 'Partner accepted the job')
        """,
        {"b": booking_id, "p": partner_id},
    )

    partner = db.fetch_one("select name, phone from partners where id = :id", {"id": partner_id})
    fcm.notify_user(
        won["user_id"],
        "partner_assigned",
        variables={"partner": partner["name"], "code": won["booking_code"]},
        data={"booking_id": str(booking_id), "type": "assigned"},
    )

    return {"success": True, "message": "Job accepted!", "booking": won}


def reject_job(booking_id: int, partner_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
    db.execute(
        """
        update booking_offers
           set response = 'rejected', responded_at = now()
         where booking_id = :b and partner_id = :p and response is null
        """,
        {"b": booking_id, "p": partner_id},
    )

    still_open = db.fetch_one(
        "select id from bookings where id = :b and status = 'confirmed' "
        "and assigned_partner_id is null",
        {"b": booking_id},
    )
    if still_open:
        pending = db.fetch_value(
            "select count(*) from booking_offers where booking_id = :b and response is null",
            {"b": booking_id},
        )
        if not pending or int(pending) == 0:
            retry(booking_id)

    return {"success": True, "message": "Job rejected."}


# ------------------------------------------------------------------
# Retry with a wider radius
# ------------------------------------------------------------------
def retry(booking_id: int) -> Dict[str, Any]:
    """Called when a round times out or everyone rejects."""
    rounds = db.fetch_value(
        "select count(distinct partner_id) from booking_offers where booking_id = :b",
        {"b": booking_id},
    )
    cfg = get_config("job_offer_radius_km", "job_offer_max_radius_km")
    base = _num(cfg.get("job_offer_radius_km"), 8)
    cap = _num(cfg.get("job_offer_max_radius_km"), 30)

    # widen by the base radius for each partner already tried
    widened = min(cap, base * (1 + int(rounds or 0) / 3))

    result = offer_to_partners(booking_id, radius_km=widened)
    if result["offered"] == 0:
        logger.warning(
            "Booking %s could not be auto-assigned — moving to manual queue", booking_id
        )
        db.execute(
            """
            insert into booking_status_history
              (booking_id, from_status, to_status, actor, note)
            values (:b, 'confirmed', 'confirmed', 'system',
                    'Auto-assignment failed — needs manual assignment')
            """,
            {"b": booking_id},
        )
    return result


def expire_stale_offers() -> int:
    """
    Marks offers older than the timeout as expired and re-offers those
    bookings. Call this from a cron job every minute.
    """
    cfg = get_config("job_offer_timeout_sec")
    timeout = int(_num(cfg.get("job_offer_timeout_sec"), 60))

    stale = db.fetch_all(
        """
        select distinct booking_id from booking_offers
         where response is null
           and offered_at < now() - (:sec || ' seconds')::interval
        """,
        {"sec": timeout},
    )

    db.execute(
        """
        update booking_offers set response = 'expired', responded_at = now()
         where response is null
           and offered_at < now() - (:sec || ' seconds')::interval
        """,
        {"sec": timeout},
    )

    for row in stale:
        booking = db.fetch_one(
            "select id from bookings where id = :b and status = 'confirmed' "
            "and assigned_partner_id is null",
            {"b": row["booking_id"]},
        )
        if booking:
            retry(row["booking_id"])

    return len(stale)


# ------------------------------------------------------------------
# Manual assignment (admin)
# ------------------------------------------------------------------
def manual_assign(booking_id: int, partner_id: int, admin_id: int) -> Dict[str, Any]:
    partner = db.fetch_one(
        "select * from partners where id = :id and status = 'approved'", {"id": partner_id}
    )
    if not partner:
        return {"success": False, "message": "Partner not found or not approved"}

    booking = db.fetch_one(
        "select *, status::text as status from bookings where id = :id", {"id": booking_id}
    )
    if not booking:
        return {"success": False, "message": "Booking not found"}
    if booking["status"] in ("completed", "paid", "cancelled", "rejected"):
        return {"success": False, "message": "This booking is already closed"}

    updated = db.execute(
        """
        update bookings
           set assigned_partner_id = :p, assigned_at = now(),
               status = case when status = 'confirmed' then 'assigned'::booking_status
                             else status end
         where id = :b
        returning *
        """,
        {"p": partner_id, "b": booking_id},
    )

    db.execute(
        """
        insert into booking_status_history
          (booking_id, from_status, to_status, actor, actor_id, note)
        values (:b, cast(:f as booking_status), 'assigned', 'admin', :a,
                'Manually assigned by admin')
        """,
        {"b": booking_id, "f": booking["status"], "a": admin_id},
    )

    fcm.notify_partner(
        partner_id, "new_job_offer",
        variables={
            "service": db.fetch_value(
                "select string_agg(service_name, ', ') from booking_items where booking_id = :b",
                {"b": booking_id},
            ) or "Service",
            "area": (booking["addr_snapshot"] or {}).get("area", ""),
            "payout": f"{float(booking['total']):.0f}",
            "code": booking["booking_code"],
        },
        data={"booking_id": str(booking_id), "type": "assigned"},
        urgent=True,
    )
    fcm.notify_user(
        booking["user_id"], "partner_assigned",
        variables={"partner": partner["name"], "code": booking["booking_code"]},
        data={"booking_id": str(booking_id)},
    )

    return {"success": True, "message": f"Assigned to {partner['name']}", "booking": updated}
