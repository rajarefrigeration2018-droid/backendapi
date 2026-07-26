# app/core/fcm.py
"""
Push notifications via Firebase Cloud Messaging.

Reuses the same service account as Firebase Phone Auth — no extra credential.

Notification wording lives in the `notification_templates` table so the
admin can reword any message without a deploy. Templates support
{{variable}} placeholders.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from app.core.firebase import _init
from app.database import db

logger = logging.getLogger(__name__)

# High-priority channel used by the partner app for new job alerts
JOB_CHANNEL = "mistrio_jobs"
DEFAULT_CHANNEL = "mistrio_default"


# ------------------------------------------------------------------
# Template rendering
# ------------------------------------------------------------------
def render(text: str, variables: Dict[str, Any]) -> str:
    def sub(match: "re.Match") -> str:
        key = match.group(1).strip()
        return str(variables.get(key, ""))

    return re.sub(r"\{\{([^}]+)\}\}", sub, text or "")


def get_template(event_key: str) -> Optional[Dict[str, Any]]:
    return db.fetch_one(
        "select * from notification_templates where event_key = :k and is_active",
        {"k": event_key},
    )


# ------------------------------------------------------------------
# Low-level send
# ------------------------------------------------------------------
def send_to_token(
    token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, str]] = None,
    image: Optional[str] = None,
    channel: str = DEFAULT_CHANNEL,
    high_priority: bool = False,
) -> bool:
    if not token:
        return False
    if not _init():
        logger.warning("FCM not configured — skipping push: %s", title)
        return False

    try:
        from firebase_admin import messaging

        message = messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body, image=image),
            data={k: str(v) for k, v in (data or {}).items()},
            android=messaging.AndroidConfig(
                priority="high" if high_priority else "normal",
                notification=messaging.AndroidNotification(
                    channel_id=channel,
                    sound="default",
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                ),
            ),
        )
        messaging.send(message)
        return True

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # a dead token should be cleared so we stop trying
        if "not-registered" in msg.lower() or "invalid-argument" in msg.lower():
            db.execute("update users set fcm_token = null where fcm_token = :t", {"t": token})
            db.execute("update partners set fcm_token = null where fcm_token = :t", {"t": token})
        logger.warning("FCM send failed: %s", msg)
        return False


def send_multicast(
    tokens: List[str], title: str, body: str, data: Optional[Dict[str, str]] = None, **kw
) -> int:
    sent = 0
    for t in set(filter(None, tokens)):
        if send_to_token(t, title, body, data, **kw):
            sent += 1
    return sent


# ------------------------------------------------------------------
# High-level: notify by event key
# ------------------------------------------------------------------
def notify_user(
    user_id: int, event_key: str, variables: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, str]] = None,
) -> bool:
    tpl = get_template(event_key)
    if not tpl:
        logger.warning("No notification template for '%s'", event_key)
        return False

    user = db.fetch_one("select fcm_token from users where id = :id", {"id": user_id})
    variables = variables or {}
    title = render(tpl["title"], variables)
    body = render(tpl["body"], variables)
    deeplink = render(tpl["deeplink"] or "", variables)

    _store_notification("user", user_id, title, body, deeplink, event_key)

    return send_to_token(
        (user or {}).get("fcm_token"),
        title, body,
        {**(data or {}), "deeplink": deeplink, "event": event_key},
    )


def notify_partner(
    partner_id: int, event_key: str, variables: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, str]] = None, urgent: bool = False,
) -> bool:
    tpl = get_template(event_key)
    if not tpl:
        logger.warning("No notification template for '%s'", event_key)
        return False

    partner = db.fetch_one("select fcm_token from partners where id = :id", {"id": partner_id})
    variables = variables or {}
    title = render(tpl["title"], variables)
    body = render(tpl["body"], variables)
    deeplink = render(tpl["deeplink"] or "", variables)

    _store_notification("partner", partner_id, title, body, deeplink, event_key)

    return send_to_token(
        (partner or {}).get("fcm_token"),
        title, body,
        {**(data or {}), "deeplink": deeplink, "event": event_key},
        channel=JOB_CHANNEL if urgent else DEFAULT_CHANNEL,
        high_priority=urgent,
    )


def broadcast(
    audience: str, title: str, body: str,
    image: Optional[str] = None, deeplink: Optional[str] = None, sent_by: Optional[int] = None,
) -> int:
    """audience: all_users | all_partners"""
    if audience == "all_users":
        rows = db.fetch_all(
            "select fcm_token from users where fcm_token is not null and not is_blocked"
        )
    else:
        rows = db.fetch_all(
            "select fcm_token from partners where fcm_token is not null and status = 'approved'"
        )

    db.execute(
        """
        insert into notifications (audience, title, body, image_url, deeplink, sent_by)
        values (:a, :t, :b, :img, :dl, :by)
        """,
        {"a": audience, "t": title, "b": body, "img": image, "dl": deeplink, "by": sent_by},
    )

    return send_multicast(
        [r["fcm_token"] for r in rows], title, body,
        {"deeplink": deeplink or ""}, image=image,
    )


def _store_notification(
    audience: str, target_id: int, title: str, body: str,
    deeplink: Optional[str], event_key: str,
) -> None:
    db.execute(
        """
        insert into notifications (audience, target_id, title, body, deeplink, event_key)
        values (:a, :t, :ti, :b, :dl, :ek)
        """,
        {"a": audience, "t": target_id, "ti": title, "b": body,
         "dl": deeplink, "ek": event_key},
    )
