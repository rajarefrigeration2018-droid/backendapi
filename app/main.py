# app/main.py
"""
Mistrio API — entry point.

Run locally:   uvicorn app.main:app --reload
Railway start: uvicorn app.main:app --host 0.0.0.0 --port $PORT
Docs:          /docs
"""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.database import db
from app.routers import admin as admin_router
from app.routers import admin_extra as admin_extra_router
from app.routers import auth as auth_router
from app.routers import booking as booking_router
from app.routers import catalog as catalog_router
from app.routers import partner as partner_router
from app.routers import config as config_router

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("mistrio")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Backend for Mistrio — User App, Partner App and Admin Panel.",
    docs_url="/docs",
    redoc_url=None,
)

# ------------------------------------------------------------------
# CORS
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,          # must stay False while origins include "*"
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Request timing + logging
# ------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.time()
    response = await call_next(request)
    ms = (time.time() - started) * 1000
    response.headers["X-Response-Time"] = f"{ms:.0f}ms"
    if ms > 1000:
        logger.warning("SLOW %s %s -> %sms", request.method, request.url.path, int(ms))
    return response


# ------------------------------------------------------------------
# Error handlers — everything returns the same envelope
# ------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "success" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": str(detail),
            "data": None,
            "error_code": f"HTTP_{exc.status_code}",
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "input"
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "message": f"{field}: {first.get('msg', 'invalid value')}",
            "data": None,
            "error_code": "VALIDATION_ERROR",
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Something went wrong. Please try again.",
            "data": None,
            "error_code": "INTERNAL_ERROR",
        },
    )


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------
@app.get("/", tags=["Health"])
def root():
    return {
        "success": True,
        "message": f"{settings.APP_NAME} is running",
        "data": {"version": settings.APP_VERSION, "env": settings.ENVIRONMENT},
        "error_code": None,
    }


@app.get("/health", tags=["Health"])
def health():
    db_ok = db.ping()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "success": db_ok,
            "message": "Healthy" if db_ok else "Database unreachable",
            "data": {"database": db_ok, "version": settings.APP_VERSION},
            "error_code": None if db_ok else "DB_DOWN",
        },
    )


# ------------------------------------------------------------------
# Routers  (more get added in the next batches)
# ------------------------------------------------------------------
API = "/api"
app.include_router(config_router.router, prefix=API)
app.include_router(auth_router.router, prefix=API)
app.include_router(catalog_router.router, prefix=API)
app.include_router(booking_router.router, prefix=API)
app.include_router(partner_router.router, prefix=API)
app.include_router(admin_router.router, prefix=API)
app.include_router(admin_extra_router.router, prefix=API)


@app.on_event("startup")
def on_startup():
    logger.info("Starting %s v%s (%s)", settings.APP_NAME, settings.APP_VERSION, settings.ENVIRONMENT)
    if db.ping():
        logger.info("Database connected")
    else:
        logger.error("DATABASE CONNECTION FAILED — check DATABASE_URL")
