import datetime as dt
import hashlib
import random
from collections import defaultdict
from .events import EVENTS


class ContentPlanner:
    """Chooses a story deliberately instead of generating a random archive topic."""
    TYPE_PRIORITY = {
        "event_today": 12, "great_battle": 10, "catastrophe": 9,
        "assassination": 9, "last_day": 9, "breakthrough": 8,
        "mystery": 7, "person_story": 6, "archive": 4,
    }

    def __init__(self, db):
        self.db = db

    @staticmethod
    def key(event):
        return hashlib.sha256(event["id"].encode("utf-8")).hexdigest()

    def _score(self, event, today):
        score = event.get("importance", 5) * 10
        score += event.get("drama", 5) * 7
        score += self.TYPE_PRIORITY.get(event.get("type"), 5) * 2
        if event.get("month") == today.month and event.get("day") == today.day:
            score += 120
        if self.db.topic_used(self.key(event)):
            score -= 100000
        # Deterministic score: ranking must not change merely because it is recalculated.
        return score

    def candidates(self, today=None):
        today = today or dt.date.today()
        rows = [e for e in EVENTS if not self.db.topic_used(self.key(e))]
        return sorted(rows, key=lambda e: self._score(e, today), reverse=True)

    def next_story(self, today=None):
        today = today or dt.date.today()
        rows = self.candidates(today)
        if not rows:
            return None
        # Scores are deterministic. Randomness is used only as a stable tie-breaker
        # inside the genuinely comparable top tier.
        top_score = self._score(rows[0], today)
        tier = [e for e in rows if self._score(e, today) >= top_score - 5]
        seed = f"{today.isoformat()}|{len(rows)}"
        rng = random.Random(seed)
        event = dict(rng.choice(tier[:min(5, len(tier))]))
        event["topic_key"] = self.key(event)
        event["topic"] = event["title"]
        event["date_label"] = (
            f"{today.day:02d}.{today.month:02d}"
            if event.get("month") == today.month and event.get("day") == today.day
            else "История"
        )
        return event
