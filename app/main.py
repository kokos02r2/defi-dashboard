"""Точка входа FastAPI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.auth import ensure_admin
from app.db.base import init_db, session_scope
from app.jobs import scheduler
from app.web.routes import router
from app.web.routes_fin import router as fin_router
from app.web.templating import TEMPLATES_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with session_scope() as db:
        created, msg = ensure_admin(db, config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
    if msg:
        log.warning("%s", msg) if not created else log.info("%s", msg)
    scheduler.start()
    log.info("дашборд поднят на http://%s:%s", config.HOST, config.PORT)
    yield
    scheduler.stop()


app = FastAPI(title="DeFi Dashboard", docs_url="/api/docs", redoc_url=None,
              lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=config.SESSION_HTTPS_ONLY,
)

app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR.parent / "static")), name="static")
app.include_router(router)
# Личные финансы — отдельное пространство под /fin. Общего с крипто-частью
# у него только вход и вёрстка, поэтому и роутер свой.
app.include_router(fin_router)

# require_user кидает HTTPException(307, Location=/login) — стандартный обработчик
# FastAPI отдаёт его вместе с заголовком, и браузер уходит на форму входа сам.
