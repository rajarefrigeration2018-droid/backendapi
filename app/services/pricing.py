# app/services/pricing.py
"""
Pricing engine — the ONLY place where a booking total is calculated.

The apps never compute money. They send items, we return the breakup.
This prevents a tampered client from paying whatever it likes.

Every rate (GST, visit charge behaviour) is read from app_config, so the
admin can change pricing rules without a deploy.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.database import db


@dataclass
class LineItem:
    service_id: int
    option_id: Optional[int] = None
    qty: int = 1
    # filled by the engine
    service_name: str = ""
    option_name: Optional[str] = None
    category_id: int = 0
    unit_price: float = 0.0
    line_total: float = 0.0
    visit_charge: float = 0.0
    price_type: str = "fixed"


@dataclass
class PriceBreakup:
    items: List[Dict[str, Any]] = field(default_factory=list)
    subtotal: float = 0.0
    visit_charge: float = 0.0
    discount: float = 0.0
    taxable: float = 0.0
    tax: float = 0.0
    tax_percent: float = 0.0
    total: float = 0.0
    coupon_id: Optional[int] = None
    coupon_code: Optional[str] = None
    coupon_message: Optional[str] = None
    coupon_applied: bool = False
    wallet_usable: float = 0.0
    has_inspection_item: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "items": self.items,
            "subtotal": round(self.subtotal, 2),
            "visit_charge": round(self.visit_charge, 2),
            "discount": round(self.discount, 2),
            "taxable": round(self.taxable, 2),
            "tax": round(self.tax, 2),
            "tax_percent": self.tax_percent,
            "total": round(self.total, 2),
            "coupon": {
                "id": self.coupon_id,
                "code": self.coupon_code,
                "applied": self.coupon_applied,
                "message": self.coupon_message,
            },
            "wallet_usable": round(self.wallet_usable, 2),
            "has_inspection_item": self.has_inspection_item,
            "breakup_lines": self._display_lines(),
        }

    def _display_lines(self) -> List[Dict[str, Any]]:
        """Ready-to-render rows for the cart screen."""
        lines = [{"label": "Item total", "value": round(self.subtotal, 2)}]
        if self.visit_charge > 0:
            lines.append({"label": "Visit charge", "value": round(self.visit_charge, 2)})
        if self.discount > 0:
            lines.append(
                {
                    "label": f"Discount ({self.coupon_code})" if self.coupon_code else "Discount",
                    "value": -round(self.discount, 2),
                    "highlight": True,
                }
            )
        if self.tax > 0:
            lines.append({"label": f"GST ({self.tax_percent:g}%)", "value": round(self.tax, 2)})
        lines.append({"label": "Total", "value": round(self.total, 2), "bold": True})
        return lines


# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------
def get_config(*keys: str) -> Dict[str, Any]:
    rows = db.fetch_all(
        "select key, value from app_config where key = any(:k)", {"k": list(keys)}
    )
    return {r["key"]: r["value"] for r in rows}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------
def calculate(
    items: List[Dict[str, Any]],
    user_id: Optional[int] = None,
    coupon_code: Optional[str] = None,
    use_wallet: bool = False,
) -> PriceBreakup:
    """
    items = [{"service_id": 2, "option_id": 5, "qty": 1}, ...]
    Raises ValueError with a user-safe message on bad input.
    """
    if not items:
        raise ValueError("Cart is empty")

    cfg = get_config("gst_percent", "default_visit_charge", "charge_visit_once")
    tax_percent = _num(cfg.get("gst_percent"), 0)
    charge_visit_once = cfg.get("charge_visit_once", True)

    breakup = PriceBreakup(tax_percent=tax_percent)
    resolved: List[LineItem] = []

    # ---------- resolve every line from the database ----------
    for raw in items:
        sid = int(raw.get("service_id", 0))
        oid = raw.get("option_id")
        qty = max(1, int(raw.get("qty", 1)))

        svc = db.fetch_one(
            """
            select id, category_id, name, base_price, visit_charge,
                   price_type::text as price_type, is_active
              from services where id = :id
            """,
            {"id": sid},
        )
        if not svc:
            raise ValueError(f"Service #{sid} not found")
        if not svc["is_active"]:
            raise ValueError(f"{svc['name']} is currently unavailable")

        unit = _num(svc["base_price"])
        option_name = None

        if oid:
            opt = db.fetch_one(
                "select id, name, extra_price, is_active from service_options "
                "where id = :id and service_id = :sid",
                {"id": int(oid), "sid": sid},
            )
            if not opt:
                raise ValueError(f"Selected option is not valid for {svc['name']}")
            if not opt["is_active"]:
                raise ValueError(f"{opt['name']} is currently unavailable")
            unit += _num(opt["extra_price"])
            option_name = opt["name"]

        line = LineItem(
            service_id=sid,
            option_id=int(oid) if oid else None,
            qty=qty,
            service_name=svc["name"],
            option_name=option_name,
            category_id=svc["category_id"],
            unit_price=unit,
            line_total=unit * qty,
            visit_charge=_num(svc["visit_charge"]),
            price_type=svc["price_type"],
        )
        resolved.append(line)

        breakup.subtotal += line.line_total
        if line.price_type == "inspection_based":
            breakup.has_inspection_item = True

        breakup.items.append(
            {
                "service_id": sid,
                "option_id": line.option_id,
                "service_name": line.service_name,
                "option_name": line.option_name,
                "qty": qty,
                "unit_price": round(unit, 2),
                "line_total": round(line.line_total, 2),
                "price_type": line.price_type,
            }
        )

    # ---------- visit charge ----------
    charges = [l.visit_charge for l in resolved if l.visit_charge > 0]
    if charges:
        breakup.visit_charge = max(charges) if charge_visit_once else sum(charges)

    # ---------- coupon ----------
    if coupon_code:
        discount, coupon, message = apply_coupon(
            coupon_code.strip().upper(), breakup.subtotal, resolved, user_id
        )
        breakup.discount = discount
        breakup.coupon_message = message
        if coupon:
            breakup.coupon_id = coupon["id"]
            breakup.coupon_code = coupon["code"]
            breakup.coupon_applied = discount > 0

    # ---------- tax & total ----------
    breakup.taxable = max(0.0, breakup.subtotal + breakup.visit_charge - breakup.discount)
    breakup.tax = round(breakup.taxable * tax_percent / 100, 2)
    breakup.total = round(breakup.taxable + breakup.tax, 2)

    # ---------- wallet ----------
    if use_wallet and user_id:
        bal = _num(
            db.fetch_value("select wallet_balance from users where id = :id", {"id": user_id})
        )
        breakup.wallet_usable = min(bal, breakup.total)

    return breakup


# ------------------------------------------------------------------
# Coupons
# ------------------------------------------------------------------
def apply_coupon(
    code: str,
    subtotal: float,
    items: List[LineItem],
    user_id: Optional[int],
) -> Tuple[float, Optional[Dict[str, Any]], str]:
    """Returns (discount_amount, coupon_row_or_None, message)."""
    coupon = db.fetch_one(
        "select * from coupons where upper(code) = :c", {"c": code}
    )
    if not coupon:
        return 0.0, None, "Coupon code galat hai"
    if not coupon["is_active"]:
        return 0.0, None, "Ye coupon ab valid nahi hai"

    now = datetime.now(timezone.utc)
    if coupon["valid_from"] and coupon["valid_from"] > now:
        return 0.0, coupon, "Ye coupon abhi shuru nahi hua"
    if coupon["valid_to"] and coupon["valid_to"] < now:
        return 0.0, coupon, "Ye coupon expire ho chuka hai"

    if coupon["usage_limit"] is not None and coupon["used_count"] >= coupon["usage_limit"]:
        return 0.0, coupon, "Is coupon ki limit khatam ho gayi"

    min_order = _num(coupon["min_order"])
    if subtotal < min_order:
        short = min_order - subtotal
        return 0.0, coupon, f"₹{short:.0f} aur add karein to ye coupon lag jayega"

    if user_id:
        used = db.fetch_value(
            "select count(*) from coupon_redemptions where coupon_id = :c and user_id = :u",
            {"c": coupon["id"], "u": user_id},
        )
        if used and int(used) >= (coupon["per_user_limit"] or 1):
            return 0.0, coupon, "Aap ye coupon pehle hi use kar chuke hain"

        if coupon["first_order_only"]:
            prior = db.fetch_value(
                "select count(*) from bookings where user_id = :u "
                "and status not in ('cancelled','rejected')",
                {"u": user_id},
            )
            if prior and int(prior) > 0:
                return 0.0, coupon, "Ye coupon sirf pehli booking par valid hai"

    # ---------- scope check ----------
    allowed_services = coupon["applicable_service_ids"] or []
    allowed_cats = coupon["applicable_category_ids"] or []
    eligible_amount = subtotal

    if allowed_services or allowed_cats:
        eligible_amount = sum(
            l.line_total
            for l in items
            if (allowed_services and l.service_id in allowed_services)
            or (allowed_cats and l.category_id in allowed_cats)
        )
        if eligible_amount <= 0:
            return 0.0, coupon, "Ye coupon in services par valid nahi hai"

    # ---------- compute ----------
    if coupon["type"] == "percent":
        discount = eligible_amount * _num(coupon["value"]) / 100
        cap = coupon["max_discount"]
        if cap is not None:
            discount = min(discount, _num(cap))
    else:
        discount = _num(coupon["value"])

    discount = round(min(discount, eligible_amount), 2)
    if discount <= 0:
        return 0.0, coupon, "Is order par discount lagu nahi hota"

    return discount, coupon, f"₹{discount:.0f} ki bachat!"


def consume_coupon(
    coupon_id: int, user_id: int, discount: float,
    booking_id: Optional[int] = None, order_id: Optional[int] = None,
) -> None:
    """Called only after a booking/order is actually created."""
    db.execute(
        "update coupons set used_count = used_count + 1 where id = :id", {"id": coupon_id}
    )
    db.execute(
        """
        insert into coupon_redemptions (coupon_id, user_id, booking_id, order_id, discount)
        values (:c, :u, :b, :o, :d)
        """,
        {"c": coupon_id, "u": user_id, "b": booking_id, "o": order_id, "d": discount},
    )


def release_coupon(booking_id: int) -> None:
    """Give the coupon back when a booking is cancelled before it was served."""
    red = db.fetch_one(
        "select * from coupon_redemptions where booking_id = :b", {"b": booking_id}
    )
    if not red:
        return
    db.execute(
        "update coupons set used_count = greatest(0, used_count - 1) where id = :id",
        {"id": red["coupon_id"]},
    )
    db.execute("delete from coupon_redemptions where id = :id", {"id": red["id"]})


# ------------------------------------------------------------------
# Recalculation after the mechanic adds extra charges
# ------------------------------------------------------------------
def recalculate_booking(booking_id: int) -> Dict[str, Any]:
    """
    Re-totals a booking including approved extra charges.
    Called when the partner adds a charge and the customer approves it.
    """
    booking = db.fetch_one("select * from bookings where id = :id", {"id": booking_id})
    if not booking:
        raise ValueError("Booking not found")

    extras = _num(
        db.fetch_value(
            """
            select coalesce(sum(amount), 0) from booking_extra_charges
             where booking_id = :b and approved_by_user = true and rejected = false
            """,
            {"b": booking_id},
        )
    )

    subtotal = _num(booking["subtotal"])
    visit = _num(booking["visit_charge"])
    discount = _num(booking["discount"])
    tax_percent = _num(get_config("gst_percent").get("gst_percent"), 0)

    taxable = max(0.0, subtotal + extras + visit - discount)
    tax = round(taxable * tax_percent / 100, 2)
    total = round(taxable + tax, 2)

    updated = db.execute(
        """
        update bookings
           set extra_charges_total = :ex, tax = :tax, total = :total
         where id = :id
        returning *
        """,
        {"ex": extras, "tax": tax, "total": total, "id": booking_id},
    )
    return updated
