# Historical AI Project

## AI Content Automation & Publishing Pipeline

A production-oriented AI automation system that turns structured historical events into a daily publishing queue, validates visual material, generates editorial content with an LLM, and publishes prepared stories to Telegram and MAX.

The project combines **AI integration, API orchestration, workflow automation, persistent state, media validation, deduplication and scheduled publishing** into one autonomous content pipeline.

---

## What the system does

The pipeline separates content preparation from publication:

```text
Historical event sources
        ↓
Filtering / ranking
        ↓
SQLite deduplication
        ↓
Daily event queue
        ↓
Image discovery
        ↓
Image validation
        ↓
LLM editorial generation
        ↓
Telegram + MAX publishing
        ↓
Persistent publication state
```

A daily batch is prepared for the current calendar date. During the day, the system processes the prepared queue sequentially rather than searching for a new topic for every publication.

---

## Core pipeline

### 1. Historical event preparation

The system uses structured historical-event data, including Wikimedia On This Day, then:

- normalizes events;
- filters and ranks candidates;
- checks publication history;
- prevents duplicate topics;
- creates a persistent daily queue.

### 2. Visual media pipeline

For each event, the system discovers candidate image URLs before publication.

The selected image goes through validation including:

- MIME / file validation;
- Pillow-based image checks;
- dimensions and aspect ratio;
- relevance checks;
- visual duplicate detection.

Images are downloaded temporarily when needed and removed after publication.

### 3. LLM generation

GigaChat receives an already selected historical event and visual context.

The LLM is responsible for editorial generation.

It does **not** perform the initial historical-topic search. Topic selection happens earlier in the deterministic pipeline.

### 4. Multi-platform publishing

The generated post is published independently to configured platforms:

- Telegram;
- MAX.

The event reaches the final `posted` state only after all configured publication targets succeed.

---

## Stateful workflow

The project uses a persistent state machine:

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

State is stored in SQLite rather than relying only on in-memory execution.

This allows the queue and publication workflow to survive process restarts.

---

## Architecture

```text
                     ┌─────────────────────┐
                     │ Historical sources  │
                     │ Wikimedia / catalog │
                     └──────────┬──────────┘
                                ↓
                     ┌─────────────────────┐
                     │ Event planning      │
                     │ filtering / ranking │
                     └──────────┬──────────┘
                                ↓
                     ┌─────────────────────┐
                     │ SQLite state        │
                     │ dedup / daily queue │
                     └──────────┬──────────┘
                                ↓
                     ┌─────────────────────┐
                     │ Media discovery     │
                     │ candidate URLs      │
                     └──────────┬──────────┘
                                ↓
                     ┌─────────────────────┐
                     │ Media validation    │
                     │ relevance / dedup   │
                     └──────────┬──────────┘
                                ↓
                     ┌─────────────────────┐
                     │ GigaChat            │
                     │ editorial generation│
                     └──────────┬──────────┘
                                ↓
                  ┌─────────────┴─────────────┐
                  ↓                           ↓
          ┌──────────────┐            ┌──────────────┐
          │ Telegram     │            │ MAX          │
          └──────────────┘            └──────────────┘
```

---

## Main engineering components

| Component | Responsibility |
|---|---|
| Historical event source | Current-date event collection |
| Planner | Filtering, ranking and deterministic planning |
| SQLite | Persistent state and deduplication |
| Daily queue | Sequential event processing |
| Media discovery | Candidate image search |
| Media validation | Technical, relevance and visual checks |
| GigaChat | Editorial text generation |
| Telegram publisher | Telegram publication |
| MAX publisher | MAX publication |
| Admin layer | Manual controls and scheduled execution |
| YouTube contour | Historical short-video generation |

---

## YouTube / History Echo

The repository also contains a separate short-video contour for historical content.

It includes:

- Edge TTS;
- FFmpeg / ffprobe;
- 1080×1920 vertical video;
- background music;
- voice/music mixing and ducking;
- MP4 validation;
- YouTube API / OAuth integration.

The YouTube contour is separated from the primary Telegram/MAX publication workflow.

Production credentials and OAuth tokens are not included in the repository.

---

## Reliability

The architecture is designed for long-running automation and external-service failures.

Relevant mechanisms include:

- persistent SQLite state;
- daily batch idempotency;
- duplicate prevention;
- explicit processing states;
- candidate-image fallback;
- media validation;
- retry-oriented external-service handling;
- restart-safe queue state;
- logging and diagnostics;
- scheduled execution;
- independent platform publishing;
- temporary media cleanup.

The goal is not simply to call an LLM API, but to coordinate several external services into a repeatable autonomous workflow.

---

## Technology

**Python · SQLite · GigaChat · Telegram Bot API · MAX API · Wikimedia · Internet Archive · Pillow · OCR / visual validation · FFmpeg · Edge TTS · YouTube API · Linux · systemd**

---

## Repository structure

```text
HISTORICAL-AI-PROJECT/
├── README.md
├── LICENSE
└── history-bot/
    ├── README.md
    ├── main.py
    ├── core.py
    ├── config.yaml
    ├── requirements.txt
    ├── self_test.py
    ├── systemd/
    └── history_bot/
        ├── admin.py
        ├── core.py
        ├── db.py
        ├── publishers.py
        ├── youtube_echo.py
        ├── youtube_echo_cli.py
        ├── content/
        ├── max/
        └── prompts/
```

---

## Security

The public repository excludes production secrets and runtime state, including:

- `.env`;
- Telegram bot tokens;
- GigaChat credentials;
- MAX sessions;
- YouTube OAuth tokens;
- client secrets;
- production databases;
- virtual environments;
- runtime state;
- backup files.

Use `.env.example` as the configuration template.

---

## Project focus

This project demonstrates practical work with:

- AI automation;
- LLM integration;
- API orchestration;
- content automation;
- workflow and state-machine design;
- data deduplication;
- media validation;
- scheduled publishing;
- external API fault handling;
- Linux deployment and operational support.

The central engineering task is coordinating **data sources, LLM services, media processing, persistent state and multiple publishing platforms** into a repeatable automated workflow.

---

## Technical documentation

Detailed installation, update and operational documentation is available in:

**[`history-bot/README.md`](history-bot/README.md)**

