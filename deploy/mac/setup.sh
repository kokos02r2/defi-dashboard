#!/bin/bash
# Выкладка дашборда на macOS. Замена systemd+nginx+certbot из DEPLOY.md:
# здесь launchd + Caddy, всё остальное — та же схема.
#
#   sudo ./deploy/mac/setup.sh ПОДДОМЕН TOKEN [ПОЧТА]
#
# Наружу выведен порт 8443, а не 443: роутер Vodafone держит внешние 80 и 443
# под свой веб-интерфейс и не даёт их пробросить. Сертификат поэтому выпускается
# DNS-проверкой через API DuckDNS — входящие порты для этого не нужны совсем.
#
# Скрипт идемпотентен: гоняйте сколько нужно, повторный запуск просто
# перезаписывает конфиги и перезапускает сервисы. Данные не трогает.
set -euo pipefail

SUB="${1:-}"
TOKEN="${2:-}"
EMAIL="${3:-}"

if [[ -z "$SUB" || -z "$TOKEN" ]]; then
	echo "Использование: sudo $0 ПОДДОМЕН TOKEN [ПОЧТА]" >&2
	echo "  ПОДДОМЕН — имя из duckdns.org без .duckdns.org" >&2
	echo "  TOKEN    — token со страницы duckdns.org" >&2
	exit 2
fi
if [[ $EUID -ne 0 ]]; then
	echo "Нужен root: sudo $0 ..." >&2
	exit 2
fi
if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
	echo "Запускайте через sudo от своей учётной записи, не из-под root-сессии." >&2
	exit 2
fi

APP_USER="$SUDO_USER"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TPL="$APP_DIR/deploy/mac"
LOGS="$APP_DIR/data/logs"
DOMAIN="$SUB.duckdns.org"
PUBLIC_PORT=8443
BREW_PREFIX="$(sudo -u "$APP_USER" brew --prefix 2>/dev/null || echo /opt/homebrew)"
# нужен caddy с модулем dns.providers.duckdns: без него DNS-проверку не сделать,
# а в сборке из Homebrew этого модуля нет
CADDY="$BREW_PREFIX/bin/caddy-duckdns"
[[ -n "$EMAIL" ]] || EMAIL="admin@$DOMAIN"

say() { printf '\n==> %s\n' "$*"; }
as_user() { sudo -u "$APP_USER" "$@"; }

say "каталог приложения: $APP_DIR"
say "адрес: https://$DOMAIN:$PUBLIC_PORT"

# --- проверки до того, как что-то менять -------------------------------------
[[ -x "$APP_DIR/.venv/bin/uvicorn" ]] || {
	echo "!! нет .venv. Сначала:" >&2
	echo "   python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
	exit 1
}
[[ -f "$APP_DIR/.env" ]] || { echo "!! нет .env" >&2; exit 1; }

if [[ ! -x "$CADDY" ]]; then
	say "скачиваю caddy с модулем DuckDNS"
	ARCH=arm64; [[ "$(uname -m)" == x86_64 ]] && ARCH=amd64
	as_user curl -fSL --max-time 300 -o "$CADDY" \
		"https://caddyserver.com/api/download?os=darwin&arch=$ARCH&p=github.com%2Fcaddy-dns%2Fduckdns"
	chmod 755 "$CADDY"
	xattr -d com.apple.quarantine "$CADDY" 2>/dev/null || true
fi
"$CADDY" list-modules 2>/dev/null | grep -q dns.providers.duckdns || {
	echo "!! в $CADDY нет модуля dns.providers.duckdns" >&2
	exit 1
}

as_user mkdir -p "$LOGS" "$APP_DIR/backups"

# --- 1. .env под внешний доступ ----------------------------------------------
# Кука сессии без SESSION_HTTPS_ONLY=true уйдёт по открытому HTTP; HOST=127.0.0.1
# обязателен, чтобы порт 8787 не смотрел в локальную сеть напрямую, минуя Caddy.
say "правлю .env"
as_user "$APP_DIR/.venv/bin/python" - "$APP_DIR/.env" "https://$DOMAIN:$PUBLIC_PORT" <<'PY'
import re, sys
path, public = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
want = {
    "SESSION_HTTPS_ONLY": "true",
    "HOST": "127.0.0.1",
    "PORT": "8787",
    "PUBLIC_URL": public,
}
for key, value in want.items():
    pattern = re.compile(rf"^{key}=.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(f"{key}={value}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    print(f"    {key}={value}")
open(path, "w", encoding="utf-8").write(text)
PY
chmod 600 "$APP_DIR/.env"

# --- 2. обновлятор DuckDNS ----------------------------------------------------
say "ставлю обновлятор DuckDNS (каждые 5 минут)"
# домашнее имя обслуживаем только если оно вообще заведено в DuckDNS
LAN_DOMAIN="$SUB-lan.duckdns.org"
LAN_SUB=""
[[ -n "$(dig +short "$LAN_DOMAIN" 2>/dev/null)" ]] && LAN_SUB="$SUB-lan"
sed -e "s|ВАШ-ПОДДОМЕН|$SUB|" \
    -e "s|ДОМАШНИЙ-ПОДДОМЕН|$LAN_SUB|" \
    -e "s|ВАШ-TOKEN|$TOKEN|" \
    -e "s|ЛОГ-ФАЙЛ|$LOGS/duckdns.log|" \
    "$TPL/duckdns.sh.template" >"$TPL/duckdns.sh"
chown "$APP_USER" "$TPL/duckdns.sh"
chmod 700 "$TPL/duckdns.sh"   # внутри token, чужим читать нечего

# первый прогон сразу и синхронно: без записи в DNS сертификат не выпустится
say "проверяю DuckDNS"
as_user "$TPL/duckdns.sh" && echo "    запись обновлена" || {
	echo "!! duckdns не принял обновление — проверьте поддомен и token" >&2
	exit 1
}

# --- 3. конфиг Caddy ----------------------------------------------------------
say "собираю конфиг Caddy"
# Из дома внешнее имя не открывается: роутер не разворачивает запрос к своему
# внешнему адресу изнутри сети. Поэтому если в DuckDNS заведён поддомен
# ПОДДОМЕН-lan, указывающий на локальный адрес мака, обслуживаем и его — тогда
# дома работает он, а снаружи основное имя. Не заведён — молча обходимся одним.
ADDRS="https://$DOMAIN:$PUBLIC_PORT"
if [[ -n "$LAN_SUB" ]]; then
	ADDRS="$ADDRS, https://$LAN_DOMAIN:$PUBLIC_PORT"
	echo "    домашнее имя: $LAN_DOMAIN"
fi
sed -e "s|АДРЕСА|$ADDRS|" -e "s|ВАШ-ПОДДОМЕН|$SUB|" \
	-e "s|ВАША-ПОЧТА|$EMAIL|" -e "s|ВАШ-TOKEN|$TOKEN|" \
	"$TPL/Caddyfile.template" >"$BREW_PREFIX/etc/Caddyfile"
chown "$APP_USER" "$BREW_PREFIX/etc/Caddyfile"
chmod 600 "$BREW_PREFIX/etc/Caddyfile"   # внутри token DuckDNS
as_user "$CADDY" validate --config "$BREW_PREFIX/etc/Caddyfile" >/dev/null 2>&1 || {
	echo "!! Caddyfile не прошёл проверку" >&2
	as_user "$CADDY" validate --config "$BREW_PREFIX/etc/Caddyfile" >&2 || true
	exit 1
}

# --- 4. службы launchd --------------------------------------------------------
# LaunchDaemon, а не LaunchAgent: демон стартует при загрузке машины, до и без
# входа в систему. Агент ждал бы логина, и после перезагрузки дашборд лежал бы
# до того, как кто-то сядет за мак.
#
# Оба процесса работают от обычного пользователя, не от root: портов ниже 1024
# мы не занимаем (8443 и 8787), а сертификаты Caddy лежат в профиле этого же
# пользователя — там, где он их уже получил.
plist() {  # plist ИМЯ РАСПИСАНИЕ-XML ЛОГ ПРОГРАММА...
	local label="$1" schedule="$2" log="$3"; shift 3
	local args=""
	for a in "$@"; do args+="		<string>$a</string>"$'\n'; done
	cat >"/Library/LaunchDaemons/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$label</string>
	<key>UserName</key><string>$APP_USER</string>
	<key>WorkingDirectory</key><string>$APP_DIR</string>
	<key>ProgramArguments</key>
	<array>
$args	</array>
$schedule
	<key>EnvironmentVariables</key>
	<dict>
		<key>PATH</key><string>$BREW_PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
		<key>HOME</key><string>/Users/$APP_USER</string>
		<key>LANG</key><string>ru_RU.UTF-8</string>
		<key>PYTHONUNBUFFERED</key><string>1</string>
	</dict>
	<key>StandardOutPath</key><string>$log</string>
	<key>StandardErrorPath</key><string>$log</string>
	<key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLIST
	chmod 644 "/Library/LaunchDaemons/$label.plist"
}

reload() {  # снимаем и грузим заново — так подхватывается изменённый плист
	local label="$1"
	launchctl bootout "system/$label" 2>/dev/null || true
	launchctl bootstrap system "/Library/LaunchDaemons/$label.plist"
}

# руками запущенные копии заняли бы порты, и службы не поднялись бы
pkill -f 'caddy-duckdns run' 2>/dev/null || true
pkill -f 'uvicorn app.main:app' 2>/dev/null || true
sleep 1

# приложение. Один процесс, без --workers: планировщик живёт внутри процесса,
# два воркера = два планировщика, дубли уведомлений и гонки записи в SQLite.
say "ставлю сервис приложения"
plist com.kokos.defi-dashboard \
	'	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ThrottleInterval</key><integer>10</integer>' \
	"$LOGS/dashboard.log" \
	"$APP_DIR/.venv/bin/uvicorn" app.main:app \
	--host 127.0.0.1 --port 8787 \
	--proxy-headers --forwarded-allow-ips 127.0.0.1
reload com.kokos.defi-dashboard

say "ставлю Caddy (HTTPS на $PUBLIC_PORT)"
plist com.kokos.caddy \
	'	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ThrottleInterval</key><integer>10</integer>' \
	"$LOGS/caddy.log" \
	"$CADDY" run --config "$BREW_PREFIX/etc/Caddyfile"
reload com.kokos.caddy

say "ставлю обновлятор DuckDNS"
plist com.kokos.duckdns \
	'	<key>RunAtLoad</key><true/>
	<key>StartInterval</key><integer>300</integer>' \
	"$LOGS/duckdns-launchd.log" \
	"$TPL/duckdns.sh"
reload com.kokos.duckdns

say "ставлю ежедневный бэкап базы (04:20)"
sed -e "s|КАТАЛОГ-ПРИЛОЖЕНИЯ|$APP_DIR|" "$TPL/backup.sh.template" >"$TPL/backup.sh"
chown "$APP_USER" "$TPL/backup.sh"
chmod 755 "$TPL/backup.sh"
plist com.kokos.defi-dashboard-backup \
	'	<key>StartCalendarInterval</key>
	<dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>20</integer></dict>' \
	"$LOGS/backup.log" \
	"$TPL/backup.sh"
reload com.kokos.defi-dashboard-backup

# встроенный файрвол macOS иначе может тихо отбрасывать входящие к caddy
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
if [[ -x $FW ]] && $FW --getglobalstate | grep -q enabled; then
	$FW --add "$CADDY" >/dev/null || true
	$FW --unblockapp "$CADDY" >/dev/null || true
	echo "    caddy разрешён в файрволе macOS"
fi

# --- 5. чтобы мак работал как сервер -----------------------------------------
# sleep 0        — не засыпать, иначе дашборд пропадает из сети
# autorestart 1  — сам включиться после отключения электричества
# womp 1         — просыпаться от сетевой активности
say "настраиваю питание (не спать, включаться после сбоя питания)"
pmset -a sleep 0 disksleep 0 autorestart 1 womp 1 >/dev/null 2>&1 || true

# --- 6. проверки --------------------------------------------------------------
say "проверяю приложение"
ok=""
for _ in $(seq 1 30); do
	if curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:8787/login; then ok=1; break; fi
	sleep 1
done
if [[ -z $ok ]]; then
	echo "!! приложение не ответило за 30 секунд. Лог:" >&2
	tail -30 "$LOGS/dashboard.log" >&2 || true
	exit 1
fi
echo "    127.0.0.1:8787/login — 200"

say "проверяю HTTPS"
ok=""
for _ in $(seq 1 24); do
	# --resolve: проверяем свой же Caddy, не выходя в интернет и не упираясь
	# в то, что роутер не разворачивает запрос к внешнему адресу изнутри сети
	if curl -fsS -o /dev/null --max-time 5 \
		--resolve "$DOMAIN:$PUBLIC_PORT:127.0.0.1" \
		"https://$DOMAIN:$PUBLIC_PORT/login"; then ok=1; break; fi
	sleep 5
done
if [[ -n $ok ]]; then
	echo "    https://$DOMAIN:$PUBLIC_PORT/login — 200, сертификат принят без оговорок"
else
	echo "!! HTTPS не поднялся за 2 минуты. Лог:" >&2
	tail -30 "$LOGS/caddy.log" >&2 || true
	exit 1
fi

# --- 7. что осталось руками ---------------------------------------------------
IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
LAN_IP="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
GW="$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}')"

cat <<FINAL

==> установлено

  приложение   com.kokos.defi-dashboard        launchctl print system/com.kokos.defi-dashboard
  HTTPS        com.kokos.caddy                 $LOGS/caddy.log
  DuckDNS      com.kokos.duckdns               $LOGS/duckdns.log
  бэкап        com.kokos.defi-dashboard-backup 04:20, $APP_DIR/backups

ОСТАЛОСЬ СДЕЛАТЬ РУКАМИ — на роутере ($GW), Internet -> Port Mapping:

  Service TCP, LAN IP $LAN_IP, Type Port,
  Public Port 8443, LAN Port 8443

  Порты 80 и 443 роутер под себя не отдаёт, поэтому и 8443.
  Адрес дашборда: https://$DOMAIN:$PUBLIC_PORT

Открывать с телефона по мобильному интернету, а не из домашнего Wi-Fi: многие
роутеры не разворачивают запрос к своему же внешнему адресу изнутри сети,
и «из дома не открывается» ещё ничего не значит.

FINAL
