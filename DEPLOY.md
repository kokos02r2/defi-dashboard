# Выкладка на сервер

Схема самая простая из работающих: **systemd + nginx + certbot**, без Docker.
Приложение однопроцессное с SQLite внутри — контейнеры и оркестрация тут ничего не
упрощают, только добавляют слоёв.

Репозиторий: `https://github.com/kokos02r2/defi-dashboard.git`
Всё делается один раз, дальше выкладка идёт сама по пушу в `main`.

---

## 0. Что понадобится

- аккаунт Hetzner Cloud;
- аккаунт на [duckdns.org](https://www.duckdns.org) (вход через GitHub);
- ssh-ключ на вашем маке (`~/.ssh/id_ed25519.pub`; если нет — `ssh-keygen -t ed25519`).

---

## 1. Сервер в Hetzner

Hetzner Cloud → **Add Server**:

| Параметр | Значение |
|---|---|
| Location | Nuremberg или Helsinki |
| Image | **Ubuntu 24.04** |
| Type | **CX22** (2 vCPU, 4 ГБ) — с большим запасом |
| SSH keys | добавьте свой публичный ключ |
| Firewall | создайте правило: входящие TCP **22, 80, 443** |

> Берите именно 24.04. На 25.04+ стоит Python 3.13, где выпилен модуль `crypt`,
> и `passlib` — библиотека хеширования пароля — на нём падает.

Запишите IP сервера. Дальше всё делается на нём:

```bash
ssh root@IP_СЕРВЕРА
```

---

## 2. Домен в DuckDNS

1. Зайдите на duckdns.org, придумайте поддомен, например `kokos-defi`.
2. В поле **current ip** впишите IP сервера, нажмите **update ip**.
3. Скопируйте свой **token** — пригодится, если IP когда-нибудь сменится.

Проверьте с мака, что имя резолвится в нужный адрес:

```bash
dig +short kokos-defi.duckdns.org
```

Пока не отдаёт ваш IP — дальше идти нет смысла, сертификат не выпустится.

---

## 3. Базовая настройка сервера

Под `root`:

```bash
apt update && apt -y upgrade
apt -y install python3-venv python3-pip git nginx certbot python3-certbot-nginx ufw curl

# отдельный пользователь без прав root: приложение ходит в интернет,
# и ему незачем работать от администратора
adduser --system --group --home /opt/dashboard --shell /bin/bash dashboard

ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
```

---

## 4. Код на сервер

```bash
sudo -u dashboard -H bash
cd /opt/dashboard
git clone https://github.com/kokos02r2/defi-dashboard.git .
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

> Если репозиторий приватный, `git clone` попросит пароль. Тогда сделайте
> **deploy key**: на сервере `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""`,
> содержимое `~/.ssh/id_ed25519.pub` добавьте в GitHub → репозиторий → Settings →
> Deploy keys (без права записи), и клонируйте по ssh:
> `git clone git@github.com:kokos02r2/defi-dashboard.git .`

Настройки:

```bash
cp .env.example .env
nano .env
```

Обязательно поправьте:

```ini
ADMIN_PASSWORD=длинный-пароль-которого-нигде-больше-нет
SESSION_HTTPS_ONLY=true
PUBLIC_URL=https://kokos-defi.duckdns.org
HOST=127.0.0.1
PORT=8787
```

`SESSION_HTTPS_ONLY=true` обязателен: без него кука сессии уйдёт по открытому HTTP.
`HOST=127.0.0.1` тоже — приложение слушает только локально, наружу его выставляет
nginx, и порт 8787 в интернете не виден.

Выйдите обратно в root: `exit`

---

## 5. systemd

```bash
cp /opt/dashboard/deploy/dashboard.service /etc/systemd/system/dashboard.service
systemctl daemon-reload
systemctl enable --now dashboard
systemctl status dashboard --no-pager
```

Должно быть `active (running)`. Проверка изнутри сервера:

```bash
curl -I http://127.0.0.1:8787/login     # ожидаем 200
```

Логи, если что-то не так: `journalctl -u dashboard -f`

Разрешите пользователю `dashboard` перезапускать только свой сервис — это нужно
для автодеплоя, и это единственное право root, которое ему выдаётся:

```bash
echo 'dashboard ALL=(root) NOPASSWD: /usr/bin/systemctl restart dashboard, /usr/bin/systemctl status dashboard' \
  > /etc/sudoers.d/dashboard
chmod 440 /etc/sudoers.d/dashboard
visudo -c
```

---

## 6. nginx и сертификат

```bash
cp /opt/dashboard/deploy/nginx.conf /etc/nginx/sites-available/dashboard
sed -i 's/ВАШ-ПОДДОМЕН/kokos-defi/' /etc/nginx/sites-available/dashboard   # своё имя
ln -sf /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/dashboard
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

Сертификат Let's Encrypt — одной командой, certbot сам допишет блок 443 и редирект
с HTTP:

```bash
certbot --nginx -d kokos-defi.duckdns.org --agree-tos -m ВАША@почта --redirect
```

Автопродление уже настроено таймером systemd, проверить:

```bash
systemctl list-timers | grep certbot
certbot renew --dry-run
```

Откройте `https://kokos-defi.duckdns.org` — должна быть форма входа.

---

## 7. Перенести базу с мака

Иначе на сервере начнётся пустая история: кошельки, партии, настройки и график
капитала останутся дома.

**На маке**, остановив локальное приложение (Ctrl+C):

```bash
cd /Users/kokos/Dev/dashboard_defi
# .backup, а не cp: копия получается согласованной даже при активном WAL
.venv/bin/python -c "
import sqlite3
s=sqlite3.connect('data/dashboard.sqlite3'); d=sqlite3.connect('/tmp/dashboard.sqlite3')
s.backup(d); d.close(); s.close()"
scp /tmp/dashboard.sqlite3 root@IP_СЕРВЕРА:/tmp/
```

**На сервере**:

```bash
systemctl stop dashboard
install -o dashboard -g dashboard -m 600 /tmp/dashboard.sqlite3 /opt/dashboard/data/dashboard.sqlite3
rm -f /opt/dashboard/data/dashboard.sqlite3-wal /opt/dashboard/data/dashboard.sqlite3-shm
systemctl start dashboard
```

Пароль входа останется тот, что был на маке, — `ADMIN_PASSWORD` из `.env`
применяется только при создании пустой базы. Сменить: `manage.py passwd admin`
(см. раздел «Обслуживание»).

---

## 8. Автовыкладка из GitHub

**На сервере** создайте ключ для GitHub Actions:

```bash
sudo -u dashboard ssh-keygen -t ed25519 -f /opt/dashboard/.ssh/deploy -N "" -C "github-actions"
cat /opt/dashboard/.ssh/deploy.pub >> /opt/dashboard/.ssh/authorized_keys
chown -R dashboard:dashboard /opt/dashboard/.ssh
chmod 700 /opt/dashboard/.ssh && chmod 600 /opt/dashboard/.ssh/authorized_keys
cat /opt/dashboard/.ssh/deploy          # приватный ключ — скопировать целиком
ssh-keyscan -H IP_СЕРВЕРА 2>/dev/null   # отпечаток сервера
```

**В GitHub**: репозиторий → Settings → Secrets and variables → Actions → New secret:

| Имя | Значение |
|---|---|
| `SSH_KEY` | приватный ключ целиком, вместе со строками `-----BEGIN...` и `-----END...` |
| `SSH_HOST` | IP сервера |
| `SSH_USER` | `dashboard` |
| `SSH_HOST_KEY` | вывод `ssh-keyscan` (необязательно, но лучше задать) |

Готово. Теперь `git push` в `main` выкладывает изменения сам: workflow заходит по
ssh, делает `git reset --hard origin/main`, доставляет зависимости, перезапускает
сервис и **проверяет, что приложение ответило**. Не ответило за 30 секунд — деплой
падает с красным крестом и куском лога, а не молча оставляет сервис лежать.

Запустить вручную: вкладка **Actions** → workflow `deploy` → **Run workflow**.

---

## 9. Обслуживание

Все команды — от пользователя `dashboard` из `/opt/dashboard`:

```bash
sudo -u dashboard -H bash
cd /opt/dashboard

.venv/bin/python manage.py stats            # что в базе
.venv/bin/python manage.py passwd admin     # сменить пароль
.venv/bin/python manage.py wallet-list
.venv/bin/python manage.py refresh sync     # разовая полная синхронизация
.venv/bin/python manage.py notify-test      # проверить Telegram
```

Логи и сервис:

```bash
journalctl -u dashboard -f
journalctl -u dashboard --since "1 hour ago"
systemctl restart dashboard
```

**Резервная копия базы.** В ней вся история, партии и настройки — на блокчейне их нет.
Раз в сутки, кладём на сервер:

```bash
mkdir -p /opt/dashboard/backups
cat > /etc/cron.daily/dashboard-backup <<'EOF'
#!/bin/sh
D=/opt/dashboard
su -s /bin/sh dashboard -c "$D/.venv/bin/python -c \"
import sqlite3, datetime
n = datetime.datetime.now().strftime('%Y%m%d')
s = sqlite3.connect('$D/data/dashboard.sqlite3')
d = sqlite3.connect('$D/backups/dashboard-%s.sqlite3' % n)
s.backup(d); d.close(); s.close()\""
find $D/backups -name 'dashboard-*.sqlite3' -mtime +14 -delete
EOF
chmod +x /etc/cron.daily/dashboard-backup
/etc/cron.daily/dashboard-backup && ls -la /opt/dashboard/backups
```

Копии стоит иногда забирать к себе: `scp root@IP:/opt/dashboard/backups/*.sqlite3 .`

---

## Про безопасность

Дашборд **только читает** блокчейн: он знает публичные адреса кошельков, ничего не
подписывает и не отправляет транзакций. Приватных ключей на сервере нет и быть не
должно. Худшее, что даёт взлом входа, — чужой человек видит ваши позиции.

Что уже сделано в приложении и в этой инструкции:

- пароль хранится хешем argon2, не открытым текстом;
- вход ограничен: 8 попыток за 5 минут с одного IP (реальный IP приложение видит
  благодаря `--proxy-headers` и `X-Forwarded-For` из nginx);
- кука сессии подписана и при `SESSION_HTTPS_ONLY=true` не уходит по HTTP;
- приложение слушает только `127.0.0.1`, наружу смотрит один nginx;
- сервис работает от пользователя без прав root, которому разрешён ровно один
  `systemctl restart` своего сервиса;
- firewall пропускает только 22, 80 и 443.

Что стоит добавить, если захотите строже:

- **Двухфакторная авторизация (TOTP).** Сейчас её нет — скажите, добавлю.
- **Доступ только со своих адресов.** Если у вас статический IP, самое дешёвое
  усиление: в блоке `location /` файла nginx дописать `allow ВАШ.IP; deny all;`
- **fail2ban** на ssh: `apt install fail2ban` — дальше работает из коробки.
- Отключить вход по паролю в ssh, оставив только ключи (в `/etc/ssh/sshd_config`:
  `PasswordAuthentication no`).
