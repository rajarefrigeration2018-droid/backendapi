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

    # ---------- Auth provider ----------
    # firebase = Firebase Phone Auth (client-side OTP, backend verifies ID token)
    # otp      = backend-sent OTP (fallback / testing only)
    AUTH_PROVIDER: str = "firebase"

    # ---------- Firebase ----------
    # Firebase Console > Project Settings > Service Accounts > Generate new private key
    # Paste the ENTIRE JSON as one line into this variable.
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""
    FIREBASE_PROJECT_ID: str = ""

    # ---------- OTP (fallback only) ----------
    OTP_LENGTH: int = 6
    OTP_EXPIRE_SECONDS: int = 300
    OTP_MAX_ATTEMPTS: int = 5
    OTP_PROVIDER: str = "mock"                # mock only (Firebase handles real OTP)
    # In development, this OTP always works. Keep empty in production.
    OTP_MASTER_CODE: str = ""

    # ---------- Razorpay ----------
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

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
    def use_firebase_auth(self) -> bool:
        return self.AUTH_PROVIDER.lower() == "firebase"

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
