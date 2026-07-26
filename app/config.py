# app/config.py
"""
Mistrio Backend — Central configuration.
Every value comes from environment variables. Nothing is hardcoded.
"""

import os
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ---------- App ----------
    APP_NAME: str = "Mistrio API"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "production"          # development | production
    DEBUG: bool = False

    # ---------- Database ----------
    # Supabase → Project Settings → Database → Connection string → URI
    # Use the "Connection pooling" (port 6543) URI on Railway.
    DATABASE_URL: str

    # ---------- Supabase (storage + admin ops) ----------
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_BUCKET: str = "mistrio"

    # ---------- JWT ----------
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30      # 30 days for apps
    ADMIN_TOKEN_EXPIRE_MINUTES: int = 60 * 12            # 12 hours for admin

    # ---------- OTP ----------
    OTP_LENGTH: int = 6
    OTP_EXPIRE_SECONDS: int = 300
    OTP_MAX_ATTEMPTS: int = 5
    OTP_PROVIDER: str = "mock"                # mock | msg91 | firebase
    MSG91_AUTH_KEY: str = ""
    MSG91_TEMPLATE_ID: str = ""
    MSG91_SENDER_ID: str = "MSTRIO"
    # In development, this OTP always works. Keep empty in production.
    OTP_MASTER_CODE: str = ""

    # ---------- Razorpay ----------
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # ---------- Firebase Cloud Messaging ----------
    FCM_SERVICE_ACCOUNT_JSON: str = ""        # full JSON as a single-line string
    FCM_PROJECT_ID: str = ""

    # ---------- Google Maps ----------
    GOOGLE_MAPS_API_KEY: str = ""

    # ---------- CORS ----------
    # Comma separated. Use "*" only while developing.
    CORS_ORIGINS: str = "*"

    # ---------- Misc ----------
    TIMEZONE: str = "Asia/Kolkata"

    @property
    def cors_origin_list(self) -> List[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
