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
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

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

# Курс сразу за диапазон дат, одним запросом на валюту. Ради этого и заведён второй
# источник: выписка за пять лет — это около двух тысяч дат, и по запросу на каждую
# загрузка идёт больше получаса. Здесь те же официальные курсы приходят за секунду.
DYNAMIC_URL = ("https://www.cbr.ru/scripts/XML_dynamic.asp"
               "?date_req1={a}&date_req2={b}&VAL_NM_RQ={cbr_id}")
# Внутренние коды валют у ЦБ. Списка «по буквенному коду» у динамики нет, поэтому
# соответствие приходится держать у себя; валюты те же, что предлагает интерфейс.
CBR_IDS = {"USD": "R01235", "EUR": "R01239", "GBP": "R01035", "CHF": "R01775",
           "TRY": "R01700J", "KZT": "R01335", "GEL": "R01210", "AED": "R01230"}

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


_RECORD = re.compile(
    r'<Record\s+Date="([^"]+)"[^>]*>.*?<Nominal>([^<]+)</Nominal>.*?<Value>([^<]+)</Value>',
    re.S)


def _fetch_range(code: str, start: date, end: date) -> list[tuple[date, float]]:
    """Курсы одной валюты за диапазон дат. Отдаются только рабочие дни."""
    url = DYNAMIC_URL.format(a=start.strftime("%d/%m/%Y"), b=end.strftime("%d/%m/%Y"),
                             cbr_id=CBR_IDS[code])
    req = urllib.request.Request(url, headers={"User-Agent": "defi-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("cp1251", errors="replace")
    except Exception as e:  # noqa: BLE001 — сеть, таймаут, смена формата
        raise FxUnavailable(str(e)[:120]) from None
    out: list[tuple[date, float]] = []
    for d, nominal, value in _RECORD.findall(text):
        try:
            day = datetime.strptime(d, "%d.%m.%Y").date()
            rub = float(value.replace(",", ".")) / float(nominal.replace(",", "."))
        except (ValueError, ZeroDivisionError):
            continue
        out.append((day, rub))
    return out


def prefetch(db: Session, codes, start: date, end: date) -> int:
    """Заранее кладёт в кэш курсы всех нужных валют за весь период выписки.

    Вызывается перед разбором файла: иначе каждая новая дата — отдельный поход в сеть,
    и загрузка выписки за несколько лет висит десятки минут, держа базу занятой.
    Молча ничего не делает, если сеть недоступна: это ускорение, а не обязательный шаг,
    и без него всё продолжает работать по-старому, просто медленно.
    """
    if start > end:
        start, end = end, start
    end = min(end, date.today())
    loaded = 0
    for code in sorted({(c or "").upper() for c in codes}):
        if code == "RUB" or code not in CBR_IDS:
            continue
        have = {d for (d,) in db.execute(
            select(FxRate.day).where(FxRate.code == code,
                                     FxRate.day >= start, FxRate.day <= end))}
        # Рабочих дней в периоде примерно пять седьмых. Если в кэше уже больше двух
        # третей — за диапазоном ходить незачем, всё нужное лежит.
        if len(have) >= (end - start).days * 0.68:
            continue
        try:
            rows = _fetch_range(code, start, end)
        except FxUnavailable as e:
            log.warning("[fx] диапазон %s %s—%s не получен: %s", code, start, end, e)
            continue
        for day, rub in rows:
            if day in have or not (start <= day <= end):
                continue
            db.add(FxRate(day=day, code=code, rub=rub, as_of=day))
            have.add(day)
            loaded += 1
        db.commit()
        log.info("[fx] %s: курсов за %s—%s загружено %d", code, start, end, loaded)
    return loaded


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

    # Выходного в кэше нет — но рабочий день перед ним обычно уже загружен диапазоном.
    # Сначала ищем его у себя и только потом идём в сеть: иначе выписка за пять лет
    # снова превратилась бы в сотни запросов ради одних суббот и воскресений.
    for back in range(1, MAX_BACKOFF_DAYS + 1):
        near = day - timedelta(days=back)
        rub = _load(db, near, code)
        if rub is not None:
            if day < date.today():
                db.add(FxRate(day=day, code=code, rub=rub, as_of=near))
                db.commit()
            return rub

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
