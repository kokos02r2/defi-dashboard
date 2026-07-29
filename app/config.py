"""Настройки приложения. Всё чувствительное — только через .env, ничего в коде."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "dashboard.sqlite3"
DATABASE_URL = f"sqlite+pysqlite:///{DB_PATH}"

# Ключ подписи сессионной куки. Если не задан — генерируем и пишем в файл,
# иначе после каждого рестарта пришлось бы логиниться заново.
_secret_file = DATA_DIR / "secret_key"


def _secret_key() -> str:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    if _secret_file.exists():
        return _secret_file.read_text().strip()
    key = secrets.token_urlsafe(48)
    _secret_file.write_text(key)
    _secret_file.chmod(0o600)
    return key


SECRET_KEY = _secret_key()

# Первичные учётные данные: применяются только при создании пустой БД.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

SESSION_MAX_AGE = _int("SESSION_MAX_AGE", 60 * 60 * 24 * 30)
# Только для localhost. При выносе наружу обязательно поднять до True вместе с HTTPS.
SESSION_HTTPS_ONLY = _bool("SESSION_HTTPS_ONLY", False)

HOST = os.environ.get("HOST", "127.0.0.1")
# не 8000: этот порт часто занят Docker и другими локальными сервисами
PORT = _int("PORT", 8787)

# Периоды фоновых задач (секунды)
LIVE_INTERVAL = _int("LIVE_INTERVAL", 60)              # состояние активных позиций
SYNC_INTERVAL = _int("SYNC_INTERVAL", 60 * 60 * 24)    # поиск новых позиций и событий
SNAPSHOT_INTERVAL = _int("SNAPSHOT_INTERVAL", 60 * 15)  # точка на графике капитала

SCHEDULER_ENABLED = _bool("SCHEDULER_ENABLED", True)

# Сети, которые опрашиваем. Пустая строка = все известные.
ENABLED_CHAINS = [c.strip() for c in os.environ.get(
    "ENABLED_CHAINS", "ethereum,arbitrum,base,polygon,optimism,bsc").split(",") if c.strip()]

# Приоритетный RPC. Либо один на все сети (RPC_URL), либо на конкретную (RPC_ETHEREUM и т.п.)
RPC_URL = os.environ.get("RPC_URL") or None

# Бюджет времени на сканирование логов одной сети, секунд
HISTORY_BUDGET = _int("HISTORY_BUDGET", 240)

# Пороги алертов
ALERT_HEALTH_FACTOR = float(os.environ.get("ALERT_HEALTH_FACTOR", "1.15"))
ALERT_OUT_OF_RANGE = _bool("ALERT_OUT_OF_RANGE", True)

# Защита от «дребезга»: у волатильной пары цена может ходить туда-сюда через
# границу диапазона, и без паузы это превратится в поток сообщений.
ALERT_COOLDOWN = _int("ALERT_COOLDOWN", 30 * 60)

# --- уведомления в Telegram ---------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_ENABLED = _bool("NOTIFY_ENABLED", True)

# Ежедневная сводка. Время местное — то, которое показывают часы на этой машине:
# «сводка в 9 утра» человек понимает буквально, а не как 9 по UTC.
DIGEST_ENABLED = _bool("DIGEST_ENABLED", True)
DIGEST_TIME = os.environ.get("DIGEST_TIME", "09:00").strip()


def digest_at() -> tuple[int, int]:
    """Час и минута сводки. Мусор в настройке не должен ронять планировщик."""
    try:
        hh, _, mm = DIGEST_TIME.partition(":")
        hour, minute = int(hh), int(mm or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except ValueError:
        pass
    return 9, 0

# Адрес, по которому дашборд открывается у вас — уходит ссылкой в сообщения
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://{HOST}:{PORT}").rstrip("/")


def telegram_configured() -> bool:
    return bool(NOTIFY_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
