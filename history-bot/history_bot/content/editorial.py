"""Editorial helpers for ranking stories and keeping channel output varied."""
from __future__ import annotations

TYPE_LABELS = {
    "event_today": "Событие дня",
    "great_battle": "Битва",
    "catastrophe": "Катастрофа",
    "assassination": "Покушение",
    "breakthrough": "Прорыв",
    "mystery": "Загадка",
    "person_story": "Человек",
    "last_day": "Последний день",
    "archive": "Архив",
}

def editorial_label(event: dict) -> str:
    return TYPE_LABELS.get(event.get("type"), "История")

def story_summary(event: dict) -> str:
    facts = event.get("facts") or []
    return " ".join([event.get("event", ""), *facts[:2]]).strip()
