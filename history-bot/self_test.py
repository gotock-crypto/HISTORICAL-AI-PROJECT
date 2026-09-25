"""Offline structural test for History Bot V3."""
import hashlib
import os
from pathlib import Path
from history_bot.content.events import EVENTS
from history_bot.content.planner import ContentPlanner
from history_bot.db import DB
from history_bot.core import HistoryEngine

ROOT = Path(__file__).resolve().parent

required = {"id", "type", "title", "event", "facts", "angle", "importance", "drama", "visual"}
errors = []
seen = set()
for e in EVENTS:
    missing = required - set(e)
    if missing:
        errors.append(f"{e.get('id')}: missing {sorted(missing)}")
    if e.get("id") in seen:
        errors.append(f"duplicate id: {e.get('id')}")
    seen.add(e.get("id"))
    if not isinstance(e.get("facts"), list) or not e.get("facts"):
        errors.append(f"{e.get('id')}: empty facts")
    if not isinstance(e.get("visual"), list) or not e.get("visual"):
        errors.append(f"{e.get('id')}: empty visual queries")

# Ensure a planner can return an unused item without a real production DB.
tmp = ROOT / "runtime" / "selftest.db"
tmp.parent.mkdir(exist_ok=True)
try:
    db = DB(tmp)
    planner = ContentPlanner(db)
    item = planner.next_story()
    if not item:
        errors.append("planner returned no story")
    else:
        if not item.get("topic_key") or item.get("topic_key") != hashlib.sha256(item["id"].encode()).hexdigest():
            errors.append("planner produced invalid topic key")
finally:
    for suffix in ("", "-wal", "-shm"):
        (Path(str(tmp) + suffix)).unlink(missing_ok=True)

# Prompt and core imports must exist.
for name in ("story_writer.txt", "archive_writer.txt"):
    if not (ROOT / "history_bot" / "prompts" / name).exists():
        errors.append(f"missing prompt: {name}")

# V4 regression checks.
planner_a = ContentPlanner(DB(ROOT / 'runtime' / 'selftest_a.db'))
planner_b = ContentPlanner(DB(ROOT / 'runtime' / 'selftest_b.db'))
try:
    import datetime as _dt
    day = _dt.date(2026, 9, 14)
    a = planner_a.candidates(day)
    b = planner_b.candidates(day)
    if [x['id'] for x in a[:10]] != [x['id'] for x in b[:10]]:
        errors.append('planner ranking is not deterministic')
finally:
    for name in ('selftest_a.db', 'selftest_b.db'):
        for suffix in ('', '-wal', '-shm'):
            (ROOT / 'runtime' / (name + suffix)).unlink(missing_ok=True)

if os.getenv('REQUESTS_VERIFY_SSL') is None:
    # Default must remain secure in core.py; this checks source rather than network.
    core_source = (ROOT / 'history_bot' / 'core.py').read_text(encoding='utf-8')
    if '"REQUESTS_VERIFY_SSL", "1"' not in core_source:
        errors.append('insecure SSL default')


if errors:
    print("SELF-TEST FAILED")
    for e in errors:
        print(" -", e)
    raise SystemExit(1)
print(f"SELF-TEST OK | events={len(EVENTS)}")

# V5 reliability regressions
core_text = (ROOT / "history_bot" / "core.py").read_text(encoding="utf-8")
admin_text = (ROOT / "history_bot" / "admin.py").read_text(encoding="utf-8")
assert "text-only fallback" not in core_text[core_text.index("def generate_draft"):], "V5 must not silently create text-only posts"
assert "latest_unfinished" in admin_text, "unfinished publish retry missing"
assert "AUTO PUBLISH ATTEMPT FAILED" in admin_text, "generation retry missing"
print("V5 RELIABILITY TEST OK | media_required=1 | retry_pending=1")

# Daily queue architecture checks.
with_db = ROOT / 'runtime' / 'selftest_daily.db'
try:
    db = DB(with_db)
    for table in ('daily_batches', 'daily_events', 'daily_media'):
        if not db.c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            errors.append(f'missing daily table: {table}')
    import history_bot.core as _core
    source = (ROOT / 'history_bot' / 'core.py').read_text(encoding='utf-8')
    if 'GigaChat EVENT RAW RESPONSE' in source:
        # Legacy function text may remain only in old archives; current pipeline must not call it.
        gen = source[source.index('def generate_draft'):]
        if 'generate_date_event' in gen or 'plan_topic' in gen:
            errors.append('generate_draft still depends on LLM topic discovery')
    daily_source = (ROOT / 'history_bot' / 'content' / 'daily.py').read_text(encoding='utf-8')
    if 'onthisday/events' not in daily_source:
        errors.append('Wikimedia On This Day source missing')
finally:
    for suffix in ('', '-wal', '-shm'):
        (Path(str(with_db) + suffix)).unlink(missing_ok=True)

if errors:
    print('DAILY SELF-TEST FAILED')
    for e in errors:
        print(' -', e)
    raise SystemExit(1)
print('DAILY ARCHITECTURE TEST OK | persistent_queue=1 | llm_topic_discovery=0')
