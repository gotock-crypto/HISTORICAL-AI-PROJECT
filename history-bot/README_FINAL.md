# History Bot — Minerals-style final

This archive is a complete source tree assembled from the supplied history project, with `history_bot/core.py` rebuilt around the image/topic pipeline principles from the supplied minerals project.

Active flow:

1. GigaChat generates a batch of date-specific historical events.
2. The batch is consumed locally; bad images do not trigger a new topic request.
3. Persistent duplicate topic check is performed.
4. Image discovery uses Internet Archive only.
5. A broad candidate pool is downloaded and validated as actual images.
6. Resolution/format/aspect/visual-quality checks are applied.
7. Event-to-image relevance is scored.
8. Best valid image is selected.
9. Existing post generation and text validation are used.
10. Topic and media are persisted only after a complete draft succeeds.
11. Existing admin/publisher architecture remains outside the changed core logic.

The archive intentionally contains no production database and no production `.env` secrets.

For the live server, use `DEPLOY_CORE_ONLY.sh` so existing `main.py`, `admin.py`, publishers, `.env`, config, and database are not overwritten.
