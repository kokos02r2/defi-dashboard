"""Пользовательские настройки, которые задаются из интерфейса и живут в БД.

Отличие от app/config.py: там — параметры запуска из .env (порт, ключи, расписание),
их меняют файлом и перезапуском. Здесь — то, что пользователь правит на ходу.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import KV, utcnow

KEY = "user_prefs"

DEFAULTS: dict[str, Any] = {
    # Сколько своих денег заведено в DeFi суммарно, в долларах.
    # None — не задано, сравнение на дашборде не показывается.
    "initial_deposit_usd": None,
    "initial_note": "",
}


def get_prefs(db: Session) -> dict:
    row = db.get(KV, KEY)
    prefs = dict(DEFAULTS)
    if row is not None and isinstance(row.value, dict):
        prefs.update({k: v for k, v in row.value.items() if k in DEFAULTS})
    return prefs


def save_prefs(db: Session, **changes) -> dict:
    prefs = get_prefs(db)
    prefs.update({k: v for k, v in changes.items() if k in DEFAULTS})
    row = db.get(KV, KEY)
    if row is None:
        db.add(KV(key=KEY, value=prefs))
    else:
        row.value = prefs
        row.updated_at = utcnow()
    db.commit()
    return prefs


def parse_money(raw: str | None) -> float | None:
    """Принимает «12 500», «12500.50», «12,500» и «$12 500» — люди пишут по-разному."""
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace(" ", "").replace(" ", "")
    if not s:
        return None
    # запятая как десятичный разделитель, если после неё не три цифры
    if "," in s and "." not in s:
        head, _, tail = s.rpartition(",")
        s = f"{head}.{tail}" if len(tail) != 3 else head + tail
    else:
        s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"не похоже на сумму: {raw!r}") from None
    if v < 0:
        raise ValueError("сумма не может быть отрицательной")
    return v
