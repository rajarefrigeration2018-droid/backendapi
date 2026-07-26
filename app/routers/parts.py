# app/routers/parts.py
"""
Spare parts shop — an e-commerce module independent of the booking engine.

Customer side:
  GET  /parts/home             storefront in one call
  GET  /parts                  catalog with filters
  GET  /parts/{id}             product detail
  POST /parts/cart/preview     server-calculated cart total
  POST /parts/orders           place an order
  GET  /parts/orders           my orders
  POST /parts/orders/{id}/cancel

Admin side:
  full CRUD on categories and products, stock adjustment with an audit
  trail, bulk CSV import, and order fulfilment.

Stock rules:
  - Stock is decremented when the order is placed, inside a transaction.
  - Cancelling or returning an order puts the stock back.
  - Every change writes a `stock_movements` row, so shrinkage is traceable.
"""

import csv
import io
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core import fcm
from app.database import db
from app.dependencies import Pagination, fail, get_current_user, ok, require_permission
from app.services import pricing

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Spare Parts"])

PERM = "parts"


# ==================================================================
# SCHEMAS
# ==================================================================
class CartItem(BaseModel):
    part_id: int
    qty: int = Field(1, ge=1)


class CartPreviewIn(BaseModel):
    items: List[CartItem]
    coupon_code: Optional[str] = None


class OrderCreateIn(BaseModel):
    items: List[CartItem]
    address_id: int
    payment_mode: str = "cod"          # cod | online | wallet
    coupon_code: Optional[str] = None


class PartsCategoryIn(BaseModel):
    name: str
    icon_url: Optional[str] = None
    display_order: int = 0
    is_active: bool = True


class PartIn(BaseModel):
    category_id: Optional[int] = None
    brand_id: Optional[int] = None
    name: str
    sku: Optional[str] = None
    description: Optional[str] = None
    images: List[str] = []
    mrp: float
    sale_price: float
    stock_qty: int = 0
    min_stock_alert: int = 5
    weight_grams: Optional[int] = None
    warranty_text: Optional[str] = None
    is_active: bool = True


class StockAdjustIn(BaseModel):
    change_qty: int                      # positive = add, negative = remove
    reason: str = "restock"
    note: Optional[str] = None


class OrderStatusIn(BaseModel):
    status: str
    tracking_id: Optional[str] = None
    courier_name: Optional[str] = None
    note: Optional[str] = None


# ==================================================================
# STOREFRONT
# ==================================================================
@router.get("/parts/home")
def parts_home():
    if not pricing.get_config("enable_parts_shop").get("enable_parts_shop", True):
        fail("The spare parts shop is currently unavailable.", "SHOP_DISABLED", 503)

    categories = db.fetch_all(
        """
        select c.*, (select count(*) from parts p
                      where p.category_id = c.id and p.is_active) as product_count
          from parts_categories c
         where c.is_active
         order by c.display_order, c.id
        """
    )
    featured = _cast(
        db.fetch_all(
            """
            select p.id, p.name, p.images, p.mrp, p.sale_price, p.stock_qty,
                   b.name as brand_name
              from parts p left join brands b on b.id = p.brand_id
             where p.is_active and p.stock_qty > 0
             order by (p.mrp - p.sale_price) desc, p.id desc
             limit 10
            """
        )
    )
    banners = db.fetch_all(
        """
        select id, title, image_url, target_type, target_id, target_url
          from banners
         where is_active and screen = 'parts'
           and (start_at is null or start_at <= now())
           and (end_at   is null or end_at   >= now())
         order by display_order, id
        """
    )
    cfg = pricing.get_config(
        "parts_delivery_fee", "parts_free_delivery_above", "parts_cod_enabled"
    )

    return ok(
        {
            "banners": banners,
            "categories": categories,
            "featured": featured,
            "delivery": {
                "fee": pricing._num(cfg.get("parts_delivery_fee"), 0),
                "free_above": pricing._num(cfg.get("parts_free_delivery_above"), 0),
            },
            "cod_enabled": bool(cfg.get("parts_cod_enabled", True)),
        }
    )


@router.get("/parts/categories")
def parts_categories():
    return ok(
        db.fetch_all(
            "select * from parts_categories where is_active order by display_order, id"
        )
    )


@router.get("/parts")
def list_parts(
    category_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    search: Optional[str] = None,
    in_stock: bool = False,
    sort: str = Query("popular", description="popular | price_low | price_high | new"),
    page: int = 1,
    limit: int = 20,
):
    pg = Pagination(page, limit)
    where = ["p.is_active"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}

    if category_id:
        where.append("p.category_id = :cid")
        params["cid"] = category_id
    if brand_id:
        where.append("p.brand_id = :bid")
        params["bid"] = brand_id
    if in_stock:
        where.append("p.stock_qty > 0")
    if search:
        where.append("(p.name ilike :q or p.sku ilike :q or p.description ilike :q)")
        params["q"] = f"%{search}%"

    order = {
        "price_low": "p.sale_price asc",
        "price_high": "p.sale_price desc",
        "new": "p.id desc",
    }.get(sort, "p.stock_qty > 0 desc, p.id desc")

    clause = " and ".join(where)
    rows = _cast(
        db.fetch_all(
            f"""
            select p.id, p.category_id, p.name, p.sku, p.images, p.mrp, p.sale_price,
                   p.stock_qty, p.warranty_text, b.name as brand_name,
                   c.name as category_name
              from parts p
              left join brands b on b.id = p.brand_id
              left join parts_categories c on c.id = p.category_id
             where {clause}
             order by {order}
             limit :l offset :o
            """,
            params,
        )
    )
    total = db.fetch_value(
        f"select count(*) from parts p where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    return ok(pg.envelope(rows, int(total or 0)))


@router.get("/parts/{pid}")
def part_detail(pid: int):
    part = db.fetch_one(
        """
        select p.*, b.name as brand_name, b.logo_url as brand_logo,
               c.name as category_name
          from parts p
          left join brands b on b.id = p.brand_id
          left join parts_categories c on c.id = p.category_id
         where p.id = :id and p.is_active
        """,
        {"id": pid},
    )
    if not part:
        fail("Product not found", "NOT_FOUND", 404)

    related = _cast(
        db.fetch_all(
            """
            select id, name, images, mrp, sale_price, stock_qty
              from parts
             where is_active and category_id = :c and id <> :id and stock_qty > 0
             order by id desc limit 6
            """,
            {"c": part["category_id"], "id": pid},
        )
    )
    cfg = pricing.get_config("parts_return_days", "parts_max_qty_per_item")
    part = _cast([part])[0]

    return ok(
        {
            **part,
            "in_stock": part["stock_qty"] > 0,
            "low_stock": 0 < part["stock_qty"] <= 5,
            "max_qty": int(pricing._num(cfg.get("parts_max_qty_per_item"), 10)),
            "return_days": int(pricing._num(cfg.get("parts_return_days"), 7)),
            "related": related,
        }
    )


# ==================================================================
# CART & CHECKOUT
# ==================================================================
@router.post("/parts/cart/preview")
def cart_preview(body: CartPreviewIn, user=Depends(get_current_user)):
    try:
        breakup = _price_cart(body.items, user["id"], body.coupon_code)
    except ValueError as exc:
        fail(str(exc), "INVALID_CART")
    return ok(breakup)


@router.post("/parts/orders")
def create_order(body: OrderCreateIn, user=Depends(get_current_user)):
    if not pricing.get_config("enable_parts_shop").get("enable_parts_shop", True):
        fail("The spare parts shop is currently unavailable.", "SHOP_DISABLED", 503)

    address = db.fetch_one(
        "select * from user_addresses where id = :a and user_id = :u",
        {"a": body.address_id, "u": user["id"]},
    )
    if not address:
        fail("Address not found", "ADDRESS_NOT_FOUND", 404)

    cfg = pricing.get_config("enable_online_payment", "enable_wallet", "parts_cod_enabled")
    allowed = {
        "online": bool(cfg.get("enable_online_payment", True)),
        "wallet": bool(cfg.get("enable_wallet", True)),
        "cod": bool(cfg.get("parts_cod_enabled", True)),
    }
    if not allowed.get(body.payment_mode):
        fail("This payment method is currently unavailable.", "PAYMENT_MODE_DISABLED")

    try:
        breakup = _price_cart(body.items, user["id"], body.coupon_code)
    except ValueError as exc:
        fail(str(exc), "INVALID_CART")

    if body.coupon_code and not breakup["coupon"]["applied"]:
        fail(breakup["coupon"]["message"] or "Coupon could not be applied", "COUPON_INVALID")

    total = breakup["total"]
    if body.payment_mode == "wallet" and float(user["wallet_balance"]) < total:
        fail("Insufficient wallet balance.", "INSUFFICIENT_WALLET")

    snapshot = {
        k: address[k]
        for k in ("label", "house", "area", "landmark", "city", "state", "pincode", "lat", "lng")
    }
    code = db.fetch_value("select gen_part_order_code()")

    # ---------- everything below must succeed together ----------
    try:
      with db.transaction() as conn:
          order_row = conn.execute(
              text(
                  """
                  insert into part_orders
                    (order_code, user_id, addr_snapshot, status, payment_mode, payment_status,
                     subtotal, delivery_fee, discount, tax, total, coupon_code)
                  values
                    (:code, :u, cast(:snap as jsonb), 'placed', cast(:pm as payment_mode),
                     'pending', :sub, :del, :disc, :tax, :total, :ccode)
                  returning id
                  """
              ),
              {
                  "code": code, "u": user["id"], "snap": json.dumps(snapshot, default=str),
                  "pm": body.payment_mode, "sub": breakup["subtotal"],
                  "del": breakup["delivery_fee"], "disc": breakup["discount"],
                  "tax": breakup["tax"], "total": total,
                  "ccode": breakup["coupon"]["code"],
              },
          ).mappings().first()
          order_id = order_row["id"]

          for item in breakup["items"]:
              # conditional decrement — fails if someone else bought the last unit
              stock = conn.execute(
                  text(
                      """
                      update parts set stock_qty = stock_qty - :q
                       where id = :id and stock_qty >= :q
                      returning stock_qty
                      """
                  ),
                  {"q": item["qty"], "id": item["part_id"]},
              ).mappings().first()

              if not stock:
                  raise RuntimeError(f"OUT_OF_STOCK::{item['name']}")

              conn.execute(
                  text(
                      """
                      insert into part_order_items
                        (order_id, part_id, part_name, qty, unit_price, line_total)
                      values (:o, :p, :n, :q, :up, :lt)
                      """
                  ),
                  {
                      "o": order_id, "p": item["part_id"], "n": item["name"],
                      "q": item["qty"], "up": item["unit_price"], "lt": item["line_total"],
                  },
              )
              conn.execute(
                  text(
                      """
                      insert into stock_movements
                        (part_id, change_qty, qty_after, reason, ref_type, ref_id)
                      values (:p, :c, :after, 'sale', 'part_order', :ref)
                      """
                  ),
                  {
                      "p": item["part_id"], "c": -item["qty"],
                      "after": stock["stock_qty"], "ref": order_id,
                  },
              )

          conn.execute(
              text(
                  """
                  insert into part_order_history (order_id, to_status, note)
                  values (:o, 'placed', 'Order placed')
                  """
              ),
              {"o": order_id},
          )
    except RuntimeError as exc:
        msg = str(exc)
        if msg.startswith("OUT_OF_STOCK::"):
            fail(f"{msg.split('::', 1)[1]} just went out of stock.", "OUT_OF_STOCK", 409)
        raise

    # ---------- post-transaction side effects ----------
    if breakup["coupon"]["id"]:
        pricing.consume_coupon(
            breakup["coupon"]["id"], user["id"], breakup["discount"], order_id=order_id
        )

    if body.payment_mode == "wallet":
        updated = db.execute(
            "update users set wallet_balance = wallet_balance - :a where id = :u "
            "returning wallet_balance",
            {"a": total, "u": user["id"]},
        )
        db.execute(
            """
            insert into wallet_transactions
              (owner_type, owner_id, direction, amount, balance_after, reason,
               ref_type, ref_id)
            values ('user', :u, 'debit', :a, :b, :r, 'part_order', :ref)
            """,
            {"u": user["id"], "a": total, "b": updated["wallet_balance"],
             "r": f"Order {code}", "ref": order_id},
        )
        db.execute(
            "update part_orders set payment_status = 'paid' where id = :id", {"id": order_id}
        )

    fcm.notify_user(user["id"], "parts_order_placed", variables={"code": code})

    return ok(
        {
            "order_id": order_id,
            "order_code": code,
            "total": total,
            "needs_payment": body.payment_mode == "online",
        },
        "Order placed successfully",
    )


# ==================================================================
# MY ORDERS
# ==================================================================
@router.get("/parts/orders")
def my_orders(page: int = 1, limit: int = 20, user=Depends(get_current_user)):
    pg = Pagination(page, limit)
    rows = db.fetch_all(
        """
        select o.id, o.order_code, o.status::text as status, o.total,
               o.payment_mode::text as payment_mode,
               o.payment_status::text as payment_status,
               o.tracking_id, o.courier_name, o.created_at, o.delivered_at,
               (select count(*) from part_order_items i where i.order_id = o.id) as item_count,
               (select string_agg(part_name, ', ') from part_order_items i
                 where i.order_id = o.id) as items_summary
          from part_orders o
         where o.user_id = :u
         order by o.created_at desc
         limit :l offset :o
        """,
        {"u": user["id"], "l": pg.limit, "o": pg.offset},
    )
    total = db.fetch_value(
        "select count(*) from part_orders where user_id = :u", {"u": user["id"]}
    )
    for r in rows:
        r["total"] = float(r["total"])
    return ok(pg.envelope(rows, int(total or 0)))


@router.get("/parts/orders/{oid}")
def order_detail(oid: int, user=Depends(get_current_user)):
    order = db.fetch_one(
        """
        select *, status::text as status, payment_mode::text as payment_mode,
               payment_status::text as payment_status
          from part_orders where id = :id and user_id = :u
        """,
        {"id": oid, "u": user["id"]},
    )
    if not order:
        fail("Order not found", "NOT_FOUND", 404)

    items = db.fetch_all(
        """
        select i.*, p.images from part_order_items i
          left join parts p on p.id = i.part_id
         where i.order_id = :o
        """,
        {"o": oid},
    )
    timeline = db.fetch_all(
        "select to_status::text as status, note, created_at "
        "from part_order_history where order_id = :o order by created_at",
        {"o": oid},
    )
    for i in items:
        i["unit_price"] = float(i["unit_price"])
        i["line_total"] = float(i["line_total"])

    return ok(
        {
            "order": _cast([order])[0],
            "items": items,
            "timeline": timeline,
            "can_cancel": order["status"] in ("placed", "packed"),
        }
    )


class CancelOrderIn(BaseModel):
    reason: Optional[str] = None


@router.post("/parts/orders/{oid}/cancel")
def cancel_order(oid: int, body: CancelOrderIn, user=Depends(get_current_user)):
    order = db.fetch_one(
        """
        select *, status::text as status, payment_status::text as payment_status
          from part_orders where id = :id and user_id = :u
        """,
        {"id": oid, "u": user["id"]},
    )
    if not order:
        fail("Order not found", "NOT_FOUND", 404)
    if order["status"] not in ("placed", "packed"):
        fail("This order can no longer be cancelled.", "CANNOT_CANCEL")

    _restock(oid, "cancel")

    db.execute(
        "update part_orders set status = 'cancelled' where id = :id", {"id": oid}
    )
    db.execute(
        "insert into part_order_history (order_id, from_status, to_status, note) "
        "values (:o, cast(:f as order_status), 'cancelled', :n)",
        {"o": oid, "f": order["status"], "n": body.reason or "Cancelled by customer"},
    )

    refund = 0.0
    if order["payment_status"] == "paid":
        refund = float(order["total"])
        updated = db.execute(
            "update users set wallet_balance = wallet_balance + :a where id = :u "
            "returning wallet_balance",
            {"a": refund, "u": user["id"]},
        )
        db.execute(
            """
            insert into wallet_transactions
              (owner_type, owner_id, direction, amount, balance_after, reason,
               ref_type, ref_id)
            values ('user', :u, 'credit', :a, :b, :r, 'part_order', :ref)
            """,
            {"u": user["id"], "a": refund, "b": updated["wallet_balance"],
             "r": f"Refund for {order['order_code']}", "ref": oid},
        )

    fcm.notify_user(user["id"], "parts_order_cancelled",
                    variables={"code": order["order_code"]})

    return ok({"refund_to_wallet": refund}, "Order cancelled")


# ==================================================================
# ADMIN — CATEGORIES
# ==================================================================
@router.get("/admin/parts-categories", dependencies=[Depends(require_permission(PERM))])
def admin_list_categories():
    return ok(db.fetch_all("select * from parts_categories order by display_order, id"))


@router.post("/admin/parts-categories")
def admin_create_category(body: PartsCategoryIn, admin=Depends(require_permission(PERM))):
    row = db.execute(
        "insert into parts_categories (name, icon_url, display_order, is_active) "
        "values (:n, :i, :o, :a) returning *",
        {"n": body.name, "i": body.icon_url, "o": body.display_order, "a": body.is_active},
    )
    return ok(row, "Category created")


@router.put("/admin/parts-categories/{cid}")
def admin_update_category(
    cid: int, body: PartsCategoryIn, admin=Depends(require_permission(PERM))
):
    row = db.execute(
        "update parts_categories set name=:n, icon_url=:i, display_order=:o, is_active=:a "
        "where id = :id returning *",
        {"n": body.name, "i": body.icon_url, "o": body.display_order,
         "a": body.is_active, "id": cid},
    )
    if not row:
        fail("Category not found", "NOT_FOUND", 404)
    return ok(row, "Category updated")


@router.delete("/admin/parts-categories/{cid}")
def admin_delete_category(cid: int, admin=Depends(require_permission(PERM))):
    count = db.fetch_value("select count(*) from parts where category_id = :id", {"id": cid})
    if count and int(count) > 0:
        fail(f"This category has {count} products. Move or remove them first.", "HAS_PRODUCTS")
    db.execute("delete from parts_categories where id = :id", {"id": cid})
    return ok(None, "Category deleted")


# ==================================================================
# ADMIN — PRODUCTS
# ==================================================================
@router.get("/admin/parts", dependencies=[Depends(require_permission(PERM))])
def admin_list_parts(
    category_id: Optional[int] = None,
    low_stock: bool = False,
    search: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}
    if category_id:
        where.append("p.category_id = :cid")
        params["cid"] = category_id
    if low_stock:
        where.append("p.stock_qty <= p.min_stock_alert")
    if search:
        where.append("(p.name ilike :q or p.sku ilike :q)")
        params["q"] = f"%{search}%"

    clause = " and ".join(where)
    rows = _cast(
        db.fetch_all(
            f"""
            select p.*, c.name as category_name, b.name as brand_name,
                   (select coalesce(sum(i.qty), 0) from part_order_items i
                      join part_orders o on o.id = i.order_id
                     where i.part_id = p.id and o.status <> 'cancelled') as units_sold
              from parts p
              left join parts_categories c on c.id = p.category_id
              left join brands b on b.id = p.brand_id
             where {clause}
             order by p.id desc limit :l offset :o
            """,
            params,
        )
    )
    total = db.fetch_value(
        f"select count(*) from parts p where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    return ok(pg.envelope(rows, int(total or 0)))


@router.post("/admin/parts")
def admin_create_part(body: PartIn, admin=Depends(require_permission(PERM))):
    if body.sku and db.fetch_one("select 1 from parts where sku = :s", {"s": body.sku}):
        fail("This SKU already exists", "DUPLICATE_SKU", 409)
    if body.sale_price > body.mrp:
        fail("Sale price cannot be higher than MRP", "INVALID_PRICE")

    row = db.execute(_PART_INSERT, _part_params(body))

    if body.stock_qty > 0:
        _log_stock(row["id"], body.stock_qty, body.stock_qty, "restock", admin["id"])
    return ok(_cast([row])[0], "Product created")


@router.put("/admin/parts/{pid}")
def admin_update_part(pid: int, body: PartIn, admin=Depends(require_permission(PERM))):
    before = db.fetch_one("select * from parts where id = :id", {"id": pid})
    if not before:
        fail("Product not found", "NOT_FOUND", 404)
    if body.sale_price > body.mrp:
        fail("Sale price cannot be higher than MRP", "INVALID_PRICE")

    row = db.execute(_PART_UPDATE, {**_part_params(body), "id": pid})

    if body.stock_qty != before["stock_qty"]:
        _log_stock(
            pid, body.stock_qty - before["stock_qty"], body.stock_qty,
            "correction", admin["id"],
        )
    return ok(_cast([row])[0], "Product updated")


@router.delete("/admin/parts/{pid}")
def admin_delete_part(pid: int, admin=Depends(require_permission(PERM))):
    sold = db.fetch_value(
        "select count(*) from part_order_items where part_id = :id", {"id": pid}
    )
    if sold and int(sold) > 0:
        db.execute("update parts set is_active = false where id = :id", {"id": pid})
        return ok(None, "This product has past orders, so it was deactivated instead of deleted.")
    db.execute("delete from parts where id = :id", {"id": pid})
    return ok(None, "Product deleted")


@router.post("/admin/parts/{pid}/stock")
def admin_adjust_stock(pid: int, body: StockAdjustIn, admin=Depends(require_permission(PERM))):
    if body.change_qty == 0:
        fail("Change quantity cannot be zero", "INVALID_QTY")

    row = db.execute(
        """
        update parts set stock_qty = stock_qty + :c
         where id = :id and stock_qty + :c >= 0
        returning id, name, stock_qty
        """,
        {"c": body.change_qty, "id": pid},
    )
    if not row:
        fail("Stock cannot go below zero", "NEGATIVE_STOCK")

    _log_stock(pid, body.change_qty, row["stock_qty"], body.reason, admin["id"], body.note)
    return ok(
        {"stock_qty": row["stock_qty"]},
        f"Stock updated to {row['stock_qty']}",
    )


@router.get("/admin/parts/{pid}/stock-history", dependencies=[Depends(require_permission(PERM))])
def stock_history(pid: int, limit: int = 100):
    return ok(
        db.fetch_all(
            """
            select m.*, a.name as changed_by_name
              from stock_movements m left join admins a on a.id = m.created_by
             where m.part_id = :p
             order by m.created_at desc limit :l
            """,
            {"p": pid, "l": min(limit, 500)},
        )
    )


@router.post("/admin/parts/import")
async def bulk_import(
    file: UploadFile = File(...), admin=Depends(require_permission(PERM))
):
    """
    CSV columns (header row required):
      name, sku, category, brand, mrp, sale_price, stock_qty, description, warranty_text

    Existing SKUs are updated; new SKUs are created.
    """
    if not (file.filename or "").lower().endswith(".csv"):
        fail("Please upload a .csv file", "INVALID_FILE")

    try:
        content = (await file.read()).decode("utf-8-sig")
    except UnicodeDecodeError:
        fail("Could not read the file. Save it as UTF-8 CSV.", "ENCODING_ERROR")

    reader = csv.DictReader(io.StringIO(content))
    created = updated = skipped = 0
    errors: List[str] = []

    default_alert = int(
        pricing._num(
            pricing.get_config("parts_low_stock_default").get("parts_low_stock_default"), 5
        )
    )

    for line_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name = row.get("name")
        if not name:
            skipped += 1
            continue

        try:
            mrp = float(row.get("mrp") or 0)
            sale = float(row.get("sale_price") or mrp)
            qty = int(float(row.get("stock_qty") or 0))
        except ValueError:
            errors.append(f"Line {line_no}: invalid number")
            skipped += 1
            continue

        if sale > mrp:
            errors.append(f"Line {line_no}: sale price above MRP")
            skipped += 1
            continue

        cat_id = _lookup_id("parts_categories", row.get("category"))
        brand_id = _lookup_id("brands", row.get("brand"), create=True)
        sku = row.get("sku") or None

        existing = (
            db.fetch_one("select id, stock_qty from parts where sku = :s", {"s": sku})
            if sku else None
        )

        if existing:
            db.execute(
                """
                update parts
                   set name=:n, category_id=coalesce(:c, category_id),
                       brand_id=coalesce(:b, brand_id), description=:d,
                       mrp=:m, sale_price=:sp, stock_qty=:q,
                       warranty_text=:w, is_active=true
                 where id = :id
                """,
                {
                    "n": name, "c": cat_id, "b": brand_id,
                    "d": row.get("description"), "m": mrp, "sp": sale,
                    "q": qty, "w": row.get("warranty_text"), "id": existing["id"],
                },
            )
            if qty != existing["stock_qty"]:
                _log_stock(
                    existing["id"], qty - existing["stock_qty"], qty,
                    "correction", admin["id"], "CSV import",
                )
            updated += 1
        else:
            new = db.execute(
                """
                insert into parts
                  (category_id, brand_id, name, sku, description, mrp, sale_price,
                   stock_qty, min_stock_alert, warranty_text, is_active)
                values (:c, :b, :n, :s, :d, :m, :sp, :q, :alert, :w, true)
                returning id
                """,
                {
                    "c": cat_id, "b": brand_id, "n": name, "s": sku,
                    "d": row.get("description"), "m": mrp, "sp": sale, "q": qty,
                    "alert": default_alert, "w": row.get("warranty_text"),
                },
            )
            if qty > 0:
                _log_stock(new["id"], qty, qty, "restock", admin["id"], "CSV import")
            created += 1

    return ok(
        {"created": created, "updated": updated, "skipped": skipped, "errors": errors[:20]},
        f"{created} created, {updated} updated, {skipped} skipped",
    )


# ==================================================================
# ADMIN — ORDERS
# ==================================================================
@router.get("/admin/parts-orders", dependencies=[Depends(require_permission(PERM))])
def admin_list_orders(
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
):
    pg = Pagination(page, limit)
    where = ["1=1"]
    params: Dict[str, Any] = {"l": pg.limit, "o": pg.offset}
    if status:
        where.append("o.status = cast(:st as order_status)")
        params["st"] = status
    if search:
        where.append("(o.order_code ilike :q or u.phone ilike :q or u.name ilike :q)")
        params["q"] = f"%{search}%"

    clause = " and ".join(where)
    rows = db.fetch_all(
        f"""
        select o.*, o.status::text as status, o.payment_mode::text as payment_mode,
               o.payment_status::text as payment_status,
               u.name as customer_name, u.phone as customer_phone,
               (select string_agg(part_name || ' x' || qty, ', ')
                  from part_order_items i where i.order_id = o.id) as items_summary
          from part_orders o join users u on u.id = o.user_id
         where {clause}
         order by o.created_at desc limit :l offset :o
        """,
        params,
    )
    total = db.fetch_value(
        f"select count(*) from part_orders o join users u on u.id = o.user_id where {clause}",
        {k: v for k, v in params.items() if k not in ("l", "o")},
    )
    for r in rows:
        for k in ("subtotal", "delivery_fee", "discount", "tax", "total"):
            r[k] = float(r[k])
    return ok(pg.envelope(rows, int(total or 0)))


@router.post("/admin/parts-orders/{oid}/status")
def admin_update_order_status(
    oid: int, body: OrderStatusIn, admin=Depends(require_permission(PERM))
):
    valid = {"placed", "packed", "shipped", "delivered", "cancelled", "returned"}
    if body.status not in valid:
        fail("Invalid status", "INVALID_STATUS")

    order = db.fetch_one(
        """
        select *, status::text as status, payment_status::text as payment_status
          from part_orders where id = :id
        """,
        {"id": oid},
    )
    if not order:
        fail("Order not found", "NOT_FOUND", 404)

    if body.status in ("cancelled", "returned") and order["status"] not in (
        "cancelled", "returned"
    ):
        _restock(oid, body.status)

    db.execute(
        """
        update part_orders
           set status = cast(:s as order_status),
               tracking_id = coalesce(:t, tracking_id),
               courier_name = coalesce(:c, courier_name),
               delivered_at = case when :s = 'delivered' then now() else delivered_at end,
               payment_status = case
                 when :s = 'delivered' and payment_mode = 'cod' then 'paid'::payment_status
                 else payment_status end
         where id = :id
        """,
        {"s": body.status, "t": body.tracking_id, "c": body.courier_name, "id": oid},
    )
    db.execute(
        """
        insert into part_order_history (order_id, from_status, to_status, note, changed_by)
        values (:o, cast(:f as order_status), cast(:t as order_status), :n, :a)
        """,
        {"o": oid, "f": order["status"], "t": body.status,
         "n": body.note, "a": admin["id"]},
    )

    event = {
        "shipped": "parts_order_shipped",
        "delivered": "parts_order_delivered",
        "cancelled": "parts_order_cancelled",
    }.get(body.status)
    if event:
        fcm.notify_user(
            order["user_id"], event,
            variables={
                "code": order["order_code"],
                "tracking": body.tracking_id or order.get("tracking_id") or "",
            },
        )

    return ok(None, f"Order marked as {body.status}")


# ==================================================================
# HELPERS
# ==================================================================
_PART_COLS = """
    category_id=:c, brand_id=:b, name=:n, sku=:s, description=:d, images=:img,
    mrp=:m, sale_price=:sp, stock_qty=:q, min_stock_alert=:alert,
    weight_grams=:w, warranty_text=:wt, is_active=:a
"""

_PART_INSERT = """
    insert into parts (category_id, brand_id, name, sku, description, images, mrp,
                       sale_price, stock_qty, min_stock_alert, weight_grams,
                       warranty_text, is_active)
    values (:c, :b, :n, :s, :d, :img, :m, :sp, :q, :alert, :w, :wt, :a)
    returning *
"""

_PART_UPDATE = f"update parts set {_PART_COLS} where id = :id returning *"


def _part_params(body: "PartIn") -> Dict[str, Any]:
    return {
        "c": body.category_id, "b": body.brand_id, "n": body.name, "s": body.sku,
        "d": body.description, "img": body.images, "m": body.mrp,
        "sp": body.sale_price, "q": body.stock_qty, "alert": body.min_stock_alert,
        "w": body.weight_grams, "wt": body.warranty_text, "a": body.is_active,
    }


def _cast(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for r in rows:
        for k in ("mrp", "sale_price", "subtotal", "delivery_fee", "discount", "tax", "total"):
            if k in r and r[k] is not None:
                r[k] = float(r[k])
        if "mrp" in r and "sale_price" in r and r["mrp"]:
            r["discount_percent"] = round((r["mrp"] - r["sale_price"]) / r["mrp"] * 100)
    return rows


def _lookup_id(table: str, name: Optional[str], create: bool = False) -> Optional[int]:
    if not name:
        return None
    row = db.fetch_one(f"select id from {table} where lower(name) = lower(:n)", {"n": name})
    if row:
        return row["id"]
    if create:
        new = db.execute(f"insert into {table} (name) values (:n) returning id", {"n": name})
        return new["id"]
    return None


def _log_stock(
    part_id: int, change: int, after: int, reason: str,
    admin_id: Optional[int] = None, note: Optional[str] = None,
) -> None:
    db.execute(
        """
        insert into stock_movements
          (part_id, change_qty, qty_after, reason, ref_type, created_by)
        values (:p, :c, :a, :r, :note, :by)
        """,
        {"p": part_id, "c": change, "a": after, "r": reason,
         "note": note, "by": admin_id},
    )


def _restock(order_id: int, reason: str) -> None:
    """Puts stock back when an order is cancelled or returned."""
    items = db.fetch_all(
        "select part_id, qty from part_order_items where order_id = :o", {"o": order_id}
    )
    for it in items:
        row = db.execute(
            "update parts set stock_qty = stock_qty + :q where id = :id returning stock_qty",
            {"q": it["qty"], "id": it["part_id"]},
        )
        if row:
            db.execute(
                """
                insert into stock_movements
                  (part_id, change_qty, qty_after, reason, ref_type, ref_id)
                values (:p, :c, :a, :r, 'part_order', :ref)
                """,
                {"p": it["part_id"], "c": it["qty"], "a": row["stock_qty"],
                 "r": reason, "ref": order_id},
            )


def _price_cart(
    items: List[CartItem], user_id: int, coupon_code: Optional[str]
) -> Dict[str, Any]:
    """Server-side cart pricing. Raises ValueError with a user-safe message."""
    if not items:
        raise ValueError("Your cart is empty")

    cfg = pricing.get_config(
        "gst_percent", "parts_delivery_fee", "parts_free_delivery_above",
        "parts_max_qty_per_item",
    )
    tax_percent = pricing._num(cfg.get("gst_percent"), 0)
    delivery_fee = pricing._num(cfg.get("parts_delivery_fee"), 0)
    free_above = pricing._num(cfg.get("parts_free_delivery_above"), 0)
    max_qty = int(pricing._num(cfg.get("parts_max_qty_per_item"), 10))

    subtotal = 0.0
    resolved: List[Dict[str, Any]] = []

    for it in items:
        part = db.fetch_one(
            "select id, name, sale_price, stock_qty, is_active from parts where id = :id",
            {"id": it.part_id},
        )
        if not part:
            raise ValueError(f"Product #{it.part_id} not found")
        if not part["is_active"]:
            raise ValueError(f"{part['name']} is no longer available")
        if it.qty > max_qty:
            raise ValueError(f"You can order at most {max_qty} of {part['name']}")
        if part["stock_qty"] < it.qty:
            raise ValueError(
                f"Only {part['stock_qty']} left of {part['name']}"
                if part["stock_qty"] > 0
                else f"{part['name']} is out of stock"
            )

        unit = float(part["sale_price"])
        line = unit * it.qty
        subtotal += line
        resolved.append(
            {
                "part_id": part["id"], "name": part["name"], "qty": it.qty,
                "unit_price": round(unit, 2), "line_total": round(line, 2),
            }
        )

    discount = 0.0
    coupon_info: Dict[str, Any] = {"id": None, "code": None, "applied": False, "message": None}
    if coupon_code:
        coupon = db.fetch_one(
            "select * from coupons where upper(code) = upper(:c) and is_active",
            {"c": coupon_code},
        )
        if not coupon:
            coupon_info["message"] = "Invalid coupon code"
        elif subtotal < pricing._num(coupon["min_order"]):
            short = pricing._num(coupon["min_order"]) - subtotal
            coupon_info["message"] = f"Add Rs. {short:.0f} more to use this coupon"
        else:
            used = db.fetch_value(
                "select count(*) from coupon_redemptions where coupon_id = :c and user_id = :u",
                {"c": coupon["id"], "u": user_id},
            )
            if used and int(used) >= (coupon["per_user_limit"] or 1):
                coupon_info["message"] = "You have already used this coupon"
            else:
                if coupon["type"] == "percent":
                    discount = subtotal * pricing._num(coupon["value"]) / 100
                    if coupon["max_discount"]:
                        discount = min(discount, pricing._num(coupon["max_discount"]))
                else:
                    discount = pricing._num(coupon["value"])
                discount = round(min(discount, subtotal), 2)
                coupon_info = {
                    "id": coupon["id"], "code": coupon["code"], "applied": discount > 0,
                    "message": f"You saved Rs. {discount:.0f}!",
                }

    shipping = 0.0 if (free_above and subtotal >= free_above) else delivery_fee
    taxable = max(0.0, subtotal - discount)
    tax = round(taxable * tax_percent / 100, 2)
    total = round(taxable + tax + shipping, 2)

    return {
        "items": resolved,
        "subtotal": round(subtotal, 2),
        "delivery_fee": round(shipping, 2),
        "discount": round(discount, 2),
        "tax": tax,
        "tax_percent": tax_percent,
        "total": total,
        "coupon": coupon_info,
        "free_delivery_at": free_above,
        "free_delivery_short": (
            round(max(0.0, free_above - subtotal), 2) if free_above and shipping > 0 else 0
        ),
    }
