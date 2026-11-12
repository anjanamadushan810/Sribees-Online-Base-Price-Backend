import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api import admin, auth, catalog, matching, orders, supplier_auth
from app.core.config import get_settings
from app.core.db import get_engine
from app.services.errors import SupplyError

log = logging.getLogger("supply")

_DB_RULE_ERRORS = {
    "23514": 422,  # check_violation
    "23001": 409,  # restrict_violation - audit log touched
    "23505": 409,  # unique_violation
    "23502": 422,  # not_null_violation
    "22003": 422,  # numeric_value_out_of_range
}


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Sribees Supply",
        version="0.1.0",
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(SupplyError)
    async def supply_error(_: Request, exc: SupplyError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(DBAPIError)
    async def database_rule(_: Request, exc: DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        status = _DB_RULE_ERRORS.get(sqlstate or "")
        if status is None:
            log.exception("database error")
            return JSONResponse(status_code=500, content={"detail": "internal error"})
        return JSONResponse(status_code=status, content={"detail": "the change breaks a rule"})

    @app.get("/health", include_in_schema=False)
    async def health():
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(supplier_auth.router)
    app.include_router(catalog.router)
    app.include_router(matching.router)
    app.include_router(orders.router)
    return app


app = create_app()
