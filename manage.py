#!/usr/bin/env python3
"""Обслуживание из терминала — без веб-интерфейса.

    .venv/bin/python manage.py adduser <логин>
    .venv/bin/python manage.py passwd <логин>
    .venv/bin/python manage.py wallet-add 0x… [название]
    .venv/bin/python manage.py wallet-list
    .venv/bin/python manage.py refresh [live|sync]
    .venv/bin/python manage.py stats
    .venv/bin/python manage.py notify-chatid    # найти свой chat_id
    .venv/bin/python manage.py notify-test      # проверить связь с ботом
    .venv/bin/python manage.py digest           # отправить сводку прямо сейчас
    .venv/bin/python manage.py backfill-prices [--apply]   # досчитать цены событий
    .venv/bin/python manage.py fin-recompute [--all]       # пересчёт личных операций
"""

from __future__ import annotations

import getpass
import sys

from sqlalchemy import func, select

from app.auth import hash_password
from app.db.base import init_db, session_scope
from app.db.models import Position, PositionEvent, PriceCache, Snapshot, User, Wallet


def _ask_password() -> str:
    p1 = getpass.getpass("Пароль: ")
    p2 = getpass.getpass("Ещё раз: ")
    if p1 != p2:
        sys.exit("пароли не совпадают")
    if len(p1) < 8:
        sys.exit("пароль короче 8 символов")
    return p1


def adduser(argv: list[str]) -> None:
    if not argv:
        sys.exit("укажите логин")
    name = argv[0]
    with session_scope() as db:
        if db.scalar(select(User).where(User.username == name)):
            sys.exit(f"пользователь {name!r} уже есть")
        db.add(User(username=name, password_hash=hash_password(_ask_password())))
    print(f"создан пользователь {name!r}")


def passwd(argv: list[str]) -> None:
    if not argv:
        sys.exit("укажите логин")
    name = argv[0]
    with session_scope() as db:
        u = db.scalar(select(User).where(User.username == name))
        if u is None:
            sys.exit(f"нет пользователя {name!r}")
        u.password_hash = hash_password(_ask_password())
    print("пароль изменён")


def wallet_add(argv: list[str]) -> None:
    if not argv:
        sys.exit("укажите адрес или ENS")
    from app.jobs.refresh import add_wallet
    w = add_wallet(argv[0], argv[1] if len(argv) > 1 else "")
    print(f"добавлен {w.address}" + (f" ({w.label})" if w.label else ""))


def wallet_list(_argv: list[str]) -> None:
    with session_scope() as db:
        rows = db.scalars(select(Wallet).order_by(Wallet.id)).all()
        if not rows:
            print("кошельков нет")
            return
        for w in rows:
            n = db.scalar(select(func.count(Position.id)).where(Position.wallet_id == w.id))
            print(f"  [{w.id}] {w.address}  {w.label or '':<20} позиций: {n}  "
                  f"{'активен' if w.enabled else 'выключен'}")


def do_refresh(argv: list[str]) -> None:
    from app.jobs.refresh import refresh
    mode = argv[0] if argv else "sync"
    print(f"запускаю {mode}…")
    st = refresh(mode)
    print(f"позиций: {st.get('positions')}, кошельков: {st.get('wallets')}, "
          f"время: {st.get('elapsed')} c")
    for e in st.get("errors", []):
        print("  !", e)


def notify_test(_argv: list[str]) -> None:
    from app.core import notify
    ok, msg = notify.check()
    print(f"  настройки: {'✓' if ok else '✕'} {msg}")
    if not ok:
        return
    if notify.send("✅ <b>DeFi Dashboard</b>\nПроверка связи — уведомления настроены."):
        print("  тестовое сообщение отправлено")
    else:
        print("  отправить не удалось, смотрите TELEGRAM_CHAT_ID")


def digest(_argv: list[str]) -> None:
    """Отправляет ежедневную сводку немедленно.

    Нужна, чтобы проверить и вид сообщения, и сами цифры, не дожидаясь утра.
    """
    from app.core.notify import format_digest
    from app.jobs.refresh import digest_payload, send_digest
    with session_scope() as db:
        data = digest_payload(db)
    print("  ── как будет выглядеть ──")
    for line in format_digest(data).splitlines():
        print("  " + line)
    print("  ─────────────────────────")
    print("  отправлено" if send_digest() else "  не отправлено (см. notify-test)")


def backfill_prices(argv: list[str]) -> None:
    """Досчитывает долларовую оценку событий, у которых её не хватило.

    Зачем нужна отдельная команда, а не обычная синхронизация: у DefiLlama история
    по адресу токена на конкретной сети начинается не с рождения токена, и часть
    старых сборов осталась без цены. Ответ «цены нет» кэшируется навсегда — иначе по
    мусорному токену мы ходили бы в API на каждом круге, — поэтому повторный sync
    такие события даже не переспрашивает. Здесь мы точечно стираем эти отметки и
    пробуем запасной ключ CoinGecko, у которого история есть за любую дату.

    Без --apply только считает и показывает, ничего не записывая.
    """
    apply = "--apply" in argv
    from sqlalchemy import delete, or_
    from app.core.chains import CHAINS
    from app.core.prices import PriceService, coin_key, fallback_coin
    from app.db.models import PriceCache

    with session_scope() as db:
        events = db.execute(
            select(PositionEvent, Position).join(Position)
            .where(PositionEvent.kind == "collect",
                   PositionEvent.fee_usd_at_time.is_(None),
                   PositionEvent.timestamp.isnot(None),
                   or_(PositionEvent.fee0 != "0", PositionEvent.fee1 != "0"))).all()
        if not events:
            print("  Нечего досчитывать: у всех событий с комиссиями цена есть.")
            return
        print(f"  событий без цены: {len(events)}")

        # какие (монета, час) понадобятся
        need: set[tuple[str, int]] = set()
        plan = []
        for e, pos in events:
            chain = CHAINS.get(pos.chain)
            d = pos.detail or {}
            legs = []
            for raw, tok in ((e.fee0, d.get("token0") or {}), (e.fee1, d.get("token1") or {})):
                amount = int(raw or 0)
                if not amount or not tok.get("address") or chain is None:
                    continue
                coin = coin_key(chain, tok["address"])
                legs.append((coin, amount, int(tok.get("decimals") or 18)))
                need.add((coin, e.timestamp))
            if legs:
                plan.append((e, pos, legs))

        coins = sorted({c for c, _ in need})
        print(f"  нужно котировок: {len(need)} по {len(coins)} монетам")
        for c in coins:
            alt = fallback_coin(c)
            print(f"    {c[:52]:52} запасной ключ: {alt or 'НЕТ'}")

        if not apply:
            print("\n  Это холостой прогон. Повторите с --apply, чтобы записать.")
            return

        # стираем только отметки «цены нет» и только по нужным парам: положительные
        # значения не трогаем, они верные
        killed = 0
        for coin, ts in need:
            killed += db.execute(delete(PriceCache).where(
                PriceCache.coin == coin, PriceCache.hour == ts // 3600,
                PriceCache.price.is_(None))).rowcount or 0
        db.commit()
        print(f"  стёрто отметок «цены нет»: {killed}")

        prices = PriceService(db)
        prices.prefetch(need)

        filled = 0
        touched: dict[int, Position] = {}
        for e, pos, legs in plan:
            total, ok = 0.0, True
            for coin, amount, dec in legs:
                px = prices.at(coin, e.timestamp)
                if px is None:
                    ok = False
                    break
                total += amount / (10 ** dec) * float(px)
            if ok and total:
                e.fee_usd_at_time = total
                filled += 1
                touched[pos.id] = pos
        db.commit()
        print(f"  досчитано событий: {filled}")

        # заклеймленные комиссии позиции — это сумма оценок её сборов на их момент
        for pos in touched.values():
            total = db.scalar(select(func.coalesce(func.sum(PositionEvent.fee_usd_at_time), 0.0))
                              .where(PositionEvent.position_id == pos.id,
                                     PositionEvent.kind == "collect"))
            was = pos.fees_claimed_usd
            pos.fees_claimed_usd = float(total or 0.0)
            # PnL и годовые пересчитает провайдер: снимаем отметку полноты истории,
            # и ближайшая синхронизация разберёт позицию заново уже с ценами в кэше
            pos.history_complete = False
            print(f"    {pos.chain:9} {pos.title[:26]:26} комиссии "
                  f"{was if was is None else round(was, 2)} -> {round(pos.fees_claimed_usd, 2)}")
        db.commit()
        print(f"  позиций обновлено: {len(touched)}")
        print("\n  Осталось: запустите полную синхронизацию — она пересчитает PnL и годовые")
        print("  у этих позиций (кнопка «Полная синхронизация» или manage.py refresh sync).")


def notify_chatid(_argv: list[str]) -> None:
    """Показывает chat_id из последних сообщений боту — искать вручную не надо."""
    from app.core import notify
    if not notify.find_chat_ids():
        print("  Ничего не найдено. Напишите боту любое сообщение в Telegram\n"
              "  и запустите команду снова.")
        return
    for c in notify.find_chat_ids():
        print(f"  chat_id={c['chat_id']}  ({c['type']}) {c['name']}")
    print("\n  Впишите нужный в .env как TELEGRAM_CHAT_ID и перезапустите дашборд.")


def stats(_argv: list[str]) -> None:
    from app.db.models import FinAccount, FinTx
    with session_scope() as db:
        def n(model, *where):
            return db.scalar(select(func.count()).select_from(model).where(*where)) or 0
        print(f"  кошельков      : {n(Wallet)}")
        print(f"  позиций        : {n(Position)} (активных {n(Position, Position.is_open.is_(True))})")
        print(f"  событий        : {n(PositionEvent)}")
        print(f"  снапшотов      : {n(Snapshot)}")
        print(f"  цен в кэше     : {n(PriceCache)}")
        if n(FinTx):
            print(f"  личных операций: {n(FinTx)} на {n(FinAccount)} счетах "
                  f"(без курса {n(FinTx, FinTx.amount_base.is_(None))})")


def fin_recompute(argv: list[str]) -> None:
    """Досчитывает пересчёт личных операций в валюту отчётов.

    Нужно, если в момент записи не было связи с ЦБ: такие операции не входят в итоги,
    и это видно в интерфейсе. С --all пересчитываются вообще все — например, после
    ручной правки базы.
    """
    from app.core.finance import recompute_base
    from app.db.prefs import base_currency
    only_missing = "--all" not in argv
    with session_scope() as db:
        print(f"  валюта отчётов: {base_currency(db)}")
        done, failed = recompute_base(db, only_missing=only_missing)
        print(f"  пересчитано: {done}")
        if failed:
            print(f"  не удалось получить курс: {failed} — попробуйте позже")


COMMANDS = {"adduser": adduser, "passwd": passwd, "wallet-add": wallet_add,
            "wallet-list": wallet_list, "refresh": do_refresh, "stats": stats,
            "notify-test": notify_test, "notify-chatid": notify_chatid,
            "digest": digest, "backfill-prices": backfill_prices,
            "fin-recompute": fin_recompute}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    init_db()
    COMMANDS[sys.argv[1]](sys.argv[2:])
