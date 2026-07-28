#!/usr/bin/env bash
# Обновление на сервере. Лежит в /opt/dashboard/deploy/deploy.sh и запускается
# из GitHub Actions по ssh. Ничего не спрашивает и не трогает данные.
set -euo pipefail

APP_DIR=/opt/dashboard
BRANCH=main

cd "$APP_DIR"

echo "==> забираю изменения"
git fetch --prune origin
# reset --hard, а не merge: на сервере правок быть не должно, и деплой не должен
# упираться в конфликт из-за случайно изменённого файла
git reset --hard "origin/$BRANCH"

echo "==> зависимости"
.venv/bin/pip install --quiet --upgrade -r requirements.txt

# Схема БД доводится сама при старте приложения (init_db создаёт новые таблицы и
# дописывает новые колонки), отдельного шага миграции не требуется.

echo "==> перезапуск"
sudo /usr/bin/systemctl restart dashboard

# ждём, пока поднимется, и проверяем — иначе «успешный» деплой мог бы оставить
# сервис лежать, а мы бы об этом не узнали
for i in $(seq 1 30); do
    if curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:8787/login; then
        echo "==> готово, версия $(git rev-parse --short HEAD)"
        exit 0
    fi
    sleep 1
done

echo "!! приложение не ответило за 30 секунд" >&2
sudo /usr/bin/systemctl status dashboard --no-pager --lines 30 >&2 || true
exit 1
