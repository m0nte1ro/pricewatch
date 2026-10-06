import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import Config
from app.database import Database
from app.logging import configure
from app.runtime import Runtime
from app.web import discovery, products, settings
from app.web.common import render


def create_app(config: Config | None = None, *, database: Database | None = None) -> FastAPI:
    config = config or Config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = database or Database(config.db_url)
    runtime = Runtime(config, db)

    @asynccontextmanager
    async def lifespan(application):
        configure(config.log_level)
        with db.engine.connect() as connection:
            try:
                connection.execute(text("SELECT version_num FROM alembic_version"))
            except Exception:
                if database is None:
                    raise RuntimeError(
                        "Initialize the database first: python -m app.migrate"
                    ) from None
        runtime.start()
        yield
        await runtime.close()

    application = FastAPI(title="Pricewatch", lifespan=lifespan, docs_url=None, redoc_url=None)
    application.state.runtime = runtime

    @application.middleware("http")
    async def security_context(request: Request, call_next):
        token = request.cookies.get("pricewatch_csrf") or secrets.token_urlsafe(32)
        request.state.csrf = token
        response = await call_next(request)
        if not request.cookies.get("pricewatch_csrf"):
            response.set_cookie(
                "pricewatch_csrf",
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        response = render(request, "error.html", message=exc.detail)
        response.status_code = exc.status_code
        return response

    @application.get("/health")
    async def health():
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    application.include_router(products.router)
    application.include_router(discovery.router)
    application.include_router(settings.router)
    application.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"
    )
    return application


app = create_app()
