# History Daily Bot — Minerals-style pipeline

Цель версии: один раз в сутки собрать очередь реальных исторических событий на текущую календарную дату, заранее подобрать несколько URL изображений, а в течение дня только последовательно публиковать подготовленную очередь.

## Архитектура

```text
00:00 Europe/Amsterdam
  -> Wikimedia On This Day (structured feed)
  -> фильтрация и ranking
  -> global dedup по SQLite
  -> 12–15+ daily_events
  -> metadata-only image discovery
  -> 3–4 URL на событие
  -> daily queue READY

каждые 3 часа / ручной запуск
  -> next READY event
  -> candidate #1..N
  -> временная загрузка
  -> MIME/PIL/relevance/dedup validation
  -> лучший кандидат
  -> GigaChat пишет Telegram/MAX post
  -> Telegram и MAX независимо
  -> только после успешной публикации всех настроенных платформ: POSTED
  -> временный файл удаляется
```

### Важное отличие от старых версий

GigaChat **не ищет темы**. Он получает уже выбранное событие из `daily_events` и используется только для написания поста.

Изображения постоянно на сервере не хранятся: в `daily_media` находятся только URL и metadata. Файл появляется во временном каталоге непосредственно перед публикацией и удаляется после использования.

## База

Существующие таблицы `posts`, `publishes`, `topics`, `media_memory` сохраняются. Добавлены:

- `daily_batches` — один batch на календарный день;
- `daily_events` — события очереди и их состояния;
- `daily_media` — URL/metadata кандидатов изображений.

В существующую `posts` добавлены совместимые поля `daily_event_id`, `topic_key`, `topic`.

## Статусы события

- `ready` — ждёт публикации;
- `processing` — зарезервировано для создания draft;
- `publishing` — есть draft, идёт/ожидается публикация платформ;
- `posted` — Telegram + MAX успешно завершены;
- `skipped` — все подготовленные изображения события недоступны.

## Установка / обновление

1. Сделать backup `/opt/history-bot`, БД, `.env` и MAX session.
2. Распаковать архив поверх проекта, **не заменяя `.env`, data/history.db и runtime/max_session**.
3. Проверить зависимости через `/opt/history-bot/venv/bin/pip install -r requirements.txt`.
4. Запустить syntax/self-test.
5. `systemctl daemon-reload` не обязателен, если unit не менялся.
6. `systemctl restart history-bot.service`.
7. Проверить `journalctl -u history-bot.service -f`.

## Проверка

```bash
/opt/history-bot/venv/bin/python -m compileall /opt/history-bot
/opt/history-bot/venv/bin/python /opt/history-bot/self_test.py
systemctl status history-bot.service
journalctl -u history-bot.service -n 200 --no-pager
```

Для реального end-to-end теста публикации использовать существующие Telegram/MAX credentials. В архиве нет и не должно быть production secrets.

## Изменённые файлы

- `history_bot/content/daily.py` — новый источник и нормализация событий дня.
- `history_bot/db.py` — совместимая дневная очередь и state machine.
- `history_bot/core.py` — подготовка queue, metadata-only media discovery, event-based draft pipeline и более мягкая media validation.
- `history_bot/admin.py` — фиксированный автопостинг 3 часа, daily preparation в 00:00 и ручная подготовка queue; существующие Telegram/MAX publishers не переписаны.
- `config.yaml` — параметры дневной очереди и media validation.
- `README.md` — архитектура и эксплуатация.

## Контрольный список после установки

- [ ] БД открывается, старые таблицы сохранены.
- [ ] `.env` сохранён.
- [ ] MAX session сохранена.
- [ ] Daily batch создаётся только один раз на дату.
- [ ] В batch попадают события именно текущего календарного дня.
- [ ] Уже опубликованные `topic_key` не попадают в новую очередь.
- [ ] Для события сохраняются только URL/metadata изображений.
- [ ] Битое/нерелевантное изображение не ломает событие целиком.
- [ ] При исчерпании изображений событие пропускается и берётся следующее.
- [ ] GigaChat вызывается только после выбора события и изображения.
- [ ] Telegram/MAX обрабатываются независимо.
- [ ] Событие становится `posted` только после успеха всех настроенных платформ.
- [ ] После публикации временный файл удалён.
- [ ] После restart состояние очереди не теряется.
