import asyncio
import hashlib
import logging
import os
import subprocess
from datetime import time as dt_time, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.request import HTTPXRequest

from .publishers import Publishers
from .youtube_echo import HistoryEchoPublisher


log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


class AdminBot:
    def __init__(self, engine):
        self.e = engine
        self.pub = Publishers()
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.ids = {
            int(x.strip())
            for x in os.getenv("ADMIN_IDS", "").split(",")
            if x.strip().isdigit()
        }
        self.auto_enabled = os.getenv(
            "AUTO_PUBLISH_ENABLED", "0"
        ).strip() == "1"
        try:
            interval = int(
                os.getenv("AUTO_PUBLISH_INTERVAL_MINUTES", "360")
            )
        except ValueError:
            interval = 360
        self.auto_interval = 180

        self.youtube_auto_enabled = os.getenv(
            "HISTORY_ECHO_YOUTUBE_AUTO_ENABLED", "0"
        ).strip() == "1"

        try:
            self.youtube_auto_interval = int(
                os.getenv("HISTORY_ECHO_YOUTUBE_INTERVAL_MINUTES", "360")
            )
        except ValueError:
            self.youtube_auto_interval = 360

        if self.youtube_auto_interval not in (60, 180, 360, 720, 1440):
            self.youtube_auto_interval = 360

        self.youtube_privacy = os.getenv(
            "YOUTUBE_PRIVACY_STATUS", "public"
        ).strip().lower()

        if self.youtube_privacy not in ("public", "private"):
            self.youtube_privacy = "public"

        self._tgmax_lock = asyncio.Lock()
        self._youtube_lock = asyncio.Lock()

    def allowed(self, uid):
        return uid in self.ids

    async def guard(self, update):
        if not update.effective_user or not self.allowed(update.effective_user.id):
            await update.effective_message.reply_text("Нет доступа")
            return False
        return True

    async def notify_admins(self, context, message):
        """Send background publication results to every configured admin."""
        for admin_id in self.ids:
            try:
                await context.bot.send_message(chat_id=admin_id, text=str(message)[:4000])
            except Exception:
                log.exception("ADMIN NOTIFICATION FAILED | admin_id=%s", admin_id)

    def save_auto_settings(self):
        env_path = ROOT / ".env"
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []

        keys = (
            "AUTO_PUBLISH_ENABLED=",
            "AUTO_PUBLISH_INTERVAL_MINUTES=",
        )
        lines = [line for line in lines if not line.startswith(keys)]
        lines += [
            "AUTO_PUBLISH_ENABLED=" + ("1" if self.auto_enabled else "0"),
            "AUTO_PUBLISH_INTERVAL_MINUTES=" + str(self.auto_interval),
        ]
        tmp = env_path.with_suffix(".env.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(env_path)

    def save_youtube_settings(self):
        env_path = ROOT / ".env"

        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []

        keys = (
            "HISTORY_ECHO_YOUTUBE_AUTO_ENABLED=",
            "HISTORY_ECHO_YOUTUBE_INTERVAL_MINUTES=",
            "YOUTUBE_PRIVACY_STATUS=",
        )

        lines = [line for line in lines if not line.startswith(keys)]

        lines += [
            "HISTORY_ECHO_YOUTUBE_AUTO_ENABLED="
            + ("1" if self.youtube_auto_enabled else "0"),
            "HISTORY_ECHO_YOUTUBE_INTERVAL_MINUTES="
            + str(self.youtube_auto_interval),
            "YOUTUBE_PRIVACY_STATUS=" + self.youtube_privacy,
        ]

        tmp = env_path.with_suffix(".env.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(env_path)

        os.environ["HISTORY_ECHO_YOUTUBE_AUTO_ENABLED"] = (
            "1" if self.youtube_auto_enabled else "0"
        )
        os.environ["HISTORY_ECHO_YOUTUBE_INTERVAL_MINUTES"] = str(
            self.youtube_auto_interval
        )
        os.environ["YOUTUBE_PRIVACY_STATUS"] = self.youtube_privacy

    def youtube_menu_keyboard(self):
        toggle = (
            "Выключить автопостинг"
            if self.youtube_auto_enabled
            else "Включить автопостинг"
        )

        hours = self.youtube_auto_interval // 60

        return InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle, callback_data="youtube_auto_toggle")],
            [
                InlineKeyboardButton(
                    f"Интервал: {hours} ч.",
                    callback_data="youtube_interval_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    f"Приватность: {self.youtube_privacy}",
                    callback_data="youtube_privacy_toggle"
                )
            ],
            [
                InlineKeyboardButton(
                    "Создать Shorts сейчас",
                    callback_data="youtube_echo"
                )
            ],
            [InlineKeyboardButton("Назад", callback_data="menu")],
        ])

    def youtube_menu_text(self):
        status = "Включён" if self.youtube_auto_enabled else "Выключен"
        hours = self.youtube_auto_interval // 60

        return (
            "YouTube Shorts\n\n"
            f"Автопостинг: {status}\n"
            f"Интервал: {hours} ч.\n"
            f"Приватность: {self.youtube_privacy}\n\n"
            "Настройки применяются сразу."
        )

    def youtube_interval_keyboard(self):
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1 час", callback_data="youtube_interval_60"),
                InlineKeyboardButton("3 часа", callback_data="youtube_interval_180"),
            ],
            [
                InlineKeyboardButton("6 часов", callback_data="youtube_interval_360"),
                InlineKeyboardButton("12 часов", callback_data="youtube_interval_720"),
            ],
            [
                InlineKeyboardButton("24 часа", callback_data="youtube_interval_1440"),
            ],
            [InlineKeyboardButton("Назад", callback_data="youtube_menu")],
        ])

    def keyboard(self):
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(" Создать пост", callback_data="gen"),
                InlineKeyboardButton(" Предпросмотр", callback_data="preview"),
            ],
            [
                InlineKeyboardButton(" Опубликовать всё", callback_data="pub"),
                InlineKeyboardButton(" Статус", callback_data="status"),
            ],
            [
                InlineKeyboardButton(" Автопостинг", callback_data="auto"),
            ],
        ])

    def auto_menu_keyboard(self):
        toggle = "Выключить" if self.auto_enabled else "Включить"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle, callback_data="auto_toggle")],
            [InlineKeyboardButton("Интервал: 3 часа (фиксировано)", callback_data="auto_fixed")],
            [InlineKeyboardButton("Подготовить события сегодня", callback_data="daily_prepare")],
            [InlineKeyboardButton("Назад", callback_data="menu")],
        ])

    def auto_menu_text(self):
        if self.auto_interval % 60 == 0:
            interval = f"{self.auto_interval // 60} ч."
        else:
            interval = f"{self.auto_interval} мин."
        status = "Включён" if self.auto_enabled else "Выключен"
        return (
            "Автопостинг\n\n"
            f"Статус: {status}\n"
            f"Интервал: {interval}\n\n"
            "Настройки применяются сразу."
        )

    async def reschedule_auto(self, context):
        jq = context.application.job_queue
        for name in (
            "history_auto_publish",
            "history_daily_prepare",
            "history_youtube_auto_publish",
        ):
            for job in jq.get_jobs_by_name(name):
                job.schedule_removal()
        # Daily preparation runs once at 00:00 Europe/Moscow.
        jq.run_daily(self.daily_prepare, time=dt_time(0, 0, tzinfo=ZoneInfo("Europe/Moscow")), name="history_daily_prepare")
        if self.auto_enabled:
            jq.run_repeating(
                self.auto_publish,
                interval=180 * 60,
                first=180 * 60,
                name="history_auto_publish",
            )
            log.info("AUTO PUBLISH ENABLED | fixed interval=180 minutes")


    async def daily_prepare(self, context):
        try:
            count = await asyncio.to_thread(self.e.prepare_daily_batch)
            log.info("DAILY PREP COMPLETE | events=%s", count)
        except Exception:
            log.exception("DAILY PREP FAILED")

    async def show_auto_menu(self, update, context):
        if await self.guard(update):
            await update.effective_message.reply_text(
                self.auto_menu_text(),
                reply_markup=self.auto_menu_keyboard(),
            )

    async def start(self, update, context):
        if await self.guard(update):
            await update.effective_message.reply_text(
                "History Daily — управление",
                reply_markup=self.keyboard(),
            )

    def status_text(self):
        p = self.e.db.latest()

        photo_ok = bool(
            p
            and p.get("media_path")
            and Path(p["media_path"]).is_file()
        )

        auto_status = " включён" if self.auto_enabled else " выключен"
        youtube_status = (
            " включён"
            if os.getenv("HISTORY_ECHO_YOUTUBE_ENABLED", "0").strip() == "1"
            else " выключен"
        )

        last_status = p["status"] if p else "нет данных"

        return (
            " <b>HISTORY DAILY  СТАТУС</b>\n\n"
            f" Всего постов: {self.e.db.post_count()}\n"
            f" Последний статус: {last_status}\n"
            f" Изображение: {' готово' if photo_ok else ' отсутствует'}\n\n"
            f" Telegram: {' настроен' if os.getenv('TELEGRAM_CHANNEL', '').strip() else ' не настроен'}\n"
            f" MAX: {' настроен' if os.getenv('MAX_CHANNEL_ID', '').strip() else ' не настроен'}\n"
            f" YouTube: {youtube_status}\n"
            f" Автопостинг: {auto_status}\n"
            f" Интервал: 3 часа\n"
            f" Событий сегодня: "
            f"{self.e.db.daily_events_count(datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat())}"
        )

    async def generate(self, update, context):
        if not await self.guard(update):
            return
        await update.effective_message.reply_text(
            "Генерирую отдельные изображения через GigaChat для Telegram/MAX и Shorts, затем проверяю OCR..."
        )
        try:
            topic, x, text = await asyncio.to_thread(self.e.generate_tgmax_draft, max_chars=1100)
            self.e.db.save_post(self.e.fp(text), x, text)
            await update.effective_message.reply_text("Черновик создан")
            await self.preview(update, context)
        except Exception as ex:
            log.exception("GENERATE FAILED")
            await update.effective_message.reply_text(
                f"Ошибка создания: {str(ex)[:500]}"
            )

    async def preview(self, update, context):
        if not await self.guard(update):
            return
        p = self.e.db.latest()
        if not p:
            await update.effective_message.reply_text("Черновика нет")
            return
        media_path = p.get("media_path", "")
        if media_path and Path(media_path).is_file():
            with open(media_path, "rb") as f:
                await update.effective_message.reply_photo(
                    photo=f,
                    caption=p["text"][:1024],
                    reply_markup=self.keyboard(),
                )
        else:
            await update.effective_message.reply_text(
                p["text"], reply_markup=self.keyboard()
            )

    def publish_post(self, p, title="История", delete_media=True):
        results = []
        success = {}
        media_path = p.get("media_path", "")
        image_url = p.get("image_url", "")
        daily_event_id = p.get("daily_event_id")
        if daily_event_id:
            self.e.db.set_event_platform_status(int(daily_event_id), "tgmax", "processing", "publication in progress")

        enabled_tg = bool(self.e.setting("publish", "telegram", True))
        enabled_max = bool(self.e.setting("publish", "max", True))
        tg_target = os.getenv("TELEGRAM_CHANNEL", "").strip()
        max_target = os.getenv("MAX_CHANNEL_ID", "").strip()

        statuses = self.e.db.publish_statuses(p["id"])
        if enabled_tg and tg_target and statuses.get("telegram") == "success":
            success["telegram"] = True
            results.append("Telegram: уже опубликовано")
        elif enabled_tg and tg_target:
            try:
                mid = self.pub.telegram(p["text"], media_path, image_url)
                success["telegram"] = True
                self.e.db.publish_log(p["id"], "telegram", tg_target, mid, "success")
                results.append(f"Telegram: опубликовано ({mid})")
            except Exception as ex:
                log.exception("TELEGRAM PUBLISH FAILED")
                success["telegram"] = False
                self.e.db.publish_log(p["id"], "telegram", tg_target, "", "error", str(ex))
                results.append(f"Telegram: ошибка — {str(ex)[:200]}")
        else:
            success["telegram"] = None
            results.append("Telegram: отключён или не настроен")

        if enabled_max and max_target and statuses.get("max") == "success":
            success["max"] = True
            results.append("MAX: уже опубликовано")
        elif enabled_max and max_target:
            try:
                mid = self.pub.max_publish(title, p["text"], media_path)
                success["max"] = True
                self.e.db.publish_log(p["id"], "max", max_target, mid, "success")
                results.append(f"MAX: опубликовано ({mid})")
            except Exception as ex:
                log.exception("MAX PUBLISH FAILED")
                success["max"] = False
                self.e.db.publish_log(p["id"], "max", max_target, "", "error", str(ex))
                results.append(f"MAX: ошибка — {str(ex)[:200]}")
        else:
            success["max"] = None
            results.append("MAX: отключён или не настроен")

        attempted = [v for v in success.values() if v is not None]
        any_ok = any(v is True for v in attempted)
        all_ok = bool(attempted) and all(v is True for v in attempted)

        if daily_event_id:
            if all_ok:
                self.e.db.mark_event_platform_posted(int(daily_event_id), "tgmax")
                topic_key = str(p.get("topic_key") or "").strip()
                topic = str(p.get("topic") or title or "История").strip()
                if topic_key:
                    self.e.db.remember_topic(topic_key, topic)
            else:
                self.e.db.set_event_platform_status(int(daily_event_id), "tgmax", "ready", "; ".join(results))

        if delete_media and all_ok and media_path:
            try:
                photo = Path(media_path)
                if photo.is_file():
                    photo.unlink()
                    self.e.db.mark_media_deleted(media_path)
                    results.append("Фото удалено с сервера")
            except Exception:
                log.exception("MEDIA DELETE FAILED")

        return {
            "results": results,
            "any_ok": any_ok,
            "all_ok": all_ok,
        }

    async def publish(self, update, context):
        if not await self.guard(update):
            return

        p = self.e.db.latest()
        if not p:
            await update.effective_message.reply_text("Черновика нет")
            return

        await update.effective_message.reply_text(
            "Запускаю единую публикацию: сначала Telegram/MAX, затем YouTube Shorts..."
        )

        try:
            result = await self._publish_unified_post(
                p, p.get("topic", "История")
            )

            if result["any_ok"]:
                self.e.db.set_status(
                    p["id"],
                    "published" if result["all_ok"]
                    else "partially_published",
                )

            await update.effective_message.reply_text(
                "\n".join(result["results"])
            )

        except Exception as ex:
            log.exception("MANUAL UNIFIED PUBLISH FAILED")
            await update.effective_message.reply_text(
                f"Ошибка единой публикации: {str(ex)[:500]}"
            )

    async def status(self, update, context):
        if await self.guard(update):
            await update.effective_message.reply_text(self.status_text())

    async def _publish_youtube_for_post(self, p):
        if os.getenv("HISTORY_ECHO_YOUTUBE_ENABLED", "0").strip() != "1":
            return ["YouTube Shorts: отключён"]

        media_path = str(p.get("media_path") or "").strip()
        topic = str(p.get("topic") or "История").strip()
        article = str(p.get("text") or "").strip()
        event_id = p.get("daily_event_id")

        if not article:
            return ["YouTube Shorts: ошибка  отсутствует текст"]

        if not media_path or not Path(media_path).is_file():
            return ["YouTube Shorts: ошибка  отсутствует изображение"]

        image_bytes = Path(media_path).read_bytes()
        article_key = hashlib.sha256(
            (str(event_id) + "\\n" + topic + "\\n" + article).encode()
        ).hexdigest()

        publisher = HistoryEchoPublisher(
            "/opt/history-bot/data/history.db",
            os.getenv(
                "HISTORY_ECHO_WORK_DIR",
                "/opt/history-bot/runtime/history_echo"
            )
        )

        async with self._youtube_lock:
            ok, video_id, error = await asyncio.to_thread(
                publisher.publish_article,
                article,
                image_bytes,
                article_key,
                topic,
                self.e.gigachat,
            )

        if ok:
            if event_id:
                self.e.db.mark_event_platform_posted(
                    int(event_id), "youtube"
                )
            return [f"YouTube Shorts: опубликовано ({video_id})"]

        if event_id:
            self.e.db.set_event_platform_status(
                int(event_id),
                "youtube",
                "ready",
                str(error or "publication failed")[:1000],
            )

        return [f"YouTube Shorts: ошибка  {str(error)[:500]}"]

    async def _publish_unified_post(self, p, title="История"):
        # Строгий порядок: сначала Telegram/MAX, затем YouTube.
        # Оба канала используют один и тот же объект p:
        # p["text"] и p["media_path"].

        try:
            tgmax_result = await asyncio.to_thread(
                self.publish_post, p, title, False
            )
        except Exception as ex:
            log.exception("UNIFIED TGMAX PUBLISH FAILED")
            tgmax_result = {
                "results": [f"TG/MAX: ошибка  {str(ex)[:500]}"],
                "any_ok": False,
                "all_ok": False,
            }

        # YouTube запускается после завершения TG/MAX.
        try:
            youtube_result = await self._publish_youtube_for_post(p)
        except Exception as ex:
            log.exception("UNIFIED YOUTUBE PUBLISH FAILED")
            youtube_result = [
                f"YouTube Shorts: ошибка  {str(ex)[:500]}"
            ]

        results = tgmax_result["results"] + youtube_result

        tgmax_ok = tgmax_result["all_ok"]
        youtube_ok = any("опубликовано" in item for item in youtube_result)
        youtube_disabled = any("отключён" in item for item in youtube_result)

        unified_ok = tgmax_ok and (youtube_ok or youtube_disabled)

        return {
            "results": results,
            "any_ok": tgmax_result["any_ok"] or youtube_ok,
            "all_ok": unified_ok,
        }

    async def auto_publish(self, context):
        if self._tgmax_lock.locked():
            log.warning("AUTO PUBLISH SKIPPED | previous run still active")
            return

        async with self._tgmax_lock:
            log.info("UNIFIED AUTO PUBLISH START")

            try:
                p = self.e.db.latest_unfinished()

                if p:
                    if not p.get("media_path") or not Path(
                        p["media_path"]
                    ).is_file():
                        log.error(
                            "UNFINISHED POST HAS NO VALID LOCAL IMAGE | post_id=%s",
                            p["id"],
                        )
                        return

                    result = await self._publish_unified_post(
                        p, p.get("topic", "История")
                    )

                    await self.notify_admins(
                        context,
                        " Единая публикация\n"
                        + "\n".join(result["results"]),
                    )

                    if result["any_ok"]:
                        self.e.db.set_status(
                            p["id"],
                            "published"
                            if result["all_ok"]
                            else "partially_published",
                        )

                    if not result["all_ok"]:
                        return

                    return

                last = None

                for attempt in range(1, 4):
                    try:
                        topic, x, text = await asyncio.to_thread(
                            self.e.generate_tgmax_draft,
                            max_chars=1100,
                        )

                        post_id = self.e.db.save_post(
                            self.e.fp(text), x, text
                        )
                        p = self.e.db.latest()

                        result = await self._publish_unified_post(
                            p, topic
                        )

                        await self.notify_admins(
                            context,
                            " Единая публикация\n"
                            + "\n".join(result["results"]),
                        )

                        if result["any_ok"]:
                            self.e.db.set_status(
                                post_id,
                                "published"
                                if result["all_ok"]
                                else "partially_published",
                            )

                        if result["all_ok"]:
                            log.info(
                                "UNIFIED AUTO PUBLISH SUCCESS | %s",
                                " | ".join(result["results"]),
                            )
                            return

                        last = RuntimeError(
                            "; ".join(result["results"])
                        )

                    except Exception as ex:
                        last = ex
                        log.exception(
                            "UNIFIED AUTO PUBLISH ATTEMPT FAILED | %s/3",
                            attempt,
                        )

                    await asyncio.sleep(min(60, attempt * 10))

                raise RuntimeError(
                    f"UNIFIED AUTO PUBLISH exhausted retries: {last}"
                )

            except Exception:
                log.exception("UNIFIED AUTO PUBLISH FAILED")

    async def youtube_auto_publish(self, context):
        # YouTube публикуется из общего материала в auto_publish().
        # Отдельная генерация запрещена во избежание дублирования статьи и изображения.
        log.info(
            "YOUTUBE AUTO PUBLISH SKIPPED | unified publication handles YouTube"
        )

    async def youtube_echo(self, update, context):
        if not await self.guard(update):
            return

        if os.getenv("HISTORY_ECHO_YOUTUBE_ENABLED", "0").strip() != "1":
            await update.effective_message.reply_text(
                "YouTube Shorts выключен. "
                "Установите HISTORY_ECHO_YOUTUBE_ENABLED=1 в .env."
            )
            return

        client_secret = os.getenv(
            "HISTORY_ECHO_YOUTUBE_CLIENT_SECRET_FILE",
            "/opt/history-bot/youtube_client_secret.json",
        )
        token_file = os.getenv(
            "HISTORY_ECHO_YOUTUBE_TOKEN_FILE",
            "/opt/history-bot/history_echo_youtube_token.json",
        )

        if not Path(client_secret).is_file():
            await update.effective_message.reply_text(
                f"OAuth-файл не найден: {client_secret}"
            )
            return

        if not Path(token_file).is_file():
            await update.effective_message.reply_text(
                "YouTube OAuth ещё не настроен. "
                "Сначала выполните авторизацию через CLI по SSH."
            )
            return

        await update.effective_message.reply_text(
            "Запускаю создание YouTube Shorts. Это может занять несколько минут..."
        )

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "/usr/bin/python3",
                    "-m",
                    "history_bot.youtube_echo_cli",
                    "--once",
                ],
                cwd="/opt/history-bot",
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )

            output = (result.stdout or "") + (result.stderr or "")
            output = output[-3000:]

            if result.returncode == 0:
                await update.effective_message.reply_text(
                    "YouTube Shorts завершён успешно.\n\n" + output
                )
            else:
                await update.effective_message.reply_text(
                    "Ошибка YouTube Shorts:\n\n" + output
                )

        except subprocess.TimeoutExpired:
            await update.effective_message.reply_text(
                "YouTube Shorts превысил лимит времени 10 минут."
            )
        except Exception as ex:
            log.exception("YOUTUBE ECHO CALLBACK FAILED")
            await update.effective_message.reply_text(
                f"Ошибка запуска YouTube Shorts: {str(ex)[:500]}"
            )

    async def safe_edit_callback(self, q, text, reply_markup=None):
        """Edit text or caption, depending on the original message type."""
        message = q.message

        try:
            if message and message.text is not None:
                await q.edit_message_text(
                    text=text[:4000],
                    reply_markup=reply_markup,
                )
            elif message and message.caption is not None:
                await q.edit_message_caption(
                    caption=text[:1024],
                    reply_markup=reply_markup,
                )
            else:
                await message.reply_text(
                    text,
                    reply_markup=reply_markup,
                )
        except Exception:
            log.exception("SAFE CALLBACK EDIT FAILED")
            await message.reply_text(
                text,
                reply_markup=reply_markup,
            )

    async def callback(self, update, context):
        q = update.callback_query
        await q.answer()
        data = q.data

        if data in ("gen", "regen"):
            await self.generate(update, context)
        elif data == "preview":
            await self.preview(update, context)
        elif data == "pub":
            await self.publish(update, context)
        elif data == "status":
            await self.status(update, context)
        elif data == "youtube_menu":
            if await self.guard(update):
                await self.safe_edit_callback(q, 
                    self.youtube_menu_text(),
                    reply_markup=self.youtube_menu_keyboard(),
                )
        elif data == "youtube_echo":
            await self.youtube_echo(update, context)
        elif data == "youtube_auto_toggle":
            if not await self.guard(update):
                return

            self.youtube_auto_enabled = not self.youtube_auto_enabled
            self.save_youtube_settings()
            await self.reschedule_auto(context)

            await self.safe_edit_callback(q, 
                self.youtube_menu_text(),
                reply_markup=self.youtube_menu_keyboard(),
            )
        elif data == "youtube_interval_menu":
            if await self.guard(update):
                await self.safe_edit_callback(q, 
                    "Выберите интервал автопостинга YouTube:",
                    reply_markup=self.youtube_interval_keyboard(),
                )
        elif data.startswith("youtube_interval_"):
            if not await self.guard(update):
                return

            value = int(data.rsplit("_", 1)[-1])
            if value not in (60, 180, 360, 720, 1440):
                await q.answer("Недопустимый интервал", show_alert=True)
                return

            self.youtube_auto_interval = value
            self.youtube_auto_enabled = True
            self.save_youtube_settings()
            await self.reschedule_auto(context)

            await self.safe_edit_callback(q, 
                self.youtube_menu_text(),
                reply_markup=self.youtube_menu_keyboard(),
            )
        elif data == "youtube_privacy_toggle":
            if not await self.guard(update):
                return

            self.youtube_privacy = (
                "private"
                if self.youtube_privacy == "public"
                else "public"
            )
            self.save_youtube_settings()

            await self.safe_edit_callback(q, 
                self.youtube_menu_text(),
                reply_markup=self.youtube_menu_keyboard(),
            )
        elif data == "auto":
            await self.show_auto_menu(update, context)
        elif data == "menu":
            if await self.guard(update):
                await self.safe_edit_callback(q, 
                    "History Daily — управление",
                    reply_markup=self.keyboard(),
                )
        elif data == "auto_fixed":
            if await self.guard(update):
                await self.safe_edit_callback(q, self.auto_menu_text(), reply_markup=self.auto_menu_keyboard())
        elif data == "daily_prepare":
            if not await self.guard(update):
                return
            try:
                count = await asyncio.to_thread(self.e.prepare_daily_batch)
                await self.safe_edit_callback(q, f"Дневная очередь готова: {count} событий", reply_markup=self.auto_menu_keyboard())
            except Exception as ex:
                await self.safe_edit_callback(q, f"Ошибка подготовки: {str(ex)[:400]}", reply_markup=self.auto_menu_keyboard())
        elif data == "auto_toggle":
            if not await self.guard(update):
                return
            self.auto_enabled = not self.auto_enabled
            self.save_auto_settings()
            await self.reschedule_auto(context)
            await self.safe_edit_callback(q, 
                self.auto_menu_text(),
                reply_markup=self.auto_menu_keyboard(),
            )
        elif data.startswith("auto_int_"):
            if await self.guard(update):
                self.auto_interval = 180
                self.auto_enabled = True
                self.save_auto_settings()
                await self.reschedule_auto(context)
                await self.safe_edit_callback(q, self.auto_menu_text(), reply_markup=self.auto_menu_keyboard())

    def run(self):
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
        if not self.ids:
            raise RuntimeError("ADMIN_IDS не задан")

        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=20.0,
            read_timeout=60.0,
            write_timeout=60.0,
            pool_timeout=20.0,
        )

        app = (
            Application.builder()
            .token(self.token)
            .request(request)
            .build()
        )
        app.add_handler(CommandHandler(["start", "menu"], self.start))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("publish", self.publish))
        app.add_handler(CallbackQueryHandler(self.callback))

        # Daily preparation and the fixed 3-hour publisher are registered together.
        app.job_queue.run_daily(self.daily_prepare, time=dt_time(0, 0, tzinfo=ZoneInfo("Europe/Moscow")), name="history_daily_prepare")
        if self.auto_enabled:
            app.job_queue.run_repeating(
                self.auto_publish,
                interval=180 * 60,
                first=180 * 60,
                name="history_auto_publish",
            )

        # Recovery path after a restart: prepare the current day without waiting until midnight.
        app.job_queue.run_once(self.daily_prepare, when=2, name="history_daily_prepare_startup")
        app.run_polling(drop_pending_updates=False)
