# History Bot V4 — безопасное обновление на сервере

## Что исправлено в V4

- детерминированный Content Planner: рейтинг больше не меняется при повторном пересчёте;
- безопасный SSL по умолчанию (`REQUESTS_VERIFY_SSL=1`);
- усиленная проверка качества текста: fallback тоже проходит validation;
- повышен минимальный Story Quality Score до 75;
- добавлены анти-пустые редакторские правила для AI;
- добавлен regression self-test для ranking и SSL;
- удалены из релизного архива `__pycache__` и `.pyc`.

## 1. Загрузить архив с Windows

В PowerShell:

```powershell
cd D:\Project\MAX
scp ".\history-bot-v4-10of10.tar.gz" root@89.44.84.51:/tmp/
```

Если используется не `root`, замените пользователя:

```powershell
scp ".\history-bot-v4-10of10.tar.gz" USER@89.44.84.51:/tmp/
```

## 2. Подключиться

```powershell
ssh root@89.44.84.51
```

## 3. Сначала определить фактический путь проекта

```bash
sudo systemctl cat history-bot
```

Если `WorkingDirectory=/opt/history-bot`, используйте команды ниже. Если путь другой — замените `/opt/history-bot` в командах.

## 4. Остановить сервис и сделать backup

```bash
sudo systemctl stop history-bot
sudo tar -C /opt -czf /opt/history-bot-backup-$(date +%F-%H%M%S).tar.gz history-bot
```

Проверка:

```bash
ls -lh /opt/history-bot-backup-*.tar.gz | tail
```

## 5. Проверить архив

```bash
sha256sum /tmp/history-bot-v4-10of10.tar.gz
```

Сравните с SHA256, указанным в сообщении с архивом.

## 6. Распаковать во временную папку

```bash
sudo rm -rf /tmp/history-v4-update
sudo mkdir -p /tmp/history-v4-update
sudo tar -xzf /tmp/history-bot-v4-10of10.tar.gz -C /tmp/history-v4-update
```

## 7. Наложить код, не удаляя `.env`, БД и runtime

```bash
sudo cp -a /tmp/history-v4-update/history-bot/. /opt/history-bot/
```

## 8. Обновить зависимости в существующем venv

```bash
cd /opt/history-bot
source venv/bin/activate
pip install -r requirements.txt
```

## 9. Обязательные проверки до запуска

```bash
cd /opt/history-bot
source venv/bin/activate
python self_test.py
python -m py_compile main.py history_bot/*.py history_bot/content/*.py history_bot/max/*.py
python main.py --health
```

Затем вручную создать черновик:

```bash
python main.py --once
```

`--once` резервирует тему и медиа в БД, поэтому используйте его именно как реальную проверку будущего поста, а не запускайте много раз подряд без необходимости.

## 10. Запустить сервис

```bash
sudo systemctl start history-bot
sudo systemctl status history-bot --no-pager
```

Логи:

```bash
journalctl -u history-bot -n 150 --no-pager
```

В реальном времени:

```bash
journalctl -u history-bot -f
```

## Откат

Остановить сервис, распаковать backup обратно в `/opt`, затем запустить:

```bash
sudo systemctl stop history-bot
sudo rm -rf /opt/history-bot
sudo tar -C /opt -xzf /opt/history-bot-backup-YYYY-MM-DD-HHMMSS.tar.gz
sudo systemctl start history-bot
```
