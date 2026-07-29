"""Планировщик фоновых задач.

Живёт внутри того же процесса, что и веб: для одного пользователя отдельный воркер
и брокер очередей были бы лишней инфраструктурой. max_instances=1 и общий замок в
refresh() гарантируют, что задачи не наложатся друг на друга.
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from tzlocal import get_localzone
from sqlalchemy import select

from app import config
from app.db.base import session_scope
from app.db.models import KV, Position, Wallet
from app.jobs.refresh import refresh, send_digest

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC", job_defaults={
    "coalesce": True,        # проспали несколько срабатываний — выполняем одно
    "max_instances": 1,
    "misfire_grace_time": 300,
})


def _live() -> None:
    try:
        refresh("live")
    except Exception:  # noqa: BLE001 — падение задачи не должно убивать планировщик
        log.exception("[scheduler] live упал")


def _sync() -> None:
    try:
        refresh("sync")
    except Exception:  # noqa: BLE001
        log.exception("[scheduler] sync упал")


def _digest() -> None:
    try:
        send_digest()
    except Exception:  # noqa: BLE001 — сводка не должна ронять планировщик
        log.exception("[scheduler] сводка упала")


def _bootstrap() -> None:
    """Первый прогон при старте: если истории ещё нет — сразу полный sync."""
    try:
        with session_scope() as db:
            has_wallets = db.scalar(select(Wallet).limit(1)) is not None
            never_synced = db.get(KV, "last_sync") is None
            has_positions = db.scalar(select(Position).limit(1)) is not None
        if not has_wallets:
            log.info("[scheduler] кошельков нет — первый прогон пропущен")
            return
        mode = "sync" if (never_synced or not has_positions) else "live"
        log.info("[scheduler] стартовый прогон: %s", mode)
        refresh(mode)
    except Exception:  # noqa: BLE001
        log.exception("[scheduler] стартовый прогон упал")


def start() -> None:
    if not config.SCHEDULER_ENABLED:
        log.info("[scheduler] выключен настройкой SCHEDULER_ENABLED")
        return
    scheduler.add_job(_live, "interval", seconds=config.LIVE_INTERVAL, id="live",
                      replace_existing=True)
    scheduler.add_job(_sync, "interval", seconds=config.SYNC_INTERVAL, id="sync",
                      replace_existing=True)
    if config.DIGEST_ENABLED:
        hour, minute = config.digest_at()
        # timezone=local: время сводки задаётся местным, каким его видит человек,
        # хотя сам планировщик живёт в UTC — с ним не спутать даты переходов
        scheduler.add_job(_digest, "cron", hour=hour, minute=minute, id="digest",
                          replace_existing=True, timezone=get_localzone(),
                          misfire_grace_time=3600)
        log.info("[scheduler] сводка в %02d:%02d по местному времени", hour, minute)
    scheduler.start()
    # стартовый прогон — отдельным потоком, чтобы не задерживать подъём веб-сервера
    threading.Thread(target=_bootstrap, name="bootstrap", daemon=True).start()
    log.info("[scheduler] запущен: live каждые %s c, sync каждые %s c",
             config.LIVE_INTERVAL, config.SYNC_INTERVAL)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def run_async(mode: str, wallet_id: int | None = None) -> None:
    """Ручной запуск из интерфейса — не блокируя HTTP-ответ."""
    threading.Thread(target=lambda: refresh(mode, wallet_id),
                     name=f"manual-{mode}", daemon=True).start()
