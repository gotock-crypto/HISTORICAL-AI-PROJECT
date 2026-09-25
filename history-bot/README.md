# Historical AI Project

## AI-powered historical content automation pipeline

Production-oriented Telegram/MAX publishing system that turns structured historical events into a daily, stateful content queue, validates visual material, generates editorial text with an LLM, and publishes each prepared story independently to configured platforms.

The project is built around a clear separation of responsibilities:

**event selection → ranking → deduplication → daily queue → image discovery → image validation → LLM writing → platform publishing → persistent state**

---

## What the system does

Every day the system prepares a queue of historical events for the current calendar date.

### Daily preparation

```text
Wikimedia On This Day
        ↓
normalization
        ↓
filtering / ranking
        ↓
SQLite global deduplication
        ↓
daily_events
        ↓
metadata-only image discovery
        ↓
multiple candidate URLs per event
        ↓
READY queue
```

The daily queue is prepared once and then consumed during the day. This separates expensive preparation from the actual publishing cycle.

### Publication cycle

```text
next READY event
      ↓
candidate image #1..N
      ↓
temporary download
      ↓
MIME / PIL / relevance / visual dedup validation
      ↓
best valid image
      ↓
GigaChat generates the post
      ↓
Telegram publication ─────┐
                          ├─→ successful platforms → POSTED
MAX publication ──────────┘
      ↓
temporary media removed
```

The system can also be triggered manually from the admin interface.

---

## Key engineering decisions

### 1. Event selection is separated from LLM generation

GigaChat does **not** search for historical topics.

The system first selects and ranks a concrete historical event and stores it in `daily_events`. Only after the event and visual material have been selected does the LLM generate the editorial text.

This keeps content selection deterministic and makes the LLM responsible for the part where it provides the most value: editorial generation.

### 2. Stateful daily queue

Each event moves through an explicit state machine:

```text
READY
  ↓
PROCESSING
  ↓
PUBLISHING
  ↓
POSTED

or

READY → SKIPPED
```

Persistent state is stored in SQLite, so the queue can recover after a process restart instead of relying on in-memory execution state.

### 3. Image discovery without permanent media storage

The system stores image URLs and metadata rather than maintaining a permanent image archive.

Before publication:

- candidate URL is selected;
- image is downloaded temporarily;
- MIME/PIL checks are performed;
- dimensions and aspect ratio are validated;
- relevance is evaluated;
- visual duplicates are rejected;
- the selected file is used for publication;
- the temporary file is deleted.

### 4. Independent platform publishing

Telegram and MAX are handled independently.

An event becomes `POSTED` only after all configured publication targets succeed. A failure on one platform therefore does not get silently represented as a fully completed event.

---

## Architecture

### Core components

| Component | Responsibility |
|---|---|
| `content/daily.py` | Current-date historical event source and normalization |
| `content/planner.py` | Event scoring, ranking and deterministic planning |
| `content/events.py` | Curated historical event metadata and editorial attributes |
| `db.py` | SQLite persistence, deduplication and queue state |
| `core.py` | Main preparation, media selection and publishing workflow |
| `admin.py` | Telegram admin controls and scheduled execution |
| `publishers.py` | Telegram publishing |
| `max/publisher.py` | MAX publishing |
| `youtube_echo.py` | Historical short-video generation/publishing contour |
| `youtube_echo_cli.py` | CLI entry point for the YouTube contour |
| `prompts/` | LLM editorial and visual-prompt templates |

---

## Data model

The project preserves the existing publishing model and adds a dedicated daily queue.

Main daily entities:

- `daily_batches` — one preparation batch for a calendar date;
- `daily_events` — selected historical events and their processing state;
- `daily_media` — candidate image URLs and metadata.

The existing `posts`, `publishes`, `topics` and `media_memory` structures remain part of the system.

The queue also uses persistent identifiers such as `daily_event_id` and `topic_key` to connect event selection, deduplication and publication history.

---

## Content pipeline

### Historical events

The system uses Wikimedia's structured **On This Day** source together with the project's event planning and ranking logic.

Events are normalized, scored and checked against publication history before entering the daily queue.

### Visual pipeline

Candidate media can be discovered from external sources such as Wikimedia Commons and Internet Archive.

The pipeline is intentionally metadata-first:

```text
search metadata
      ↓
candidate URLs
      ↓
download only when needed
      ↓
technical validation
      ↓
relevance validation
      ↓
visual duplicate validation
      ↓
selected media
```

This reduces unnecessary permanent storage and keeps media handling inside the publication workflow.

### Editorial generation

GigaChat receives the already-selected historical event and selected visual context.

It is used for generating the final editorial text rather than for discovering the underlying historical topic.

---

## Reliability and operational design

The project includes production-oriented mechanisms such as:

- SQLite persistent state;
- idempotent daily preparation;
- publication state machine;
- duplicate prevention;
- retry/fallback-oriented media handling;
- candidate image fallback;
- logging and runtime diagnostics;
- restart-safe queue state;
- admin controls;
- scheduled preparation and publishing;
- independent platform publishing;
- temporary media cleanup.

The system is designed around the assumption that external APIs, media URLs and publishing platforms can fail independently.

---

## YouTube / History Echo

The repository also contains a separate short-video contour for historical content.

The implementation includes:

- Edge TTS voice generation;
- FFmpeg/ffprobe media processing;
- vertical 1080×1920 video format;
- background music;
- voice/music mixing and ducking;
- MP4 validation;
- YouTube API/OAuth integration.

This contour is separated from the main Telegram/MAX publishing pipeline.

Production credentials and OAuth tokens are intentionally excluded from the repository.

---

## Configuration

Runtime configuration is kept separate from secrets.

Public repository files include:

- `config.yaml` — non-secret application parameters;
- `.env.example` — environment variable template;
- `requirements.txt` — Python dependencies.

Production secrets belong in `.env` and local credential/session files and are excluded by `.gitignore`.

---

## Installation

Example server workflow:

```bash
git clone https://github.com/gotock-crypto/HISTORICAL-AI-PROJECT.git
cd HISTORICAL-AI-PROJECT/history-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Configure the environment using `.env.example`.

For an existing production installation, preserve the production database, environment variables and runtime session data when updating the source.

---

## Verification

Basic checks:

```bash
python -m compileall .
python self_test.py
```

For a systemd deployment:

```bash
systemctl status history-bot.service
journalctl -u history-bot.service -n 200 --no-pager
```

---

## Repository structure

```text
history-bot/
├── main.py
├── core.py
├── config.yaml
├── requirements.txt
├── self_test.py
├── systemd/
├── history_bot/
│   ├── admin.py
│   ├── core.py
│   ├── db.py
│   ├── publishers.py
│   ├── youtube_echo.py
│   ├── youtube_echo_cli.py
│   ├── content/
│   │   ├── daily.py
│   │   ├── editorial.py
│   │   ├── events.py
│   │   └── planner.py
│   ├── max/
│   │   └── publisher.py
│   └── prompts/
└── .env.example
```

---

## Tech stack

**Python · SQLite · Telegram Bot API · MAX API · GigaChat · Wikimedia · Internet Archive · Pillow · OCR/visual validation · FFmpeg · Edge TTS · YouTube API · systemd · Linux**

---

## Security

The public repository intentionally excludes:

- production `.env`;
- Telegram bot tokens;
- GigaChat credentials;
- MAX sessions;
- YouTube OAuth tokens;
- YouTube client secrets;
- production SQLite databases;
- runtime state;
- virtual environments;
- backup files.

Use `.env.example` as the configuration template.

---

## Project focus

This project demonstrates practical work at the intersection of:

- AI automation;
- LLM integration;
- API orchestration;
- content pipelines;
- workflow/state-machine design;
- data deduplication;
- media validation;
- scheduled publishing;
- external-service fault handling;
- production-oriented Linux deployment.

It is not a single LLM script. The core engineering problem is coordinating multiple external services and persistent application state into a repeatable autonomous workflow.
