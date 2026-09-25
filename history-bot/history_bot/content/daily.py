"""Daily historical-event preparation.

The daily queue is built once per calendar day from Wikimedia's structured
On This Day feed. No LLM is used to discover topics. The queue is persisted in
SQLite so the posting loop only consumes prepared events.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Moscow")
WIKIMEDIA_FEED = "https://api.wikimedia.org/feed/v1/wikipedia/{lang}/onthisday/events/{month:02d}/{day:02d}"


def day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).strftime("%m-%d")


def day_label(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).strftime("%d.%m")


def _norm(value: str) -> str:
    value = str(value or "").lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _event_title(text: str, pages: list[dict]) -> str:
    if pages:
        title = str(pages[0].get("normalizedtitle") or pages[0].get("title") or "").strip()
        if title:
            return title.replace("_", " ")
    text = re.sub(r"^\s*(?:\d{1,4}\s*)?", "", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180]


def _importance(text: str, year: int) -> tuple[int, int]:
    hay = _norm(text)
    drama_words = ("war", "battle", "invasion", "revolution", "disaster", "earthquake", "fire", "assassination", "bomb", "independence", "войн", "битв", "революц", "катастроф", "землетряс", "пожар", "убийств", "независим")
    science_words = ("discovered", "invented", "first", "launched", "opened", "founded", "изобр", "открыт", "запущ", "основан")
    importance = 5 + min(4, len(hay.split()) // 25)
    drama = 5
    if any(w in hay for w in drama_words):
        drama += 3
        importance += 1
    if any(w in hay for w in science_words):
        importance += 1
    if year >= 1800:
        importance += 1
    return min(10, importance), min(10, drama)


def canonical_key(month_day: str, year: int, text: str) -> str:
    raw = f"{month_day}|{int(year)}|{_norm(text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _usable(item: dict) -> bool:
    text = str(item.get("text") or "").strip()
    try:
        year = int(item.get("year"))
    except Exception:
        return False
    if not text or year < 1 or year > 2100:
        return False
    low = _norm(text)
    if any(x in low for x in ("born", "died", "birthed", "родил", "умер", "скончал")):
        return False
    # Skip events that are clearly not useful as a standalone post.
    if len(text) < 35:
        return False
    return True


def _convert(rows: list[dict], month_day: str) -> list[dict]:
    result = []
    seen = set()
    for raw in rows:
        if not _usable(raw):
            continue
        year = int(raw["year"])
        text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
        pages = raw.get("pages") or []
        title = _event_title(text, pages)
        key = canonical_key(month_day, year, text)
        if key in seen:
            continue
        seen.add(key)
        importance, drama = _importance(text, year)
        page = pages[0] if pages else {}
        page_title = str(page.get("title") or page.get("normalizedtitle") or title).replace("_", " ")
        extract = str(page.get("extract") or "").strip()
        result.append({
            "topic_key": key,
            "month_day": month_day,
            "year": year,
            "title": title[:240],
            "event": text[:1800],
            "facts": [extract[:700]] if extract else [],
            "importance": importance,
            "drama": drama,
            "wiki_title": page_title[:300],
            "wiki_lang": "",
            "source_url": "",
            "visual": [],
        })
    return result


def fetch_events(request_json, now: datetime | None = None, languages=("en", "ru")) -> list[dict]:
    now = now or datetime.now(TZ)
    month_day = day_label(now)
    rows: list[dict] = []
    for lang in languages:
        url = WIKIMEDIA_FEED.format(lang=lang, month=now.month, day=now.day)
        data = request_json(url, label=f"onthisday:{lang}", attempts=2, timeout=20)
        if not data:
            log.warning("ON THIS DAY SOURCE FAILED | lang=%s", lang)
            continue
        got = data.get("events") or []
        for item in got:
            item = dict(item)
            item["_lang"] = lang
            rows.append(item)
        # English is the primary feed. Only call the Russian feed when the
        # primary source did not give a healthy pool.
        if lang == "en" and len(got) >= 15:
            break
    events = _convert(rows, month_day)
    # Put stronger candidates first, but retain a healthy queue for the day.
    events.sort(key=lambda x: (x["importance"] * 10 + x["drama"] * 6, x["year"]), reverse=True)
    return events
