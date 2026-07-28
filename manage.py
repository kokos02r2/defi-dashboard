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
    with session_scope() as db:
        def n(model, *where):
            return db.scalar(select(func.count()).select_from(model).where(*where)) or 0
        print(f"  кошельков      : {n(Wallet)}")
        print(f"  позиций        : {n(Position)} (активных {n(Position, Position.is_open.is_(True))})")
        print(f"  событий        : {n(PositionEvent)}")
        print(f"  снапшотов      : {n(Snapshot)}")
        print(f"  цен в кэше     : {n(PriceCache)}")


COMMANDS = {"adduser": adduser, "passwd": passwd, "wallet-add": wallet_add,
            "wallet-list": wallet_list, "refresh": do_refresh, "stats": stats,
            "notify-test": notify_test, "notify-chatid": notify_chatid}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    init_db()
    COMMANDS[sys.argv[1]](sys.argv[2:])
