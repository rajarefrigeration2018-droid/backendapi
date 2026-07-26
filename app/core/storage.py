# app/core/storage.py
"""
Supabase Storage — file uploads for profile photos, ID documents,
job photos, banners and generated invoices.

The bucket is created once from the Supabase dashboard (or by
`ensure_bucket()` below). Uploads use the service key, so this must never
run client-side.
"""

import logging
import mimetypes
import uuid
from typing import Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

_client = None

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}
ALLOWED_DOC_TYPES = {"application/pdf"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024      # 5 MB
MAX_DOC_BYTES = 10 * 1024 * 1024       # 10 MB

# folder -> what may go in it
FOLDERS = {
    "profiles": ALLOWED_IMAGE_TYPES,
    "documents": ALLOWED_IMAGE_TYPES | ALLOWED_DOC_TYPES,
    "jobs": ALLOWED_IMAGE_TYPES,
    "banners": ALLOWED_IMAGE_TYPES,
    "services": ALLOWED_IMAGE_TYPES,
    "parts": ALLOWED_IMAGE_TYPES,
    "reviews": ALLOWED_IMAGE_TYPES,
    "invoices": ALLOWED_DOC_TYPES,
}


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        logger.error("SUPABASE_URL / SUPABASE_SERVICE_KEY not configured")
        return None
    try:
        from supabase import create_client

        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        return _client
    except Exception as exc:  # noqa: BLE001
        logger.exception("Supabase client init failed: %s", exc)
        return None


def is_configured() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY)


def validate(folder: str, content_type: str, size_bytes: int) -> Tuple[bool, str]:
    allowed = FOLDERS.get(folder)
    if allowed is None:
        return False, "Invalid upload folder"
    if content_type not in allowed:
        return False, "This file type is not allowed"

    limit = MAX_DOC_BYTES if content_type in ALLOWED_DOC_TYPES else MAX_IMAGE_BYTES
    if size_bytes > limit:
        return False, f"File is too large (max {limit // (1024 * 1024)} MB)"
    return True, ""


def upload(
    folder: str,
    data: bytes,
    filename: str,
    content_type: Optional[str] = None,
) -> Optional[str]:
    """Uploads and returns the public URL, or None on failure."""
    client = _get_client()
    if not client:
        return None

    content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "bin").lower()
    key = f"{folder}/{uuid.uuid4().hex}.{ext}"

    try:
        client.storage.from_(settings.SUPABASE_BUCKET).upload(
            key,
            data,
            {"content-type": content_type, "cache-control": "31536000", "upsert": "false"},
        )
        return client.storage.from_(settings.SUPABASE_BUCKET).get_public_url(key)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Upload failed for %s: %s", key, exc)
        return None


def delete(public_url: str) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        marker = f"/{settings.SUPABASE_BUCKET}/"
        if marker not in public_url:
            return False
        key = public_url.split(marker, 1)[1].split("?", 1)[0]
        client.storage.from_(settings.SUPABASE_BUCKET).remove([key])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Delete failed: %s", exc)
        return False


def ensure_bucket() -> bool:
    """Creates the public bucket if it does not exist. Safe to call repeatedly."""
    client = _get_client()
    if not client:
        return False
    try:
        buckets = [b.name for b in client.storage.list_buckets()]
        if settings.SUPABASE_BUCKET not in buckets:
            client.storage.create_bucket(settings.SUPABASE_BUCKET, options={"public": True})
            logger.info("Created storage bucket '%s'", settings.SUPABASE_BUCKET)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_bucket failed: %s", exc)
        return False
