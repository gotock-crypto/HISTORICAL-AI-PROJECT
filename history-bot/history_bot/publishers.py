import asyncio
import html
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import requests
from PIL import Image

from .max.publisher import MaxPublisher


log = logging.getLogger("history_bot.publishers")


class Publishers:
    def __init__(self):
        self.s = requests.Session()

    def prepare_text(self, text, platform="telegram"):
        tg_link = os.getenv("TELEGRAM_CHANNEL_LINK", "").strip()
        max_link = os.getenv("MAX_CHANNEL_LINK", "").strip()

        if platform == "telegram":
            body = html.escape(text.rstrip())
            links = []
            if tg_link:
                links.append(
                    f'<a href="{html.escape(tg_link, quote=True)}">Мы в Telegram</a>'
                )
            if max_link:
                links.append(
                    f'<a href="{html.escape(max_link, quote=True)}">Мы в MAX</a>'
                )
            return body + ("\n\n" + "\n".join(links) if links else "")

        links = []
        if tg_link:
            links.append(f"[Мы в Telegram]({tg_link})")
        if max_link:
            links.append(f"[Мы в MAX]({max_link})")
        return text.rstrip() + ("\n\n" + "\n".join(links) if links else "")

    def optimize_photo(self, media_path):
        source = Path(media_path)
        if not source.is_file():
            return media_path, None

        max_bytes = 9_500_000
        max_side = 2200
        if source.stat().st_size <= max_bytes:
            return media_path, None

        with Image.open(source) as probe:
            width, height = probe.size
        ratio = min(1.0, max_side / max(width, height))
        target = (
            max(1, int(width * ratio)),
            max(1, int(height * ratio)),
        )

        with Image.open(source) as img:
            img.thumbnail(target, Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", img.size, "white")
                bg.paste(img.convert("RGB"), mask=img.getchannel("A"))
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            tmp = tempfile.NamedTemporaryFile(
                suffix=".jpg", delete=False
            )
            tmp_path = tmp.name
            tmp.close()

            for quality in (90, 85, 80, 75, 70, 65):
                img.save(
                    tmp_path, "JPEG", quality=quality,
                    optimize=True, progressive=True
                )
                if Path(tmp_path).stat().st_size <= max_bytes:
                    return tmp_path, tmp_path

            while min(img.size) > 700:
                img = img.resize(
                    (
                        max(1, int(img.size[0] * 0.8)),
                        max(1, int(img.size[1] * 0.8)),
                    ),
                    Image.Resampling.LANCZOS,
                )
                img.save(
                    tmp_path, "JPEG", quality=75,
                    optimize=True, progressive=True
                )
                if Path(tmp_path).stat().st_size <= max_bytes:
                    return tmp_path, tmp_path
            return tmp_path, tmp_path

    def _post(self, base, method, data=None, files=None):
        last = None
        max_attempts = 1 if files else 3
        for attempt in range(1, max_attempts + 1):
            try:
                r = self.s.post(
                    base + "/" + method,
                    data=data,
                    files=files,
                    timeout=90,
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(
                        f"{r.status_code} temporary response"
                    )
                r.raise_for_status()
                result = r.json()
                if not result.get("ok"):
                    raise RuntimeError(str(result))
                return result["result"]
            except Exception as e:
                last = e
                if attempt < max_attempts:
                    import time
                    time.sleep(attempt)
        raise last

    def telegram(self, text, media_path="", image_url=""):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        channel = os.getenv("TELEGRAM_CHANNEL", "").strip()

        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
        if not channel:
            raise RuntimeError("TELEGRAM_CHANNEL не задан")

        media = Path(media_path) if media_path else None
        if not media or not media.is_file():
            raise RuntimeError(
                "Публикация запрещена: обязательное локальное изображение отсутствует"
            )

        base = f"https://api.telegram.org/bot{token}"
        body = self.prepare_text(text, "telegram")
        optimized_path = None

        try:
            send_path, optimized_path = self.optimize_photo(str(media))

            if len(body) <= 1024:
                with open(send_path, "rb") as photo:
                    result = self._post(
                        base,
                        "sendPhoto",
                        data={
                            "chat_id": channel,
                            "caption": body,
                            "parse_mode": "HTML",
                        },
                        files={"photo": photo},
                    )
                return result["message_id"]

            with open(send_path, "rb") as photo:
                self._post(
                    base,
                    "sendPhoto",
                    data={"chat_id": channel},
                    files={"photo": photo},
                )

            result = self._post(
                base,
                "sendMessage",
                data={
                    "chat_id": channel,
                    "text": body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
            return result["message_id"]

        finally:
            if optimized_path:
                Path(optimized_path).unlink(missing_ok=True)

    def max_publish(self, title, text, media_path=""):
        channel = os.getenv("MAX_CHANNEL_ID", "").strip()
        if not channel:
            raise RuntimeError("MAX_CHANNEL_ID не задан")

        s = SimpleNamespace(
            max_phone=os.getenv("MAX_PHONE", "").strip(),
            max_session=os.getenv("MAX_SESSION", "history_max").strip(),
            max_work_dir=os.getenv(
                "MAX_WORK_DIR",
                "/opt/history-bot/runtime/max_session"
            ).strip(),
            max_channel_id=channel,
        )
        body = self.prepare_text(text, "max")

        async def runner():
            p = MaxPublisher(s)
            try:
                return await p.publish("", body, media_path)
            finally:
                await p.close()

        return asyncio.run(runner())
