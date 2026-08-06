"""Курсы валют на конкретную дату — для пересчёта личных операций в одну валюту.

Отличие от core/market.py: там курс «сейчас» для тикера в шапке, живёт полчаса и
никуда не пишется. Здесь нужен курс на дату операции в прошлом, и он должен быть
неизменным: если пересчитывать историю по сегодняшнему курсу, расходы за прошлый
год будут меняться каждый день, и сравнивать месяцы станет невозможно.

Источник — официальные курсы ЦБ РФ, у него есть архив по датам и не нужен ключ.
ЦБ публикует всё в рублях за единицу валюты, поэтому рубль здесь опорная величина:
курс любой пары получается делением двух строк одного дня — это и есть официальный
кросс-курс.

Кэш бессрочный. Курс за прошедший день не изменится никогда, так что каждая дата
запрашивается ровно один раз за всю жизнь базы.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FxRate

log = logging.getLogger(__name__)

# Валюты, которые предлагаются в интерфейсе. Список ЦБ гораздо шире, но выбор из
# сорока строк в поле «валюта счёта» только мешает: тут те, в которых реально
# бывают карты и наличные у человека между Россией и Европой.
CURRENCIES = ("EUR", "USD", "RUB", "GBP", "CHF", "TRY", "KZT", "GEL", "RSD", "AED")

SYMBOLS = {"EUR": "€", "USD": "$", "RUB": "₽", "GBP": "£", "CHF": "Fr",
           "TRY": "₺", "KZT": "₸", "GEL": "₾", "RSD": "din", "AED": "dh"}

ARCHIVE_URL = "https://www.cbr-xml-daily.ru/archive/{y:04d}/{m:02d}/{d:02d}/daily_json.js"
LATEST_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

# За выходные и праздники курс не публикуется — отступаем назад до рабочего дня.
# Десяти дней хватает на новогодние каникулы, самый длинный перерыв в году.
MAX_BACKOFF_DAYS = 10


class FxUnavailable(Exception):
    """Курс получить не удалось: нет сети или ЦБ не отдал дату."""


def _fetch_day(day: date) -> dict[str, float] | None:
    """Все курсы ЦБ за один день: код валюты -> рублей за единицу. None — нет данных."""
    url = ARCHIVE_URL.format(y=day.year, m=day.month, d=day.day)
    req = urllib.request.Request(url, headers={"User-Agent": "defi-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None          # выходной или праздник — нормальный случай
        raise FxUnavailable(f"ЦБ ответил {e.code}") from None
    except Exception as e:  # noqa: BLE001 — сеть, таймаут, битый JSON
        raise FxUnavailable(str(e)[:120]) from None

    out: dict[str, float] = {"RUB": 1.0}
    for code, v in ((data or {}).get("Valute") or {}).items():
        try:
            # Nominal делим честно: часть валют ЦБ публикует за 10 или 100 единиц,
            # и для тенге разница в сто раз
            out[code] = float(v["Value"]) / float(v["Nominal"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return out or None


def _store(db: Session, day: date, as_of: date, rates: dict[str, float]) -> None:
    have = {r.code for r in db.scalars(select(FxRate).where(FxRate.day == day))}
    for code, rub in rates.items():
        if code in have:
            continue
        db.add(FxRate(day=day, code=code, rub=rub, as_of=as_of))
    db.commit()


def _load(db: Session, day: date, code: str) -> float | None:
    return db.scalar(select(FxRate.rub).where(FxRate.day == day, FxRate.code == code))


def rub_per(db: Session, code: str, day: date) -> float:
    """Сколько рублей за единицу валюты `code` в день `day`.

    Курс за выходной сохраняется под запрошенной датой со ссылкой на реальный день
    публикации в as_of — так дата операции всегда находит курс с первого запроса.
    Исключение: сегодняшний день. ЦБ публикует курс к середине дня, и до публикации
    мы получили бы вчерашний; записать его навсегда как «курс на сегодня» нельзя —
    к вечеру он уже неверен, а исправить будет некому.
    """
    code = (code or "").upper()
    if code == "RUB":
        return 1.0

    cached = _load(db, day, code)
    if cached is not None:
        return cached

    today = date.today()
    probe = min(day, today)
    for back in range(MAX_BACKOFF_DAYS + 1):
        d = probe - timedelta(days=back)
        rates = _fetch_day(d)
        if not rates:
            continue
        if day < today:
            _store(db, day, d, rates)
        rub = rates.get(code)
        if rub is None:
            raise FxUnavailable(f"ЦБ не публикует курс {code}")
        return rub
    raise FxUnavailable(f"нет курса на {day.isoformat()}")


def convert(db: Session, amount: float, frm: str, to: str,
            day: date) -> tuple[float, float]:
    """Сумму из одной валюты в другую по курсу на дату. Возвращает (сумма, курс).

    Курс — сколько единиц `to` за одну единицу `frm`; он хранится вместе с операцией,
    чтобы потом было видно, по чему считали.
    """
    frm, to = (frm or "").upper(), (to or "").upper()
    if not amount:
        return 0.0, 1.0
    if frm == to:
        return float(amount), 1.0
    rate = rub_per(db, frm, day) / rub_per(db, to, day)
    return float(amount) * rate, rate


def symbol(code: str) -> str:
    return SYMBOLS.get((code or "").upper(), (code or "").upper())
