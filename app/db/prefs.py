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
    # Какие сети опрашивать. None — берём список из .env (ENABLED_CHAINS).
    # Список сетей переехал сюда из .env, потому что его меняют по ходу дела:
    # завели позицию в новой сети — включили галочку, а не полезли в файл и
    # перезапуск. Значение из .env осталось значением по умолчанию.
    "enabled_chains": None,
    # Пороги оповещений о позициях. None — берём значение из .env.
    # Переехали сюда по той же причине, что список сетей: их крутят по ходу дела,
    # глядя на свои позиции, а не задают один раз при установке.
    "alert_health_factor": None,
    "alert_out_of_range": None,
    "alert_cooldown": None,
    # Валюта отчётов в разделе личных финансов. Операции хранятся в своих валютах,
    # а сводить их надо в одну, иначе «потрачено за месяц» не складывается.
    "fin_base_currency": "EUR",
    # Заведён ли стартовый список категорий. Флаг нужен, чтобы удалённые категории
    # не появлялись заново при каждом открытии раздела.
    "fin_seeded": False,
    "fin_rules_seeded": False,
    # Не загружать из выписок операции раньше этой даты (ISO, пусто — брать все).
    # Нужно потому, что банки отдают выписку за всю историю счёта: почистив старые
    # годы, человек получил бы их обратно при следующей же загрузке того же файла.
    "fin_import_from": "",
}


def get_prefs(db: Session) -> dict:
    row = db.get(KV, KEY)
    prefs = dict(DEFAULTS)
    if row is not None and isinstance(row.value, dict):
        prefs.update({k: v for k, v in row.value.items() if k in DEFAULTS})
    return prefs


def enabled_chain_keys(db: Session) -> list[str]:
    """Ключи сетей для опроса: из настроек, а если там пусто — из .env.

    Пустой список в настройках не принимаем: он означал бы «не опрашивать ничего»,
    и дашборд молча перестал бы обновляться. Такое состояние должно быть невозможным,
    а не задаваемым.
    """
    from app import config
    chosen = get_prefs(db).get("enabled_chains")
    if isinstance(chosen, list) and chosen:
        return [str(k) for k in chosen]
    return list(config.ENABLED_CHAINS)


def alert_settings(db: Session) -> dict:
    """Пороги оповещений: из настроек, а чего там нет — из .env.

    Приложение читает их на каждом прогоне, поэтому изменённый в интерфейсе порог
    действует со следующего цикла, без перезапуска.
    """
    from app import config
    p = get_prefs(db)
    hf = p.get("alert_health_factor")
    oor = p.get("alert_out_of_range")
    cd = p.get("alert_cooldown")
    return {
        "health_factor": float(hf) if hf is not None else config.ALERT_HEALTH_FACTOR,
        "out_of_range": bool(oor) if oor is not None else config.ALERT_OUT_OF_RANGE,
        "cooldown": int(cd) if cd is not None else config.ALERT_COOLDOWN,
    }


def base_currency(db: Session) -> str:
    """Валюта отчётов личных финансов. Пустое значение недопустимо: без неё нечем
    складывать операции в разных валютах, и весь раздел показывал бы «н/д»."""
    code = (get_prefs(db).get("fin_base_currency") or "").upper()
    return code if len(code) == 3 else "EUR"


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


def parse_amount(raw: str | None) -> float | None:
    """Количество токенов: запятая — ВСЕГДА десятичный разделитель.

    Отдельно от parse_money, и вот почему. Для денег «12,500» — это двенадцать
    тысяч пятьсот, и правило «запятая с тремя цифрами = разделитель тысяч» верное.
    Для количества монет то же правило превращает «0,001» в 1: человек записал
    тысячную долю биткоина, а в базу легла целая монета — ошибка в тысячу раз,
    ровно в том поле, где почти все значения начинаются с нуля и запятой.

    Разделитель тысяч в количестве всё же поддержан, но только когда он однозначен:
    есть и запятая, и точка («1,234.5678») либо пробел («1 234,5»).
    """
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace(" ", "").replace(" ", "").replace("₿", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")           # «1,234.56» — запятая точно про тысячи
    else:
        s = s.replace(",", ".")          # «0,001» и «1,234» — запятая про дробь
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"не похоже на количество: {raw!r}") from None
    if v < 0:
        raise ValueError("количество не может быть отрицательным")
    return v
