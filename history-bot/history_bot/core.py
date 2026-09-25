from datetime import datetime
import json
import hashlib
import logging
import asyncio
import os
from dotenv import load_dotenv

import sys
sys.path.insert(0, "/opt")
from gigachat_gate import gigachat_gate, gigachat_mark_rate_limit, gigachat_mark_rate_limit_locked

load_dotenv('/opt/history-bot/.env')
import random
import re
import time
import uuid
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
from PIL import Image, ImageFile, ImageOps, ImageEnhance, ImageFilter
from io import BytesIO

from .db import DB
from .content.planner import ContentPlanner
from .content.daily import fetch_events, day_key, day_label, TZ


ImageFile.LOAD_TRUNCATED_IMAGES = False
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = ROOT / "runtime" / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

COMMONS = "https://commons.wikimedia.org/w/api.php"
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata/"


class HistoryEngine:
    def __init__(self):
        self.cfg = yaml.safe_load(
            (ROOT / "config.yaml").read_text(encoding="utf-8")
        ) or {}
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "HistoryDailyBot/11.0 (+archival educational publishing)"
        })
        self.s.verify = os.getenv(
            "REQUESTS_VERIFY_SSL", "1"
        ).strip().lower() in ("1", "true", "yes")
        self.db = DB(ROOT / "data" / "history.db")
        self.planner = ContentPlanner(self.db)
        self.db.recover_daily_processing(__import__('datetime').datetime.now(TZ).date().isoformat())
        self._giga_token = ""
        self._giga_token_until = 0.0
        self._giga_verify_ssl = os.getenv(
            "GIGACHAT_VERIFY_SSL", "1"
        ).strip().lower() in ("1", "true", "yes")

    def setting(self, section, key, default):
        return (self.cfg.get(section) or {}).get(key, default)

    @staticmethod
    def clean(value):
        value = unescape(str(value or ""))
        value = re.sub(r"<[^>]+>", " ", value, flags=re.I)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def norm(value):
        value = str(value or "").lower().replace("ё", "е")
        value = re.sub(r"[^a-zа-я0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def request_json(self, url, params=None, label="request", attempts=3, timeout=30):
        last = None

        is_wikimedia = any(
            host in url
            for host in (
                "commons.wikimedia.org",
                "upload.wikimedia.org",
                "wikipedia.org",
                "wikidata.org",
            )
        )

        for attempt in range(1, attempts + 1):
            try:
                # Wikimedia strongly rate-limits repeated API requests.
                # Respect Retry-After and avoid rapid retry loops.
                if is_wikimedia:
                    cooldown_until = getattr(
                        self,
                        "_wikimedia_cooldown_until",
                        0.0,
                    )
                    now = time.time()

                    if cooldown_until > now:
                        delay = cooldown_until - now
                        log.warning(
                            "WIKIMEDIA COOLDOWN | %.1fs | %s",
                            delay,
                            label,
                        )
                        time.sleep(delay)

                headers = {}
                if is_wikimedia:
                    headers["User-Agent"] = (
                        "HistoryDailyBot/1.0 "
                        "(historical media research bot)"
                    )

                r = self.s.get(
                    url,
                    params=params,
                    headers=headers or None,
                    timeout=timeout,
                )

                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")

                    try:
                        delay = max(20.0, float(retry_after))
                    except (TypeError, ValueError):
                        delay = min(120.0, 20.0 * attempt)

                    if is_wikimedia:
                        self._wikimedia_cooldown_until = (
                            time.time() + delay
                        )

                    last = requests.HTTPError(
                        f"429 rate limited | retry_after={delay}s"
                    )

                    log.warning(
                        "RATE LIMITED | %s | %s/%s | sleep=%.1fs",
                        label,
                        attempt,
                        attempts,
                        delay,
                    )

                    if attempt < attempts:
                        time.sleep(delay)
                        continue

                    break

                if r.status_code in (408, 425, 500, 502, 503, 504):
                    raise requests.HTTPError(
                        f"{r.status_code} temporary response"
                    )

                r.raise_for_status()
                return r.json()

            except Exception as e:
                last = e

                log.warning(
                    "REQUEST RETRY | %s | %s/%s | %s",
                    label,
                    attempt,
                    attempts,
                    e,
                )

                if attempt < attempts and not (
                    is_wikimedia and "429" in str(e)
                ):
                    time.sleep(
                        min(8, 0.8 * attempt) + random.random()
                    )

        log.error(
            "REQUEST FAILED | %s | %s",
            label,
            last,
        )
        return None

    def domains(self):
        return [
            ("Российская империя", "Moscow", "1890s"),
            ("Российская империя", "Saint Petersburg", "1900s"),
            ("СССР", "Moscow", "1930s"),
            ("СССР", "Leningrad", "1950s"),
            ("города мира", "Paris", "1920s"),
            ("города мира", "London", "1900s"),
            ("города мира", "New York", "1910s"),
            ("города мира", "Berlin", "1920s"),
            ("города мира", "Prague", "1930s"),
            ("города мира", "Vienna", "1910s"),
            ("города мира", "Rome", "1920s"),
            ("города мира", "Chicago", "1910s"),
        ]


    def _date_topic_key(self, p):
        """
        Stable canonical identity:
        same calendar date + same year + same event = same topic forever.
        """
        day = str(p.get("date_label", "")).strip()
        year = str(p.get("year", "")).strip()
        event = self.norm(str(p.get("event", "")).strip())

        raw = f"{day}|{year}|{event}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _date_topic_already_used(self, p, key):
        """
        Check both canonical key and existing historical topic titles.
        """
        if self.db.topic_used(key):
            return True

        title = self.norm(str(p.get("title", "")).strip())
        event = self.norm(str(p.get("event", "")).strip())

        if not title and not event:
            return False

        rows = self.db.c.execute(
            "SELECT topic, topic_key FROM topics"
        ).fetchall()

        title_tokens = {x for x in title.split() if len(x) >= 5}

        for old_topic, old_key in rows:
            old_topic = self.norm(str(old_topic or ""))

            if old_key == key:
                return True

            if title and old_topic == title:
                return True

            old_tokens = {x for x in old_topic.split() if len(x) >= 5}

            if title_tokens and old_tokens:
                overlap = len(title_tokens & old_tokens) / max(
                    1, min(len(title_tokens), len(old_tokens))
                )
                if overlap >= 0.80:
                    return True

        return False

    def _used_topic_titles(self, limit=200):
        """Return permanently used topic titles for duplicate prevention."""
        try:
            rows = self.db.c.execute(
                "SELECT topic FROM topics ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return {
                self.norm(str(row[0] or ""))
                for row in rows
                if self.norm(str(row[0] or ""))
            }
        except Exception:
            return set()

    def generate_date_event(self, exclude_titles=None):
        """Return the next event from the persisted daily queue.

        Topic discovery is deliberately not an LLM operation anymore. The
        daily queue is populated once from Wikimedia On This Day and then
        consumed locally throughout the day.
        """
        return self.next_daily_event()

    def plan_topic(self, rejected_titles=None):
        p = self.next_daily_event()
        if not p:
            raise RuntimeError("Дневная очередь исторических событий пуста")
        return p

    def _daily_event_payload(self, row):
        facts = []
        try:
            facts = json.loads(row.get("facts") or "[]")
        except Exception:
            pass
        return {
            "id": row["id"],
            "daily_event_id": row["id"],
            "title": row["title"],
            "topic": row["title"],
            "topic_key": row["topic_key"],
            "event": row["event"],
            "year": row["year"],
            "event_date": row["month_day"],
            "date_label": row["month_day"],
            "facts": facts,
            "importance": row.get("importance", 5),
            "drama": row.get("drama", 5),
            "domain": "История",
            "location": "Исторический контекст",
            "period": str(row["year"]),
            "subject": "event_today",
            "type": "event_today",
        }

    def prepare_daily_batch(self, force=False):
        """Build today's event + media queue exactly once and persist it."""
        from datetime import datetime
        now = datetime.now(TZ)
        batch_date = now.date().isoformat()
        md = day_label(now)

        removed = self.db.cleanup_old_daily_data(batch_date)
        if removed:
            log.info(
                "DAILY OLD DATA CLEANED | before=%s | events=%s",
                batch_date,
                removed,
            )

        existing = self.db.daily_batch(batch_date)
        if existing and existing["status"] == "ready" and not force:
            log.info("DAILY BATCH EXISTS | date=%s | events=%s", batch_date, self.db.daily_events_count(batch_date))
            return self.db.daily_events_count(batch_date)

        batch = self.db.create_daily_batch(batch_date, md)
        try:
            raw = fetch_events(self.request_json, now=now)
            used = set()
            selected = []
            for item in raw:
                key = item["topic_key"]
                if key in used or self.db.topic_used(key):
                    continue
                # Existing topics may have been saved under an older title key.
                if self._date_topic_already_used(item, key):
                    continue
                used.add(key)
                selected.append(item)
                if len(selected) >= int(self.setting("daily", "events_per_day", 15)):
                    break
            if len(selected) < int(self.setting("daily", "minimum_events", 12)):
                log.warning("DAILY QUEUE SMALL | date=%s | events=%s", batch_date, len(selected))

            for item in selected:
                row = self.db.add_daily_event(batch["id"], item)
                p = self._daily_event_payload(row)
                try:
                    # Media is generated from the finished article at draft time.
                    # Keep one persisted placeholder for backward-compatible queue storage.
                    candidate = {
                        "image_url": f"gigachat://history/{row['id']}",
                        "source_url": "",
                        "source_name": "GigaChat generated",
                        "title": row["title"],
                        "metadata": "generated_per_platform",
                        "score": 100,
                    }
                    self.db.add_daily_media(row["id"], candidate, 1)
                except Exception as exc:
                    log.warning("DAILY MEDIA PREP FAILED | event=%s | %s", row["title"], exc)
                    self.db.set_event_platform_status(
                        row["id"], "tgmax", "ready",
                        f"media preparation: {exc}"
                    )
                    self.db.set_event_platform_status(
                        row["id"], "youtube", "ready",
                        f"media preparation: {exc}"
                    )
            self.db.set_daily_batch_status(batch["id"], "ready")
            log.info("DAILY BATCH READY | date=%s | events=%s", batch_date, self.db.daily_events_count(batch_date))
            return self.db.daily_events_count(batch_date)
        except Exception as exc:
            self.db.set_daily_batch_status(batch["id"], "error")
            log.exception("DAILY BATCH FAILED | %s", exc)
            raise

    def next_daily_event(self):
        """Backward-compatible adapter for the Telegram/MAX queue."""
        from datetime import datetime

        batch_date = datetime.now(TZ).date().isoformat()

        if not self.db.daily_batch_ready(batch_date):
            self.prepare_daily_batch()

        p = self.db.next_daily_event_for_platform(batch_date, "tgmax")
        if not p:
            return None

        return self._daily_event_payload(p)

    def search_daily_media(self, p):
        """Metadata-only historical image discovery with layered event-specific search."""
        out, seen = [], set()

        title = self.clean(p.get("title"))
        year = str(p.get("year"))
        event = self.clean(p.get("event"))

        # Extract meaningful words from the event.
        words = [
            w.lower()
            for w in re.findall(
                r"[A-Za-zА-Яа-яЁё0-9-]+",
                event,
            )
            if len(w) >= 5
        ]

        generic = {
            "about", "after", "before", "during", "event", "events",
            "history", "historical", "people", "country", "countries",
            "state", "states", "world", "first", "second", "third",
            "war", "battle", "begins", "takes", "place", "officially",
            "soviet", "union", "united", "kingdom", "american",
            "heads", "toward", "sets", "motion", "which", "their",
            "there", "these", "those", "later", "following",
            "trial", "deposed", "sentence", "money", "drug",
            "trafficking", "laundering", "years", "ends",
        }

        title_words = {
            w.lower()
            for w in re.findall(
                r"[A-Za-zА-Яа-яЁё0-9-]+",
                title,
            )
            if len(w) >= 5
        }

        anchors = []
        for w in words:
            if w not in generic and w not in title_words and w not in anchors:
                anchors.append(w)

        queries = []

        # 1. Exact historical phrase from the event.
        # Prefer the most informative capitalized words/phrases.
        proper = [
            w.lower()
            for w in re.findall(
                r"(?<!^)(?<![.!?]\s)[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]{4,}",
                p.get("event", ""),
            )
        ]

        proper = [
            w for w in proper
            if w not in {
                "Soviet", "United", "American", "First",
                "Second", "Third", "The",
            }
        ]

        # 2. Pairs of concrete anchors.
        # Use proper-name phrases only when they are not generic
        # event vocabulary and do not duplicate the title.
        proper_filtered = [
            w for w in proper
            if w not in title_words
            and w not in generic
        ]

        if len(proper_filtered) >= 2:
            queries.append(
                f'"{" ".join(proper_filtered[:3])}"'
            )

        if len(anchors) >= 2:
            queries.append(
                f'"{anchors[0]}" "{anchors[1]}"'
            )

        # 3. Important multi-word phrases explicitly present in event.
        event_lower = event.lower()

        phrases = [
            "cuban missile crisis",
            "first sino-japanese war",
            "franco-prussian war",
            "american civil war",
            "world war i",
            "world war ii",
            "korean war",
            "battle of peleliu",
            "battle of dobro pole",
            "16th street baptist church",
            "parsons green",
        ]

        for phrase in phrases:
            if phrase in event_lower or phrase in title.lower():
                queries.append(f'"{phrase}" {year}')

        # 4. Exact title + year.
        if title:
            queries.append(f'"{title}"')

        # 5. Use only meaningful multi-word contextual queries.
        # Avoid weak isolated words such as "money", "sentence", or "ends".
        weak_anchors = {
            "money", "sentence", "ends", "ended", "trial",
            "years", "United", "States", "drug", "trafficking",
            "officially", "following", "during", "after",
        }

        strong_anchors = [
            a for a in anchors
            if a.lower() not in weak_anchors
        ]

        if title and strong_anchors:
            queries.append(
                f'"{title}" "{strong_anchors[0]}"'
            )

        if len(strong_anchors) >= 2:
            queries.append(
                f'"{strong_anchors[0]}" "{strong_anchors[1]}"'
            )

        # 6. Generic historical fallback.
        if title:
            queries.append(f'"{title}" photograph')

        unique_queries = []
        for q in queries:
            q = self.clean(q)
            if q and q not in unique_queries:
                unique_queries.append(q)

        for q in unique_queries[:12]:
            data = self.request_json(
                COMMONS,
                {
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": q,
                    "gsrnamespace": 6,
                    "gsrlimit": 50,
                    "prop": "imageinfo|info|categories",
                    "iiprop": "url|extmetadata|size|mime",
                    "iiurlwidth": 1600,
                    "inprop": "url",
                    "format": "json",
                },
                f"daily:commons:{q}",
                attempts=2,
                timeout=20,
            )

            if not data:
                continue

            for page in (
                data.get("query", {}).get("pages", {}) or {}
            ).values():
                ii = (page.get("imageinfo") or [{}])[0]

                url = ii.get("thumburl") or ii.get("url") or ""
                if not url or url in seen:
                    continue

                mime = str(ii.get("mime") or "").lower()
                if mime not in {"image/jpeg", "image/png", "image/webp"}:
                    continue

                meta = ii.get("extmetadata") or {}

                desc = str(
                    (meta.get("ImageDescription") or {}).get("value") or ""
                )

                categories = [
                    str(c.get("title", ""))
                    for c in page.get("categories", [])
                    if c.get("title")
                ]

                candidate = {
                    "title": page.get("title", ""),
                    "description": desc,
                    "source_url": (
                        page.get("canonicalurl", "")
                        or "https://commons.wikimedia.org/"
                    ),
                    "metadata": " ".join(categories),
                }

                hay = self.norm(" ".join([
                    candidate["title"],
                    candidate["description"],
                    candidate["metadata"],
                ]))

                if self.bad_media(hay):
                    continue

                score = float(
                    self._history_primary_subject_score(
                        p,
                        candidate,
                    )
                )

                if score < float(
                    self.setting("media", "min_match_score", 4)
                ):
                    continue

                seen.add(url)

                out.append({
                    "image_url": url,
                    "source_url": candidate["source_url"],
                    "source_name": "Wikimedia Commons",
                    "title": candidate["title"],
                    "metadata": json.dumps(
                        {
                            "mime": ii.get("mime"),
                            "width": ii.get("width"),
                            "height": ii.get("height"),
                            "description": desc,
                            "categories": categories,
                            "query": q,
                        },
                        ensure_ascii=False,
                    ),
                    "score": score,
                })

        out.sort(
            key=lambda x: (
                float(x.get("score", 0)),
                -len(str(x.get("title", ""))),
            ),
            reverse=True,
        )

        return out[:8]

    def queries(self, p):
        """Build broad event-specific archival queries; ranking happens after download."""
        title = self.clean(p.get("title") or p.get("topic") or "")
        year = str(p.get("year") or "").strip()
        event = self.clean(p.get("event") or "")
        queries = []

        supplied = p.get("visual") or []
        for q in supplied:
            q = self.clean(q)
            if q and q not in queries:
                queries.append(q)

        base = [
            f'"{title}" {year}',
            f'"{title}" historical photograph',
            f'"{title}" archive photograph',
            f'"{title}" {year} archive',
        ]

        # A second query uses the first useful words from the event description.
        words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9-]+", event) if len(w) >= 5]
        if words:
            base.append(f'{" ".join(words[:6])} {year} historical photograph')

        for q in base:
            q = self.clean(q)
            if q and q not in queries:
                queries.append(q)
        return queries[:8]

    def bad_media(self, hay):
        return any(w in hay for w in (
            "illustration", "engraving", "painting", "oil painting",
            "watercolor", "artwork", "canvas", "drawing", "sketch",
            "lithograph", "map", "diagram", "poster", "logo", "stamp",
            "coat of arms", "woodcut", "printmaking", "digital art",
            "ai generated", "portrait painting", "cartoon", "comic",
            "advertisement", "book cover", "album cover", "sculpture",
            "museum object", "postcard design",
            "historical marker", "historical markers",
            "memorial plaque", "memorial tablet",
            "information plaque", "commemorative plaque",
            "tourist sign", "road sign", "street sign",
            "pdf", "document scan", "scanned document",
            "book page", "newspaper page", "manuscript"
        ))

    def _match_score(self, p, title, desc, date="", extra=""):
        if p.get("visual"):
            hay = self.norm(" ".join([title, desc, date, extra]))
            if self.bad_media(hay):
                return -999
            tokens=[]
            for q in p.get("visual", []):
                tokens += [t for t in self.norm(q).split() if len(t) > 3]
            title_tokens = [t for t in self.norm(p.get("title", "")).split() if len(t) > 4]
            hits = sum(1 for t in set(tokens) if t in hay)
            title_hits = sum(1 for t in set(title_tokens) if t in hay)
            score = hits * 2 + title_hits * 3
            if any(x in hay for x in ("photograph", "photo", "archive", "historical", "collection")):
                score += 5
            if desc:
                score += 2
            # For modern events we strongly prefer actual photographs. Ancient stories may use
            # contextual photographs of sites, artifacts and monuments.
            if p.get("visual_kind") == "photo" and not any(x in hay for x in ("photograph", "photo", "archive")):
                score -= 2
            return score if hits else 1
        hay = self.norm(" ".join([title, desc, date, extra]))
        if self.bad_media(hay):
            return -999

        score = 0
        loc = self.norm(p["location"])
        location_aliases = {
            "moscow": ("moscow", "москва"),
            "saint petersburg": ("saint petersburg", "st petersburg", "петербург", "ленинград"),
            "leningrad": ("leningrad", "ленинград", "saint petersburg", "петербург"),
            "new york": ("new york", "nyc"),
            "paris": ("paris", "париж"),
            "london": ("london", "лондон"),
            "berlin": ("berlin", "берлин"),
            "prague": ("prague", "praha", "прага"),
            "vienna": ("vienna", "wien", "виена", "вена"),
            "rome": ("rome", "roma", "рим"),
            "chicago": ("chicago", "чикаго"),
        }
        aliases = location_aliases.get(loc, (loc,))
        if any(a in hay for a in aliases):
            score += 8
        else:
            return -999

        subject_words = {
            "городской быт": ("street", "daily", "life", "people", "город", "улиц"),
            "рынки и торговля": ("market", "shop", "vendor", "trade", "рынок", "торгов"),
            "профессии": ("worker", "occupation", "work", "employee", "рабоч", "професс"),
            "детство": ("child", "children", "boy", "girl", "дет"),
            "мода": ("fashion", "clothing", "dress", "costume", "мод", "одеж"),
            "школа": ("school", "classroom", "pupil", "student", "школ"),
            "кафе и рестораны": ("cafe", "restaurant", "dining", "coffee", "каф", "ресторан"),
            "почта и связь": ("post", "mail", "telephone", "telegraph", "почт", "телефон"),
            "праздники": ("festival", "celebration", "parade", "празд", "парад"),
            "работа": ("worker", "work", "workplace", "factory", "рабоч", "фабрик"),
            "улицы и дворы": ("street", "courtyard", "road", "улиц", "двор"),
            "архитектура": ("architecture", "building", "house", "street", "архитект", "здани"),
            "парки": ("park", "garden", "парк", "сад"),
            "спорт": ("sport", "athlete", "football", "sport", "спорт"),
            "музыка": ("music", "musician", "concert", "оркестр", "музык"),
            "транспорт": ("tram", "rail", "train", "transport", "carriage", "трам", "желез", "транспорт"),
        }
        tokens = subject_words.get(p["subject"], ())
        hits = sum(1 for token in tokens if token in hay)
        if hits:
            score += min(6, hits * 2)
        else:
            return -999

        years = [int(y) for y in re.findall(r"\b(18\d{2}|19\d{2}|20[01]\d)\b", hay)]
        target = int(re.sub(r"[^0-9]", "", p["period"])[:4] or 0)
        if years and target:
            if any(abs(y - target) <= 20 for y in years):
                score += 4
            elif all(abs(y - target) > 40 for y in years):
                score -= 5

        if any(x in hay for x in ("photograph", "photo", "photography", "photographs")):
            score += 3
        if desc:
            score += 1
        return score

    def commons_candidates(self, p):
        out, seen = [], set()
        limit = int(self.setting("media", "max_candidates", 50))
        minw = int(self.setting("media", "min_width", 900))

        generic_terms = {
            "painting", "paintings",
            "photo", "photos", "photograph", "photographs",
            "historical", "history", "archive", "archival",
            "museum", "collection", "image", "images",
            "picture", "pictures", "portrait", "portraits",
        }

        def meaningful_tokens(q):
            return [
                t for t in self.norm(q).split()
                if len(t) > 3 and t not in generic_terms
            ]

        for q in self.queries(p):
            query_tokens = meaningful_tokens(q)
            if not query_tokens:
                continue

            data = self.request_json(
                COMMONS,
                {
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": q,
                    "gsrnamespace": 6,
                    "gsrlimit": min(limit, 20),
                    "prop": "imageinfo|info|categories",
                    "iiprop": "url|extmetadata|size|mime",
                    "iiurlwidth": 1600,
                    "iiurlheight": 1200,
                    "inprop": "url",
                    "cllimit": 50,
                    "format": "json",
                },
                f"commons:{q}",
            )

            if not data:
                continue

            for page in data.get("query", {}).get("pages", {}).values():
                ii = (page.get("imageinfo") or [{}])[0]

                original_url = ii.get("url", "")
                thumb_url = ii.get("thumburl", "")
                image_url = thumb_url or original_url

                if not image_url:
                    continue

                if image_url in seen or original_url in seen:
                    continue

                meta = ii.get("extmetadata") or {}

                title = self.clean(page.get("title", ""))
                desc = self.clean(
                    (meta.get("ImageDescription") or {}).get("value", "")
                )
                date = self.clean(
                    (meta.get("DateTimeOriginal") or {}).get("value", "")
                )
                cats = " ".join(
                    self.clean(c.get("title", ""))
                    for c in page.get("categories", [])
                )

                hay = self.norm(" ".join([title, desc, date, cats]))
                title_hay = self.norm(title)
                cats_hay = self.norm(cats)

                # Строгая привязка к конкретному запросу:
                # хотя бы одно содержательное слово запроса должно быть
                # именно в названии файла/страницы или категориях.
                title_cat_hits = sum(
                    1 for t in set(query_tokens)
                    if t in title_hay or t in cats_hay
                )
                if title_cat_hits < 1:
                    continue

                # Для запросов из двух и более содержательных слов
                # стараемся требовать хотя бы два совпадения.
                if len(set(query_tokens)) >= 2 and title_cat_hits < 2:
                    continue

                query_hits = sum(1 for t in set(query_tokens) if t in hay)

                if self.db.media_used(image_url) or self.db.media_used(original_url):
                    continue

                w = int(ii.get("width") or 0)
                h = int(ii.get("height") or 0)
                mime = str(ii.get("mime") or "").lower()

                if w < minw or h < 550:
                    continue

                if mime not in ("image/jpeg", "image/png", "image/webp"):
                    continue

                score = self._match_score(p, title, desc, date, cats)
                if score < int(self.setting("media", "min_match_score", 10)):
                    continue

                item = {
                    **p,
                    "title": title,
                    "image_url": image_url,
                    "original_image_url": original_url,
                    "description": desc,
                    "date": date,
                    "width": int(ii.get("width") or 0),
                    "height": int(ii.get("height") or 0),
                    "thumb_width": int(ii.get("thumbwidth") or 0),
                    "thumb_height": int(ii.get("thumbheight") or 0),
                    "source_url": page.get("fullurl", ""),
                    "search_query": q,
                    "score": score,
                    "source_name": "Wikimedia Commons",
                    "metadata": cats[:4000],
                }

                out.append(item)
                seen.add(image_url)
                seen.add(original_url)

        out.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
        return out

    def internet_archive_candidates(self, p):
        """Wide Internet Archive discovery. Real-file validation happens later."""
        out = []
        seen_urls = set()
        seen_identifiers = set()

        for q in self.queries(p)[:4]:
            data = self.request_json(
                IA_SEARCH,
                {
                    "q": f"({q}) AND mediatype:image",
                    "fl[]": ["identifier", "title", "description", "date", "year"],
                    "rows": 20,
                    "page": 1,
                    "output": "json",
                },
                f"internet-archive:{q}",
            )
            if not data:
                continue

            docs = (data.get("response") or {}).get("docs") or []
            for item in docs:
                identifier = self.clean(item.get("identifier", ""))
                if not identifier or identifier in seen_identifiers:
                    continue
                seen_identifiers.add(identifier)

                title = self.clean(item.get("title", ""))
                desc = self.clean(item.get("description", ""))
                date = self.clean(item.get("date") or item.get("year") or "")
                score = self._match_score(p, title, desc, date)

                # Do not kill results on metadata score. Minerals uses the same idea:
                # discover broadly, then judge the downloaded file.
                if score < 0:
                    continue

                meta = self.request_json(
                    IA_META + quote(identifier, safe=""),
                    label=f"internet-archive-meta:{identifier}",
                    attempts=2,
                    timeout=35,
                )
                if not meta:
                    continue

                image_files = []
                for f in meta.get("files") or []:
                    name = str(f.get("name") or "")
                    if not re.search(r"\.(jpg|jpeg|png|webp)$", name.lower()):
                        continue
                    try:
                        size = int(f.get("size") or 0)
                    except Exception:
                        size = 0
                    image_files.append((name, size))

                image_files.sort(key=lambda z: z[1], reverse=True)

                for name, size in image_files[:10]:
                    url = (
                        f"https://archive.org/download/{quote(identifier, safe='')}/"
                        f"{quote(name, safe='/')}"
                    )
                    if url in seen_urls or self.db.media_used(url):
                        continue
                    seen_urls.add(url)

                    out.append({
                        **p,
                        "title": title,
                        "image_url": url,
                        "description": desc,
                        "date": date,
                        "source_url": f"https://archive.org/details/{quote(identifier, safe='')}",
                        "search_query": q,
                        "score": score,
                        "archive_size": size,
                        "source_name": "Internet Archive",
                        "metadata": identifier,
                    })

        out.sort(
            key=lambda x: (
                float(x.get("score", 0)),
                int(x.get("archive_size", 0)),
            ),
            reverse=True,
        )
        return out[:120]

    def _event_media_fit(self, p, candidate):
        """Strict metadata relevance gate for historical-event images."""
        if not p.get("event"):
            return True

        hay = self.norm(" ".join([
            candidate.get("title", ""),
            candidate.get("description", ""),
            candidate.get("date", ""),
            candidate.get("metadata", ""),
        ]))

        if self.bad_media(hay):
            return False

        visual_tokens = []
        for q in p.get("visual") or []:
            visual_tokens.extend(
                t for t in self.norm(q).split() if len(t) >= 5
            )

        title_tokens = [
            t for t in self.norm(p.get("title", "")).split()
            if len(t) >= 5
        ]

        visual_hits = len({t for t in visual_tokens if t in hay})
        title_hits = len({t for t in title_tokens if t in hay})

        try:
            target_year = int(p.get("year"))
        except (TypeError, ValueError):
            target_year = None

        years = [int(y) for y in re.findall(r"\b(15\d{2}|16\d{2}|17\d{2}|18\d{2}|19\d{2}|20[0-3]\d)\b", hay)]
        year_ok = not years or target_year is None or any(abs(y - target_year) <= 20 for y in years)

        return year_ok and (title_hits >= 1 or visual_hits >= 2)

    def search_media(self, p):
        return self.search_daily_media(p)

    def context(self, p, x):
        if p.get("event"):
            return f"""FACT_CONTEXT:
TITLE: {p.get('title','')}
EVENT: {p.get('event','')}
FACTS: {' | '.join(p.get('facts') or [])}
ANGLE: {p.get('angle','')}
YEAR: {p.get('year','')}
TYPE: {p.get('type','')}

IMAGE_CONTEXT:
TITLE: {x.get('title','')}
DATE: {x.get('date','')}
DESCRIPTION: {x.get('description','')}
SOURCE: {x.get('source_name','')}"""
        return f"""TOPIC: {x.get('topic', p['topic'])}
DOMAIN: {p['domain']}
LOCATION: {p['location']}
PERIOD: {p['period']}
SUBJECT: {p['subject']}
ANGLE: {p['angle']}
IMAGE TITLE: {x.get('title', '')}
IMAGE DATE: {x.get('date', '')}
IMAGE DESCRIPTION: {x.get('description', '')}
IMAGE METADATA: {x.get('metadata', '')}
MEDIA SOURCE: {x.get('source_name', '')}
SOURCE: {x.get('source_url', '')}"""

    def _get_giga_token(self):
        now = time.time()
        if self._giga_token and now < self._giga_token_until:
            return self._giga_token

        key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
        if not key:
            raise RuntimeError("GIGACHAT_AUTH_KEY не задан")

        url = os.getenv(
            "GIGACHAT_OAUTH_URL",
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        )
        r = self.s.post(
            url,
            headers={
                "Authorization": "Basic " + key,
                "RqUID": str(uuid.uuid4()),
            },
            data={"scope": os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")},
            timeout=45,
            verify=self._giga_verify_ssl,
        )
        r.raise_for_status()
        data = r.json()
        token = data["access_token"]
        expires = int(data.get("expires_at") or 0)
        self._giga_token = token
        self._giga_token_until = (
            expires / 1000 - 60 if expires > 10_000_000_000
            else now + 1500
        )
        return token

    def gigachat(self, ctx):
        if "VISUAL_PROMPT_CONTEXT:" in ctx:
            prompt_name = "visual_prompt.txt"
        elif "FACT_CONTEXT:" in ctx:
            prompt_name = "story_writer.txt"
        else:
            prompt_name = "archive_writer.txt"

        prompt = (ROOT / "history_bot" / "prompts" / prompt_name).read_text(encoding="utf-8")
        base = os.getenv("GIGACHAT_API_BASE", "https://api.giga.chat/v1").rstrip("/")
        timeout = int(os.getenv("GIGACHAT_TIMEOUT", "120"))
        last = None

        # ВАЖНО: вся операция, включая retry после 429,
        # выполняется последовательно внутри глобальной очереди.
        with gigachat_gate():
            for attempt in range(1, 5):
                try:
                    token = self._get_giga_token()

                    r = self.s.post(
                        base + "/chat/completions",
                        headers={"Authorization": "Bearer " + token},
                        json={
                            "model": os.getenv("GIGACHAT_MODEL", "GigaChat"),
                            "messages": [{
                                "role": "user",
                                "content": prompt + "\n\nSOURCE_CONTEXT:\n" + ctx,
                            }],
                            "temperature": 0.35,
                            "max_tokens": int(os.getenv("GIGACHAT_MAX_TOKENS", "1100")),
                        },
                        timeout=timeout,
                        verify=self._giga_verify_ssl,
                    )

                    if r.status_code in (401, 403):
                        self._giga_token = ""
                        self._giga_token_until = 0
                        raise requests.HTTPError(f"{r.status_code} token reset")

                    if r.status_code in (429, 500, 502, 503, 504):
                        if r.status_code == 429:
                            gigachat_mark_rate_limit_locked(10)

                        body = (r.text or "").replace("\\n", " ")[:1000]

                        log.warning(
                            "GIGACHAT HTTP ERROR | status=%s | body=%s",
                            r.status_code,
                            body,
                        )

                        raise requests.HTTPError(
                            f"{r.status_code} temporary response: {body}"
                        )

                    r.raise_for_status()

                    text = r.json()["choices"][0]["message"]["content"].strip()

                    if text:
                        return text

                    raise RuntimeError("GigaChat вернул пустой текст")

                except Exception as e:
                    last = e

                    log.warning(
                        "GIGACHAT RETRY | %s/4 | %s",
                        attempt,
                        e,
                    )

                    if attempt < 4:
                        delay = 10 if "429" in str(e) or "Too Many Requests" in str(e) else attempt * 2

                        log.warning(
                            "GIGACHAT QUEUE RETRY WAIT | seconds=%s",
                            delay,
                        )

                        time.sleep(delay)

        raise RuntimeError(f"GigaChat unavailable: {last}")

    def deterministic_fallback(self, p, x):
        if p.get("event"):
            tags = ["#История", "#" + re.sub(r"[^A-Za-zА-Яа-я0-9]", "", p.get("type", "История").title())]
            facts = [self.clean(f) for f in (p.get("facts") or []) if self.clean(f)]
            body = ["📜 " + p.get("title", "История"), p.get("angle", ""), p.get("event", "")]
            body.extend(facts[:3])
            body.append("Эти факты важны не сами по себе: они показывают, как конкретное событие повлияло на людей, решения и дальнейший ход истории.")
            return "\n\n".join([x for x in body if x]) + "\n\n" + " ".join(tags)
        title = self.clean(x.get("title", "Архивная фотография"))
        desc = self.clean(x.get("description", ""))
        date = self.clean(x.get("date", ""))
        source = self.clean(x.get("source_name", "архивный источник"))
        parts = [
            f"{title}.",
            f"Снимок найден в источнике {source} и отобран для темы «{p['topic']}».",
        ]
        if date:
            parts.append(f"В архивной записи указана дата или период: {date}.")
        if desc:
            parts.append(desc)
        parts.append(
            "Этот материал публикуется с опорой на описание самой архивной записи. "
            "Если источник не раскрывает отдельные детали, бот не добавляет их от себя."
        )
        tags = [
            "#История",
            "#" + re.sub(r"[^A-Za-zА-Яа-я0-9]", "", p["location"])[:30],
            "#" + re.sub(r"[^A-Za-zА-Яа-я0-9]", "", p["subject"].title())[:30],
        ]
        return "\n\n".join(parts) + "\n\n" + " ".join(tags)

    def fp(self, text):
        return self.norm(text)[:1600]

    def duplicate(self, text):
        current = self.fp(text)
        if not current:
            return True
        threshold = float(self.setting(
            "quality", "duplicate_threshold", 0.76
        ))
        return any(
            SequenceMatcher(None, current, old).ratio() >= threshold
            for old in self.db.recent_fp()
        )

    @staticmethod
    def visual_hash(path):
        with Image.open(path) as im:
            gray = im.convert("L").resize((16, 16))
            pixels = list(gray.getdata())
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if p >= avg else "0" for p in pixels)
        return f"{int(bits, 2):064x}"

    def _image_metrics(self, path):
        """Same deterministic image-quality signals used by the minerals pipeline."""
        with Image.open(path).convert("RGB") as im:
            w, h = im.size
            small = im.resize((192, 192))
            gray = small.convert("L")
            vals = list(gray.getdata())
            n = max(1, len(vals))
            hist = gray.histogram()
            import math
            entropy = -sum((c / n) * math.log2(c / n) for c in hist if c)
            mean = sum(vals) / n
            variance = sum((v - mean) ** 2 for v in vals) / n

            px = list(small.getdata())
            sat = [
                (max(rgb) - min(rgb)) / max(1, max(rgb))
                for rgb in px
            ]
            white = sum(
                1 for r, g, b in px
                if r > 245 and g > 245 and b > 245
            ) / n
            black = sum(
                1 for r, g, b in px
                if r < 10 and g < 10 and b < 10
            ) / n

            border = []
            for y in list(range(0, 16)) + list(range(176, 192)):
                border.extend(vals[y * 192:(y + 1) * 192])
            for y in range(16, 176):
                border.extend(vals[y * 192:y * 192 + 16])
                border.extend(vals[y * 192 + 176:y * 192 + 192])
            bmean = sum(border) / max(1, len(border))
            bvar = sum((v - bmean) ** 2 for v in border) / max(1, len(border))

            center = gray.crop((48, 48, 144, 144))
            cv = list(center.getdata())
            cmean = sum(cv) / max(1, len(cv))
            cvar = sum((v - cmean) ** 2 for v in cv) / max(1, len(cv))

            edge = sum(
                abs(vals[y * 192 + x] - vals[y * 192 + x - 1])
                + abs(vals[y * 192 + x] - vals[(y - 1) * 192 + x])
                for y in range(1, 192)
                for x in range(1, 192)
            ) / (2 * 191 * 191)

            return {
                "w": w, "h": h,
                "entropy": entropy,
                "variance": variance,
                "saturation": sum(sat) / n,
                "white": white,
                "black": black,
                "border_variance": bvar,
                "center_variance": cvar,
                "edge": edge,
            }

    def _image_visual_score(self, x):
        try:
            m = self._image_metrics(x["media_path"])
            short = min(m["w"], m["h"])
            ratio = max(m["w"], m["h"]) / max(1, short)
            score = 0.0

            if short >= 1800: score += 8
            elif short >= 1400: score += 6
            elif short >= 1100: score += 4
            elif short >= 1000: score += 2

            if ratio <= 1.35: score += 3
            elif ratio <= 1.65: score += 1
            elif ratio > float(self.setting("media", "max_aspect_ratio", 2.40)): score -= 6

            if 4.6 <= m["entropy"] <= 7.8: score += 4
            elif m["entropy"] < 1.5: score -= 6

            if 500 < m["variance"] < 4200: score += 2
            elif m["variance"] < 180: score -= 4

            if 0.10 <= m["saturation"] <= 0.72: score += 2
            elif m["saturation"] < 0.03: score -= 3

            if m["white"] > 0.62: score -= 7
            elif m["white"] > 0.42: score -= 3
            elif m["white"] < 0.20: score += 1

            if m["black"] > 0.55: score -= 3
            if m["border_variance"] < 90 and m["white"] > 0.35: score -= 4
            if m["center_variance"] > m["variance"] * 0.85: score += 1.5
            if m["edge"] >= 9: score += 3
            elif m["edge"] < 4: score -= 3
            return score
        except Exception as exc:
            log.info("visual score unavailable: %s", exc)
            return -20

    def _image_visual_reject(self, x):
        try:
            m = self._image_metrics(x["media_path"])
            short = min(m["w"], m["h"])
            ratio = max(m["w"], m["h"]) / max(1, short)
            min_width = max(300, int(self.setting("media", "download_min_width", 300)))
            max_aspect = float(self.setting("media", "download_max_aspect_ratio", 3.5))

            if short < min_width:
                return True, "low_resolution"
            if ratio > max_aspect + 0.35:
                return True, "extreme_aspect_ratio"
            if m["white"] > 0.82 and m["border_variance"] < 120:
                return True, "blank_catalogue_background"
            if m["white"] > 0.75 and m["center_variance"] < 120:
                return True, "washed_out_catalogue"
            if m["entropy"] < 1.5:
                return True, "too_flat_or_blank"
            if m["edge"] < 1.0:
                return True, "insufficient_detail"
            if m["black"] > 0.72 and m["entropy"] < 4.1:
                return True, "too_dark_and_flat"
            return False, ""
        except Exception:
            return True, "unreadable"

    def _history_primary_subject_score(self, p, x):
        """Strict event-specific relevance score for historical images."""
        title = self.norm(x.get("title", ""))
        desc = self.norm(x.get("description", ""))
        meta = self.norm(x.get("metadata", ""))

        # Filename/title is weak evidence.
        title_hay = title

        # Description + Wikimedia categories are the main semantic evidence.
        context_hay = " ".join((desc, meta)).strip()
        hay = " ".join((title_hay, context_hay)).strip()

        if not hay:
            return -50

        hard_bad = (
            "stock photo", "product", "auction", "advertisement", "logo",
            "screenshot", "catalog", "catalogue", "clipart", "illustration",
            "ai generated", "watermark", "jewelry", "jewellery",
            "book cover", "album cover", "poster", "diagram",
        )
        if any(t in hay for t in hard_bad):
            return -40

        title_words = {
            w for w in self.norm(p.get("title", "")).split()
            if len(w) >= 5
        }

        event_words = {
            w for w in self.norm(p.get("event", "")).split()
            if len(w) >= 5
        }

        generic = {
            "about", "after", "before", "during", "event", "events",
            "history", "historical", "people", "country", "countries",
            "state", "states", "world", "first", "second", "third",
            "war", "battle", "begins", "takes", "place", "officially",
            "soviet", "union", "united", "kingdom", "american",
            "heads", "toward", "sets", "motion", "crisis", "missile",
            "ship",
        }

        anchors = {
            w for w in event_words
            if w not in title_words
            and w not in generic
            and len(w) >= 5
        }

        context_words = set(context_hay.split())
        title_image_words = set(title_hay.split())

        context_anchor_hits = anchors & context_words
        title_anchor_hits = anchors & title_image_words
        title_hits = title_words & title_image_words
        context_title_hits = title_words & context_words

        # Weak geographic/general descriptors must not establish relevance
        # on their own for a specific historical event.
        weak_context_terms = {
            "panamanian", "american", "soviet", "british",
            "french", "german", "russian", "military",
            "leader", "former", "national", "general",
        }

        strong_context_anchor_hits = {
            w for w in context_anchor_hits
            if w not in weak_context_terms
        }

        event_signal_terms = {
            w for w in (
                "trial", "sentence", "drug", "trafficking",
                "laundering", "capture", "arrest", "custody",
                "invasion", "execution", "assassination",
                "election", "treaty", "agreement",
            )
            if w in event_words
        }

        event_signal_hits = event_signal_terms & context_words

        # Инициализируем score до любых штрафов.
        score = 0.0

        # For event-specific stories, require at least one concrete
        # event signal in the image description or categories.
        # Не отклоняем изображение только из-за отсутствия
        # явного event-signal: исторические фотографии часто
        # имеют неполные или нейтральные метаданные.
        if event_signal_terms and not event_signal_hits:
            score -= 8.0

        # If only weak geographic/general terms match, reject the image.
        if (
            anchors
            and not strong_context_anchor_hits
            and not event_signal_hits
        ):
            return 0.0

        # Concrete event anchors in the actual description/categories
        # are the strongest semantic signal.
        score += min(48, len(context_anchor_hits) * 12)

        # Filename alone is deliberately weak.
        score += min(6, len(title_anchor_hits) * 2)

        # Generic topic/title vocabulary is only supporting evidence.
        score += min(10, len(title_hits) * 2)

        # If the event title itself appears in Wikimedia description/categories,
        # that is useful but still weaker than a concrete object match.
        score += min(8, len(context_title_hits) * 2)

        # Strong contextual phrase matches.
        event_lower = self.norm(p.get("event", ""))

        important_phrases = [
            phrase for phrase in (
                "cuban missile crisis",
                "first sino japanese war",
                "franco prussian war",
                "american civil war",
                "world war i",
                "world war ii",
                "korean war",
                "battle of peleliu",
                "battle of dobro pole",
                "16th street baptist church",
                "parsons green",
            )
            if phrase in event_lower
        ]

        for phrase in important_phrases:
            if phrase in context_hay:
                score += 8

        # Year is supporting evidence, never the main relevance signal.
        try:
            year = int(p.get("year"))
            years = [
                int(y) for y in re.findall(
                    r"\b(1[0-9]{3}|20[0-9]{2})\b", hay
                )
            ]
            if year in years:
                score += 8
            elif any(abs(y - year) <= 10 for y in years):
                score += 2
        except Exception:
            pass

        if any(
            t in context_hay
            for t in ("photograph", "photo", "archive", "archival")
        ):
            score += 4

        # A concrete anchor should normally be present in semantic metadata.
        # For named historical persons/events, multiple title-word matches
        # can also provide sufficient evidence when metadata is incomplete.
        if anchors and not context_anchor_hits:
            named_title_hits = {
                w for w in title_anchor_hits
                if len(w) >= 5 and w not in weak_context_terms
            }
            if len(named_title_hits) < 2:
                return 0.0

        if not anchors and not title_hits and not context_title_hits:
            return 0.0

        return max(-50, min(90, score))

    def _ocr_reject_bytes(self, data):
        """Fail-closed OCR gate adapted from quote-bot."""
        try:
            import pytesseract
        except ImportError as exc:
            log.error("OCR dependencies unavailable: %s", exc)
            return True
        try:
            img = Image.open(BytesIO(data)).convert("RGB")
            scale = 2 if max(img.size) < 1800 else 1
            gray = ImageOps.grayscale(img)
            enlarged = gray.resize((gray.width * scale, gray.height * scale))
            enhanced = ImageEnhance.Contrast(enlarged).enhance(2.2)
            sharpened = enhanced.filter(ImageFilter.SHARPEN)
            hits = []
            for variant, cfg in ((img, "--oem 3 --psm 11"), (enhanced, "--oem 3 --psm 11"), (sharpened, "--oem 3 --psm 6")):
                info = pytesseract.image_to_data(variant, lang="eng+rus", config=cfg, output_type=pytesseract.Output.DICT)
                for value, conf in zip(info.get("text", []), info.get("conf", [])):
                    token = self.clean(value)
                    letters = re.sub(r"[^A-Za-zА-Яа-яЁё0-9]", "", token)
                    try: confidence = float(conf)
                    except Exception: confidence = -1
                    length = len(letters)
                    suspicious = (
    length >= 10 and confidence >= 65
    or length >= 7 and confidence >= 80
    or length >= 5 and confidence >= 90
    or length >= 4 and confidence >= 97
)
                    if suspicious: hits.append((token, round(confidence, 1)))
            if hits:
                log.warning("GENERATED IMAGE REJECTED BY OCR | hits=%s", json.dumps(hits[:12], ensure_ascii=False))
                return True
            return False
        except Exception as exc:
            log.error("OCR verification failed: %s", exc)
            return True

    def _gigachat_image(self, prompt):
        token = self._get_giga_token()
        base = os.getenv("GIGACHAT_API_BASE", "https://gigachat.devices.sberbank.ru/api/v1").rstrip("/")
        model = os.getenv("GIGACHAT_MODEL", "GigaChat")
        payload = {"model": model, "function_call": "auto", "messages": [
            {"role": "system", "content": "Generate one photorealistic historical illustration. Absolutely zero readable text: no letters, numbers, captions, signs, logos, watermarks, labels, book pages or typography."},
            {"role": "user", "content": prompt},
        ]}
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        with gigachat_gate():
            r = self.s.post(
                base + "/chat/completions",
                headers=headers,
                json=payload,
                timeout=int(os.getenv("GIGACHAT_IMAGE_TIMEOUT", "180")),
                verify=self._giga_verify_ssl,
            )
            if r.status_code in (401, 403):
                self._giga_token = ""
                token = self._get_giga_token()
                headers["Authorization"] = "Bearer " + token
                r = self.s.post(
                    base + "/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=180,
                    verify=self._giga_verify_ssl,
                )
        if r.status_code == 429:
            gigachat_mark_rate_limit()

        r.raise_for_status()
        response_json = r.json()
        message = response_json.get("choices", [{}])[0].get("message", {}) or {}
        content = message.get("content", "")

        if isinstance(content, list):
            content = " ".join(
                str(x.get("text", x)) if isinstance(x, dict) else str(x)
                for x in content
            )
        else:
            content = str(content or "")

        log.warning(
            "GIGACHAT IMAGE RESPONSE META | message_keys=%s | content_type=%s | content=%s",
            list(message.keys()),
            type(message.get("content")).__name__,
            content[:1500],
        )

        # GigaChat returns the image UUID inside <img src="..."/>.
        m = re.search(
            r'<img[^>]+src=["\\\']([a-f0-9-]{36})["\\\']',
            content,
            re.I
        )

        # Do not search UUIDs in the complete message:
        # functions_state_id is not an image file ID.

        if not m:
            if "censored=\"true\"" in content or "censored='true'" in content:
                raise RuntimeError(
                    "GigaChat image generation was censored: no image file returned"
                )
            log.error(
                "GIGACHAT IMAGE RESPONSE WITHOUT FILE ID | keys=%s | content=%s",
                list(message.keys()),
                content[:1000]
            )
            raise RuntimeError("GigaChat image response has no file id")

        file_id = m.group(1) if m.lastindex else m.group(0)
        # The generated file may become available with a short delay.
        ir = None
        for download_attempt in range(1, 6):
            ir = self.s.get(
                base + "/files/" + file_id + "/content",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "image/jpeg, image/png, application/octet-stream",
                },
                timeout=180,
                verify=self._giga_verify_ssl,
            )
            if ir.status_code != 404:
                break
            log.warning(
                "GIGACHAT IMAGE FILE NOT READY | file_id=%s | attempt=%s/5 | body=%s",
                file_id,
                download_attempt,
                ir.text[:500],
            )
            if download_attempt < 5:
                time.sleep(5)

        ir.raise_for_status()
        data = ir.content
        with Image.open(BytesIO(data)) as im: im.verify()
        return data

    def _generate_platform_image(self, p, platform, attempt_hint="", article_text=""):
        orientation = (
            "vertical portrait 9:16, subject safely inside the central area"
            if platform == "shorts"
            else "horizontal landscape 16:9, strong editorial composition"
        )

        event = str(p.get("event", "")).strip()
        title = str(p.get("title", "")).strip()
        facts = "; ".join(
            [event] + [str(x) for x in (p.get("facts") or [])]
        ).strip()

        article_text = str(article_text or "").strip()
        if not article_text:
            raise RuntimeError(
                "Нельзя создать изображение: отсутствует полный текст статьи"
            )

        max_attempts = int(self.setting("media", "generation_attempts", 3))
        last = None

        for attempt in range(1, max_attempts + 1):
            try:
                if attempt == 1:
                    visual_request = f"""VISUAL_PROMPT_CONTEXT:

Ты редактор визуального исторического контента.

На основе ПОЛНОГО ТЕКСТА СТАТЬИ создай один подробный промпт
для генерации исторической образовательной иллюстрации в GigaChat.

ТЕКСТ СТАТЬИ:
{article_text}

ИСТОРИЧЕСКОЕ СОБЫТИЕ:
{title}

ФАКТЫ:
{facts}

ТРЕБОВАНИЯ:
- Изображение должно непосредственно отражать содержание статьи.
- Выбери один главный исторический эпизод.
- Не добавляй вымышленных событий или персонажей.
- Соблюдай историческую эпоху.
- Формат: {orientation}.
- Один понятный главный сюжетный центр.
- Без текста, букв, цифр, надписей, плакатов, газет, книг, вывесок,
  логотипов и водяных знаков.
- Не используй чрезмерно кровавые или шокирующие детали.
- Верни только готовый промпт на русском языке.
"""
                    prompt = self.gigachat(visual_request).strip()

                elif attempt == 2:
                    prompt = (
                        f"Историческая образовательная иллюстрация, {orientation}. "
                        f"Сцена непосредственно связана с событием: {title}. "
                        f"Показать только один главный исторический эпизод. "
                        f"Исторически правдоподобные люди, одежда, архитектура и предметы "
                        f"соответствующей эпохи. "
                        f"Нейтральная документальная атмосфера, реалистичное освещение, "
                        f"фотореалистичная историческая реконструкция. "
                        f"Без насилия крупным планом, без крови и шокирующих деталей. "
                        f"Без текста, букв, цифр, надписей, плакатов, газет, книг, "
                        f"логотипов и водяных знаков."
                    )

                else:
                    prompt = (
                        f"Фотореалистичная историческая реконструкция, {orientation}. "
                        f"Один простой сюжет по событию: {title}. "
                        f"Одна сцена, небольшое количество персонажей, "
                        f"исторически достоверная эпоха и окружение. "
                        f"Спокойная документальная композиция. "
                        f"Без текста, букв, цифр, надписей, логотипов, "
                        f"водяных знаков, крови и шокирующих деталей."
                    )

                if not prompt:
                    raise RuntimeError(
                        "Пустой промпт изображения"
                    )

                log.info(
                    "GIGACHAT VISUAL PROMPT READY | platform=%s | attempt=%s/%s",
                    platform, attempt, max_attempts
                )

                data = self._gigachat_image(prompt)

                if self._ocr_reject_bytes(data):
                    raise RuntimeError("OCR обнаружил возможный текст")

                with Image.open(BytesIO(data)) as im:
                    if im.width < 512 or im.height < 512:
                        raise RuntimeError(f"low_resolution={im.size}")

                path = MEDIA_DIR / (
                    uuid.uuid4().hex
                    + ("_shorts.jpg" if platform == "shorts" else "_post.jpg")
                )
                path.write_bytes(data)
                return str(path)

            except Exception as exc:
                last = exc
                log.warning(
                    "GIGACHAT IMAGE RETRY | platform=%s | attempt=%s/%s | %s",
                    platform, attempt, max_attempts, exc
                )

                if attempt < max_attempts:
                    is_rate_limit = (
                        "429" in str(exc)
                        or "Too Many Requests" in str(exc)
                    )
                    delay = min(180, attempt * 60) if is_rate_limit else 3

                    log.warning(
                        "GIGACHAT IMAGE RETRY WAIT | platform=%s | seconds=%s",
                        platform, delay
                    )
                    time.sleep(delay)

        raise RuntimeError(
            f"Не удалось сгенерировать изображение для {platform}: {last}"
        )

    def download_media(self, x):
        url = x["image_url"]
        ext = Path(url.split("?")[0]).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        path = MEDIA_DIR / (uuid.uuid4().hex + ext)

        try:
            with self.s.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "").lower()
                if not ctype.startswith("image/"):
                    raise RuntimeError("Источник вернул не изображение")

                h = hashlib.sha256()
                size = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_content(262144):
                        if not chunk:
                            continue
                        f.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
                        if size > 50 * 1024 * 1024:
                            raise RuntimeError("Изображение слишком большое")

            if size < 15 * 1024:
                raise RuntimeError("Изображение слишком маленькое")

            with Image.open(path) as im:
                im.verify()

            with Image.open(path) as im:
                w, hgt = im.size
                fmt = (im.format or "").upper()
                if fmt not in ("JPEG", "PNG", "WEBP"):
                    raise RuntimeError(f"Неподдерживаемый формат {fmt}")
                if w < int(self.setting("media", "download_min_width", 300)) or hgt < int(self.setting("media", "download_min_height", 200)):
                    raise RuntimeError(
                        f"Недостаточное разрешение {w}x{hgt}"
                    )
                ratio = w / max(hgt, 1)
                min_ratio = float(self.setting("media", "download_min_aspect_ratio", 0.35))
                max_ratio = float(self.setting("media", "download_max_aspect_ratio", 3.5))
                if ratio < min_ratio or ratio > max_ratio:
                    raise RuntimeError(f"Неподходящее соотношение сторон {ratio:.2f}")

            x["image_hash"] = h.hexdigest()
            if self.db.media_hash_used(x["image_hash"]):
                raise RuntimeError("Байт-в-байт повтор изображения")

            x["visual_hash"] = self.visual_hash(path)
            if self.db.similar_visual_used(
                x["visual_hash"],
                int(self.setting("media", "visual_duplicate_distance", 6)),
            ):
                raise RuntimeError("Слишком похожее ранее использованное изображение")

            x["media_path"] = str(path)
            return x
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def final_topic(self, p, x):
        if p.get("event"):
            return p.get("title", p.get("topic", "История"))[:240], p["topic_key"]
        identity = self.norm(x.get("source_url") or x.get("title") or x.get("image_url"))
        title = self.clean(x.get("title", "архивный сюжет"))
        topic = f"{p['subject'].capitalize()} в {p['location']} в {p['period']}: {title}"
        key = hashlib.sha256((self.norm(p["location"])+"|"+self.norm(p["period"])+"|"+self.norm(p["subject"])+"|"+identity).encode("utf-8")).hexdigest()
        return topic[:240], key

    def story_quality_score(self, text, p):
        """Transparent heuristic: quality checks assist the editor, they do not replace facts."""
        text = str(text or "").strip()
        if not text:
            return 0
        score = 40
        length = len(text)
        ideal_lo = int(self.setting("content", "ideal_min_chars", 800))
        ideal_hi = int(self.setting("content", "ideal_max_chars", 1150))
        if ideal_lo <= length <= ideal_hi:
            score += 15
        elif length >= 650:
            score += 8
        paragraphs = [x for x in text.split("\n\n") if x.strip()]
        if 4 <= len(paragraphs) <= 7:
            score += 10
        if any(ch in text for ch in ("—", ":", "…")):
            score += 3
        title_words = [w for w in self.norm(p.get("title", "")).split() if len(w) > 4]
        post_words_all = set(self.norm(text).split())
        if title_words and any(w in post_words_all for w in title_words):
            score += 5
        generic = ("история становится живой", "направление эпохи меняется", "вопрос без окончательного ответа")
        if any(g in text.lower() for g in generic):
            score -= 8
        # Story should reflect at least some verified event vocabulary.
        fact_words = set(self.norm(" ".join([p.get("event", "")] + list(p.get("facts") or []))).split())
        post_words = set(self.norm(text).split())
        if fact_words:
            overlap = len([w for w in fact_words if len(w) > 4 and w in post_words])
            score += min(18, overlap * 2)
        tags = re.findall(r"#[A-Za-zА-Яа-яЁё0-9_]+", text)
        if 2 <= len(tags) <= 4:
            score += 7
        return min(100, score)

    def _fit_text_to_limit(self, text, limit=850):
        """Гарантированно укладывает текст в лимит, не обрывая предложение."""
        text = str(text or "").strip()

        if len(text) <= limit:
            return text

        cut = text[:limit]

        # Ищем последнюю границу предложения.
        positions = []
        for char in ".!?":
            pos = cut.rfind(char)
            if pos >= 0:
                positions.append(pos)

        if positions:
            pos = max(positions) + 1
            candidate = cut[:pos].strip()

            if len(candidate) >= 650:
                return candidate

        # Если подходящего конца предложения нет 
        # режем по последнему пробелу.
        pos = cut.rfind(" ")

        if pos >= 650:
            return cut[:pos].rstrip(" ,;:-")

        return cut[:limit].rstrip(" ,;:-")

    def validate_text(self, text, x, p=None, max_chars=None):
        text = str(text or "").strip().replace(" .", ".")
        lo = int(self.setting("content", "min_chars", 700))
        hi = int(max_chars if max_chars is not None else self.setting("content", "max_chars", 1300))
        if not text:
            return False, "Пустой текст"
        if len(text) > hi:
            return False, f"Текст слишком длинный: {len(text)}"
        # The emergency fallback is intentionally allowed to be shorter:
        # publishing availability is better than losing the scheduled post.
        allow_youtube_short = os.getenv("HISTORY_ECHO_ALLOW_SHORT", "0").strip() == "1"
        if (
            len(text) < lo
            and x.get("source_name") != "text-only fallback"
            and not allow_youtube_short
        ):
            return False, f"Текст слишком короткий: {len(text)}"
        if self.duplicate(text):
            return False, "Повтор похожего поста"
        if p and p.get("event"):
            quality = self.story_quality_score(text, p)
            minimum = int(self.setting("quality", "min_story_score", 72))
            if quality < minimum:
                return False, f"Недостаточное качество поста: {quality}/{minimum}"
        return True, ""

    def _generate_single_platform_draft(self, platform, max_chars=None):
        """Generate one draft for exactly one platform."""
        batch_date = datetime.now(TZ).date().isoformat()
        platform_key = "youtube" if platform == "shorts" else "tgmax"

        p = self.db.next_daily_event_for_platform(
            batch_date,
            platform_key,
        )

        if p:
            p = self._daily_event_payload(p)
        if not p:
            raise RuntimeError("На сегодня больше нет готовых исторических событий")

        best = {
            "source_name": "GigaChat generated",
            "image_url": "gigachat://generated",
            "daily_event_id": p["daily_event_id"],
            "topic_key": p["topic_key"],
            "topic": p["title"],
            "platform": platform,
        }

        try:
            # Сначала создаём и проверяем полный текст статьи.
            raw = self.gigachat(self.context(p, best))
            ok, reason = self.validate_text(raw, best, p, max_chars=max_chars)

            if not ok:
                raw = self.gigachat(
                    self.context(p, best)
                    + "\nПЕРЕПИШИ ТЕКСТ СТРОГО В ДИАПАЗОНЕ 650-850 СИМВОЛОВ. "
                      "Максимум 850 символов. Не превышай лимит ни при каких обстоятельствах. "
                      "Сохрани только самые важные проверяемые факты, причины, последствия и исторический контекст. "
                      "Не добавляй вымышленные факты."
                )
                ok, reason = self.validate_text(raw, best, p, max_chars=max_chars)

            # GigaChat иногда игнорирует ограничение длины.
            # В этом случае просим его сделать финальную компактную версию.
            if not ok and "слишком длинный" in reason.lower():
                raw = self.gigachat(
                    raw
                    + "\n\nСОКРАТИ ЭТОТ ТЕКСТ ДО 800 СИМВОЛОВ. "
                      "ЖЁСТКИЙ МАКСИМУМ  850 СИМВОЛОВ. "
                      "Сохрани фактическое содержание и читаемость. "
                      "Не добавляй новые факты, заголовки и пояснения."
                )
                ok, reason = self.validate_text(raw, best, p, max_chars=max_chars)

            if not ok:
                raise RuntimeError(reason)

            # Только после готовности статьи генерируем изображение
            # исключительно через GigaChat, по полному тексту статьи.
            if platform == "shorts":
                best["shorts_media_path"] = self._generate_platform_image(
                    p, "shorts", article_text=raw
                )
                best["media_path"] = best["shorts_media_path"]
            else:
                best["media_path"] = self._generate_platform_image(
                    p, "post", article_text=raw
                )

            log.info(
                "DRAFT READY | platform=%s | event_id=%s | %s",
                platform, p["daily_event_id"], p["title"]
            )
            return p["title"], best, raw

        except Exception as exc:
            self.db.set_event_platform_status(
                p["daily_event_id"],
                platform_key,
                "ready",
                f"{platform} draft generation: {exc}"
            )

            for key in ("media_path", "shorts_media_path"):
                path = best.get(key)
                if path:
                    Path(path).unlink(missing_ok=True)

            raise

    def generate_tgmax_draft(self, max_chars=None):
        """Generate only Telegram/MAX draft."""
        return self._generate_single_platform_draft("post", max_chars)

    def generate_youtube_draft(self, max_chars=None):
        """Generate only YouTube Shorts draft."""
        return self._generate_single_platform_draft("shorts", max_chars)

    def generate_draft(self, max_chars=None):
        """Backward-compatible alias for Telegram/MAX generation."""
        return self.generate_tgmax_draft(max_chars)

