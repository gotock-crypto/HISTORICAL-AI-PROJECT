# Обновление существующего `/opt/history-bot`

> Цель — заменить код pipeline целиком, сохранив production state и рабочие publisher/admin infrastructure.

## 1. Backup перед обновлением

На сервере:

```bash
cd /opt
cp -a history-bot history-bot.backup-$(date +%Y%m%d-%H%M%S)
cp -a history-bot/data/history.db history-bot.backup-db-$(date +%Y%m%d-%H%M%S).sqlite
cp -a history-bot/.env history-bot/.env.backup-$(date +%Y%m%d-%H%M%S)
cp -a history-bot/runtime/max_session history-bot/runtime/max_session.backup-$(date +%Y%m%d-%H%M%S)
```

Если MAX session является симлинком/нестандартным путём — сохранить фактический каталог отдельно.

## 2. Остановить сервис

```bash
systemctl stop history-bot.service
```

## 3. Распаковать новый архив во временный каталог

Не распаковывать сразу поверх production.

```bash
mkdir -p /opt/history-bot-new
# загрузить архив в /opt/history-bot-new/
cd /opt/history-bot-new
```

## 4. Перенести код

Сохранить из production без замены:

```text
/opt/history-bot/.env
/opt/history-bot/data/
/opt/history-bot/runtime/max_session/
```

Также не менять unit и override:

```text
/etc/systemd/system/history-bot.service
/etc/systemd/system/history-bot.service.d/override.conf
```

Новый код должен содержать те же:

```text
main.py
history_bot/admin.py
history_bot/publishers.py
history_bot/max/publisher.py
```

по рабочей версии, но `.env` и runtime state остаются production.

## 5. Зависимости

```bash
/opt/history-bot/venv/bin/pip install -r /opt/history-bot-new/requirements.txt
```

## 6. Syntax / self-test до запуска

```bash
/opt/history-bot/venv/bin/python -m compileall /opt/history-bot-new
/opt/history-bot/venv/bin/python /opt/history-bot-new/self_test.py
```

## 7. Замена кода

После успешных локальных проверок:

```bash
# сохранить production .env/data/runtime отдельно
rsync -a --delete \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='runtime/max_session/' \
  /opt/history-bot-new/ /opt/history-bot/
```

Если `runtime/media/` существует, его содержимое можно удалить: новая версия не использует его как постоянное хранилище изображений.

## 8. База данных

База не пересоздаётся.

При первом запуске `DB` добавит недостающие таблицы/колонки:

```text
daily_batches
daily_events
daily_media
posts.daily_event_id
posts.topic_key
posts.topic
```

Существующие:

```text
posts
publishes
topics
media_memory
destinations
```

остаются.

## 9. Запуск

```bash
systemctl start history-bot.service
systemctl status history-bot.service
```

Логи:

```bash
journalctl -u history-bot.service -n 200 --no-pager
journalctl -u history-bot.service -f
```

## 10. Что должно появиться в логах

При старте:

```text
DAILY BATCH EXISTS ...
```

или после startup recovery:

```text
DAILY BATCH READY ...
```

Должны быть видны запросы `onthisday:en` и, при необходимости, `onthisday:ru`, затем `daily:commons:...`.

В течение дня повторный поиск исторических событий выполняться не должен.

## 11. Реальный контроль

Проверить через админ-бот:

1. Статус.
2. Подготовку дневной очереди.
3. Создание ручного поста.
4. Предпросмотр.
5. Публикацию.
6. Повторный запуск публикации после искусственной ошибки одной платформы.

Проверить SQLite:

```bash
sqlite3 /opt/history-bot/data/history.db \
  'select month_day,status,count(*) from daily_events group by month_day,status;'
```

После полного успеха Telegram + MAX должно быть:

```text
status = posted
```

## 12. Важное поведение

- событие не считается использованным при одном только добавлении в дневную очередь;
- `topics` запоминает тему после успешной полной публикации;
- одна плохая фотография не удаляет событие;
- все плохие фотографии → событие `skipped`, берётся следующее;
- Telegram и MAX обрабатываются независимо;
- partial publication не создаёт второй новый event в том же автозапуске;
- при restart незавершённый post остаётся в `posts` и может быть доведён до завершения;
- постоянного каталога изображений нет.
