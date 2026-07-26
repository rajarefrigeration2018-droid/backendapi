# app/routers/config.py
"""
/app-config — the single endpoint that drives ALL hardcode-free behaviour
in the user app, partner app and website.

Apps call this on splash and cache it. Admin edits a value here and every
app picks it up on next launch — no redeploy, no app update.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.database import db
from app.dependencies import fail, ok, require_permission

router = APIRouter(tags=["Config"])


# ==================================================================
# PUBLIC
# ==================================================================
@router.get("/app-config")
def get_app_config(
    platform: str = Query("user", description="user | partner | web"),
    app_version: Optional[str] = Query(None, description="Caller's app version, e.g. 1.0.0"),
):
    """
    Returns every public config value plus a resolved update/maintenance verdict.
    Apps should call this before anything else.
    """
    rows = db.fetch_all(
        "select key, value, group_name, data_type from app_config where is_public = true"
    )
    cfg: Dict[str, Any] = {r["key"]: r["value"] for r in rows}

    # ---- force update check ----
    version_key = "min_partner_app_version" if platform == "partner" else "min_user_app_version"
    min_version = str(cfg.get(version_key, "1.0.0"))
    update_required = False
    if app_version:
        update_required = _version_lt(app_version, min_version)

    # ---- serviceable cities (for the location picker) ----
    cities = db.fetch_all(
        "select distinct city, state from service_areas where is_active order by city"
    )

    payload = {
        "config": cfg,
        "grouped": _group(rows),
        "update": {
            "min_version": min_version,
            "update_required": update_required,
            "force_update": bool(cfg.get("force_update", False)),
            "message": "A new version is available. Please update the app.",
        },
        "maintenance": {
            "enabled": bool(cfg.get("maintenance_mode", False)),
            "message": cfg.get("maintenance_message", ""),
        },
        "payment_methods": {
            "online": bool(cfg.get("enable_online_payment", True)),
            "cod": bool(cfg.get("enable_cod", True)),
            "wallet": bool(cfg.get("enable_wallet", True)),
        },
        "cities": cities,
    }
    return ok(payload)


@router.get("/service-areas/check")
def check_serviceability(pincode: str = Query(..., min_length=6, max_length=6)):
    row = db.fetch_one(
        "select pincode, city, state, is_active from service_areas where pincode = :p",
        {"p": pincode},
    )
    if not row or not row["is_active"]:
        return ok(
            {"serviceable": False, "pincode": pincode},
            "We don't serve this area yet.",
        )
    return ok({"serviceable": True, **row}, f"Great! We serve {row['city']}.")


@router.get("/service-areas")
def list_service_areas(city: Optional[str] = None):
    sql = "select * from service_areas where is_active"
    params: Dict[str, Any] = {}
    if city:
        sql += " and lower(city) = lower(:city)"
        params["city"] = city
    sql += " order by city, pincode"
    return ok(db.fetch_all(sql, params))


# ==================================================================
# ADMIN
# ==================================================================
class ConfigUpdate(BaseModel):
    value: Any


class ConfigCreate(BaseModel):
    key: str
    value: Any
    group_name: str = "general"
    data_type: str = "string"
    label: Optional[str] = None
    description: Optional[str] = None
    is_public: bool = True


@router.get("/admin/config", dependencies=[Depends(require_permission("config"))])
def admin_list_config():
    """Full list including private keys, grouped for the settings screen."""
    rows = db.fetch_all("select * from app_config order by group_name, key")
    return ok({"grouped": _group(rows), "flat": rows})


@router.put("/admin/config/{key}")
def admin_update_config(
    key: str,
    body: ConfigUpdate,
    admin: Dict[str, Any] = Depends(require_permission("config")),
):
    existing = db.fetch_one("select * from app_config where key = :k", {"k": key})
    if not existing:
        fail(f"Config key '{key}' not found", "CONFIG_NOT_FOUND", 404)

    updated = db.execute(
        """
        update app_config
           set value = cast(:v as jsonb), updated_at = now()
         where key = :k
        returning *
        """,
        {"k": key, "v": _to_json(body.value)},
    )

    _audit(admin, "update", "app_config", key, existing, updated)
    return ok(updated, "Config updated")


@router.post("/admin/config")
def admin_create_config(
    body: ConfigCreate,
    admin: Dict[str, Any] = Depends(require_permission("config")),
):
    if db.fetch_one("select 1 from app_config where key = :k", {"k": body.key}):
        fail("This key already exists", "DUPLICATE_KEY", 409)

    created = db.execute(
        """
        insert into app_config (key, value, group_name, data_type, label, description, is_public)
        values (:k, cast(:v as jsonb), :g, :d, :l, :desc, :pub)
        returning *
        """,
        {
            "k": body.key,
            "v": _to_json(body.value),
            "g": body.group_name,
            "d": body.data_type,
            "l": body.label,
            "desc": body.description,
            "pub": body.is_public,
        },
    )
    _audit(admin, "create", "app_config", body.key, None, created)
    return ok(created, "Config created")


@router.delete("/admin/config/{key}")
def admin_delete_config(
    key: str,
    admin: Dict[str, Any] = Depends(require_permission("config")),
):
    existing = db.fetch_one("select * from app_config where key = :k", {"k": key})
    if not existing:
        fail("Config key not found", "CONFIG_NOT_FOUND", 404)

    db.execute("delete from app_config where key = :k", {"k": key})
    _audit(admin, "delete", "app_config", key, existing, None)
    return ok(None, "Config deleted")


# ==================================================================
# HELPERS
# ==================================================================
def _group(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["group_name"], []).append(r)
    return out


def _to_json(value: Any) -> str:
    import json

    return json.dumps(value)


def _version_lt(current: str, minimum: str) -> bool:
    """True if current < minimum. Handles '1.2.10' style versions safely."""

    def parts(v: str) -> List[int]:
        out = []
        for p in str(v).split("."):
            digits = "".join(ch for ch in p if ch.isdigit())
            out.append(int(digits) if digits else 0)
        while len(out) < 3:
            out.append(0)
        return out[:3]

    return parts(current) < parts(minimum)


def _audit(admin: Dict[str, Any], action: str, entity: str, entity_id: Any, before, after) -> None:
    import json

    db.execute(
        """
        insert into audit_logs (actor_type, actor_id, action, entity, entity_id, before, after)
        values ('admin', :aid, :act, :ent, null, cast(:b as jsonb), cast(:a as jsonb))
        """,
        {
            "aid": admin["id"],
            "act": f"{action}:{entity_id}",
            "ent": entity,
            "b": json.dumps(before, default=str) if before else None,
            "a": json.dumps(after, default=str) if after else None,
        },
    )
