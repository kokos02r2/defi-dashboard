"""Доставка алертов в Telegram.

Осознанное ограничение: приложение работает у вас на машине, поэтому пока она спит
или выключена, обновления не идут вообще — и просадку health factor ночью дашборд
не увидит. Это не проблема канала доставки, её решает только вынос на VPS.

Сообщения отправляются пачкой: если за один прогон сработало пять алертов, придёт
одно сообщение, а не пять.
"""

from __future__ import annotations

import html
import json
import logging
import time
import urllib.parse
import urllib.request

from app import config

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3900          # лимит Telegram 4096, оставляем запас на разметку

ICONS = {
    "health": "🔴",
    "liquidated": "💥",
    "out_of_range": "🟡",
    "back_in_range": "🟢",
}


def _call(method: str, params: dict, timeout: int = 20) -> dict:
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "defi-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def send(text: str, retries: int = 2) -> bool:
    """Отправляет сообщение. False — не настроено или не доставлено."""
    if not config.telegram_configured():
        return False
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text[:MAX_LEN],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    for attempt in range(retries + 1):
        try:
            res = _call("sendMessage", payload)
            if res.get("ok"):
                return True
            log.warning("[notify] Telegram отказал: %s", str(res.get("description"))[:160])
            return False        # ошибка самого API — повтор не поможет
        except Exception as e:  # noqa: BLE001 — сетевой сбой, есть смысл повторить
            if attempt >= retries:
                log.warning("[notify] не отправлено: %s", str(e)[:160])
                return False
            time.sleep(1.5 * (attempt + 1))
    return False


def check() -> tuple[bool, str]:
    """Проверка настроек: возвращает (успех, человеческое описание)."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN не задан в .env"
    if not config.TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_CHAT_ID не задан в .env"
    if not config.NOTIFY_ENABLED:
        return False, "уведомления выключены (NOTIFY_ENABLED=false)"
    try:
        res = _call("getMe", {})
    except Exception as e:  # noqa: BLE001
        return False, f"Telegram недоступен: {str(e)[:120]}"
    if not res.get("ok"):
        return False, f"токен отвергнут: {str(res.get('description'))[:120]}"
    return True, f"бот @{res['result'].get('username')} на связи"


def find_chat_ids() -> list[dict]:
    """Читает getUpdates — чтобы не искать свой chat_id вручную.

    Работает, только если вы уже написали боту хоть одно сообщение.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        return []
    try:
        res = _call("getUpdates", {"limit": 20})
    except Exception as e:  # noqa: BLE001
        log.warning("[notify] getUpdates: %s", str(e)[:120])
        return []
    out, seen = [], set()
    for u in res.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        name = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
        out.append({"chat_id": cid, "type": chat.get("type"), "name": name})
    return out


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def format_alerts(alerts: list) -> str:
    """Собирает одно сообщение из нескольких алертов."""
    if len(alerts) == 1:
        a = alerts[0]
        icon = ICONS.get(a.kind, "ℹ️")
        body = f"{icon} <b>{_esc(a.message)}</b>"
        if a.position_id:
            body += f"\n\n{config.PUBLIC_URL}/position/{a.position_id}"
        return body

    lines = [f"<b>Изменений: {len(alerts)}</b>", ""]
    for a in alerts:
        lines.append(f"{ICONS.get(a.kind, 'ℹ️')} {_esc(a.message)}")
    lines += ["", config.PUBLIC_URL]
    return "\n".join(lines)


def format_digest(totals: dict, worst_hf, out_of_range: int) -> str:
    """Ежедневная сводка. Не включена по умолчанию — вызывается только по расписанию."""
    def usd(v):
        return f"${v:,.2f}".replace(",", " ") if v is not None else "н/д"

    lines = [
        "<b>Сводка по портфелю</b>", "",
        f"Чистая стоимость: {usd(totals.get('net'))}",
        f"Несобранные комиссии: {usd(totals.get('fees_unclaimed'))}",
        f"Активных позиций: {totals.get('open_count', 0)}",
    ]
    if worst_hf is not None:
        lines.append(f"Минимальный health factor: {worst_hf:.2f}")
    if out_of_range:
        lines.append(f"Вне диапазона: {out_of_range}")
    lines += ["", config.PUBLIC_URL]
    return "\n".join(lines)
