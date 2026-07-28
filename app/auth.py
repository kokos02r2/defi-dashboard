"""Авторизация: один пользователь, пароль, подписанная сессионная кука.

Дашборд рассчитан на localhost, но пароль всё равно хранится хэшем (argon2), а не
в открытом виде, и попытки входа ограничены по частоте. Это стоит десяти строк и
снимает вопрос «а что если вынесу наружу».

Приложение принципиально read-only по отношению к блокчейну: оно знает только
публичные адреса кошельков. Приватных ключей и seed-фраз здесь нет и быть не должно.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import get_session
from app.db.models import User, utcnow

pwd = CryptContext(schemes=["argon2"], deprecated="auto")

MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 300
_attempts: dict[str, list[float]] = defaultdict(list)


def hash_password(raw: str) -> str:
    return pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd.verify(raw, hashed)
    except Exception:  # noqa: BLE001 — битый хэш не должен ронять вход
        return False


def throttled(ip: str) -> int:
    """Сколько секунд ждать до следующей попытки. 0 — можно пробовать."""
    now = time.time()
    tries = [t for t in _attempts[ip] if now - t < LOCKOUT_SECONDS]
    _attempts[ip] = tries
    if len(tries) < MAX_ATTEMPTS:
        return 0
    return int(LOCKOUT_SECONDS - (now - tries[0])) + 1


def note_failure(ip: str) -> None:
    _attempts[ip].append(time.time())


def reset_attempts(ip: str) -> None:
    _attempts.pop(ip, None)


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        # прогоняем хэш вхолостую: иначе по времени ответа видно, существует ли логин
        pwd.hash(password)
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login = utcnow()
    db.commit()
    return user


def current_user(request: Request, db: Session = Depends(get_session)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get(User, uid)


def require_user(request: Request, db: Session = Depends(get_session)) -> User:
    """Зависимость для страниц: не залогинен — редирект на /login."""
    user = current_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            detail="login required",
            headers={"Location": "/login"},
        )
    return user


def require_user_api(request: Request, db: Session = Depends(get_session)) -> User:
    """То же для JSON-эндпоинтов: 401 вместо редиректа."""
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="не авторизован")
    return user


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


def ensure_admin(db: Session, username: str, password: str) -> tuple[bool, str]:
    """Создаёт первого пользователя, если их ещё нет. Возвращает (создан, сообщение)."""
    if db.scalar(select(User).limit(1)) is not None:
        return False, ""
    if not password:
        return False, ("Пользователей нет, а ADMIN_PASSWORD не задан. "
                       "Пропишите его в .env и перезапустите.")
    db.add(User(username=username, password_hash=hash_password(password)))
    db.commit()
    return True, f"Создан пользователь {username!r}."
