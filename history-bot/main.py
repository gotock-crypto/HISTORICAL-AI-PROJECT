import sys
import asyncio
import logging
import os
from types import SimpleNamespace

from dotenv import load_dotenv

from history_bot.core import HistoryEngine
from history_bot.admin import AdminBot
from history_bot.max.publisher import MaxPublisher


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

load_dotenv()
e = HistoryEngine()


async def max_login():
    s = SimpleNamespace(
        max_phone=os.getenv("MAX_PHONE", "").strip(),
        max_session=os.getenv("MAX_SESSION", "history_max").strip(),
        max_work_dir=os.getenv(
            "MAX_WORK_DIR", "/opt/history-bot/runtime/max_session"
        ).strip(),
        max_channel_id=os.getenv("MAX_CHANNEL_ID", "").strip(),
    )
    if not s.max_phone:
        raise RuntimeError("MAX_PHONE не задан")

    p = MaxPublisher(s)
    try:
        print("Подключение к MAX...")
        await p.start()
        print("MAX авторизация успешна")
    finally:
        await p.close()


def health():
    checks = []
    checks.append(("config", bool(e.cfg)))
    checks.append(("database", e.db.post_count() >= 0))
    checks.append(("gigachat_key", bool(os.getenv("GIGACHAT_AUTH_KEY", "").strip())))
    checks.append(("telegram_token", bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())))
    checks.append(("admin_ids", bool(os.getenv("ADMIN_IDS", "").strip())))

    # Public source availability only; failures are reported, not fatal.
    for name, url in (
        ("wikimedia", "https://commons.wikimedia.org/w/api.php"),
        ("library_of_congress", "https://www.loc.gov/photos/"),
        ("internet_archive", "https://archive.org/advancedsearch.php"),
    ):
        data = e.request_json(
            url,
            {"action": "query", "format": "json"} if name == "wikimedia"
            else ({"fo": "json", "c": 1} if name == "library_of_congress"
                  else {"q": "mediatype:image", "rows": 1, "output": "json"}),
            f"health:{name}",
            attempts=1,
            timeout=15,
        )
        checks.append((name, data is not None))

    ok = True
    for name, status in checks:
        print(f"{name}: {'OK' if status else 'FAIL'}")
        if name in ("config", "database") and not status:
            ok = False
    return 0 if ok else 1


if "--once" in sys.argv:
    t, x, text = e.generate_draft()
    print(text)
    print("\nTOPIC:", t)
    print("IMAGE:", x.get("image_url", ""))
    print("LOCAL:", x.get("media_path", ""))
    print("SOURCE:", x.get("source_url", ""))

elif "--max-login" in sys.argv:
    asyncio.run(max_login())

elif "--health" in sys.argv:
    raise SystemExit(health())

elif "--admin" in sys.argv:
    AdminBot(e).run()

else:
    print(
        "History Daily Bot\n"
        "Use --admin | --once | --health | --max-login"
    )
