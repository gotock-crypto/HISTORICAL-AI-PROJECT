# History Bot V3 — безопасное обновление сервера

## Что обновляет V3

- осмысленный Content Planner вместо случайных архивных тем;
- календарные события, битвы, катастрофы, загадки и исторические переломы;
- scoring темы по значимости, драматургии и актуальности даты;
- строгий набор подтверждённых фактов для генератора текста;
- отдельная проверка качества истории;
- улучшенный scoring изображения;
- проверка разрешения, формата, пропорций, SHA256 и visual hash;
- сохранение совместимости с существующими Telegram, MAX, SQLite и админ-ботом.

## ВАЖНО

Архив V3 является **overlay-обновлением**. Он не содержит:

- `.env`;
- `data/history.db`;
- `runtime/`;
- MAX-сессию;
- виртуальное окружение `venv/`.

Эти данные должны остаться на сервере.

## 1. Загрузить архив

С Windows PowerShell:

```powershell
scp "D:\Project\MAX\history-bot-v3-10of10.tar.gz" USER@89.44.84.51:/tmp/
```

Замените `USER` на реального пользователя SSH. Если вход выполняется от root:

```powershell
scp "D:\Project\MAX\history-bot-v3-10of10.tar.gz" root@89.44.84.51:/tmp/
```

## 2. Подключиться

```powershell
ssh USER@89.44.84.51
```

## 3. Проверить путь сервиса

На текущей версии проекта systemd использует `/opt/history-bot`:

```bash
sudo systemctl cat history-bot
```

Если `WorkingDirectory` другой, используйте его вместо `/opt/history-bot`.

## 4. Остановить сервис

```bash
sudo systemctl stop history-bot
```

## 5. Сделать полный backup

```bash
sudo tar -czf /opt/history-bot-backup-$(date +%F-%H%M%S).tar.gz /opt/history-bot
```

## 6. Распаковать в отдельную временную папку

```bash
rm -rf /tmp/history-v3-update
mkdir -p /tmp/history-v3-update
tar -xzf /tmp/history-bot-v3-10of10.tar.gz -C /tmp/history-v3-update
```

## 7. Наложить обновление БЕЗ удаления runtime и базы

```bash
sudo cp -a /tmp/history-v3-update/history-bot/. /opt/history-bot/
```

`cp -a` добавляет и обновляет файлы, но не удаляет отсутствующие в архиве `.env`, `data/`, `runtime/` и `venv/`.

## 8. Проверить права

Узнайте пользователя сервиса:

```bash
sudo systemctl show history-bot -p User
```

Если сервис работает от отдельного пользователя, при необходимости:

```bash
sudo chown -R USER:USER /opt/history-bot
```

Не меняйте владельца вслепую, если сервис уже работал корректно.

## 9. Активировать окружение и установить зависимости

```bash
cd /opt/history-bot
source venv/bin/activate
pip install -r requirements.txt
```

## 10. Оффлайн-проверка V3

```bash
cd /opt/history-bot
source venv/bin/activate
python self_test.py
```

Ожидаемый результат:

```text
SELF-TEST OK | events=...
```

## 11. Проверка синтаксиса

```bash
python -m py_compile main.py history_bot/*.py history_bot/content/*.py history_bot/max/*.py
```

## 12. Проверка здоровья

```bash
python main.py --health
```

Публичные источники могут временно показать `FAIL`, но config/database должны быть `OK`.

## 13. Создать тестовый черновик

Перед реальной публикацией:

```bash
python main.py --once
```

Проверьте:

- выбрана осмысленная тема;
- текст соответствует событию;
- нет выдуманных деталей;
- изображение действительно релевантно;
- `LOCAL:` содержит скачанный файл, если найдено изображение;
- нет ошибок в traceback.

## 14. Запустить сервис

```bash
sudo systemctl start history-bot
sudo systemctl status history-bot --no-pager
```

## 15. Проверить логи

```bash
journalctl -u history-bot -n 150 --no-pager
```

И в реальном времени:

```bash
journalctl -u history-bot -f
```

## Быстрый rollback

Остановите сервис, распакуйте backup обратно в `/`, затем запустите:

```bash
sudo systemctl stop history-bot
sudo tar -xzf /opt/history-bot-backup-YYYY-MM-DD-HHMMSS.tar.gz -C /
sudo systemctl start history-bot
```

После rollback обязательно проверьте:

```bash
sudo systemctl status history-bot --no-pager
```
