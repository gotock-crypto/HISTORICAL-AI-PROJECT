import argparse
import hashlib
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from .core import HistoryEngine
from .youtube_echo import HistoryEchoPublisher

log = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    args = ap.parse_args()
    load_dotenv('/opt/history-bot/.env')
    os.environ['HISTORY_ECHO_ALLOW_SHORT'] = '1'

    e = HistoryEngine()
    media = {}
    topic = ''
    event_id = None

    try:
        topic, media, raw = e.generate_youtube_draft(max_chars=1100)
        event_id = media.get('daily_event_id')
        image_path = media.get('shorts_media_path') or media.get('media_path')
        if not image_path or not Path(image_path).is_file():
            raise RuntimeError('У статьи нет локального изображения')

        key = hashlib.sha256((str(event_id) + '\n' + topic + '\n' + raw).encode()).hexdigest()
        llm = e.gigachat
        p = HistoryEchoPublisher(
            '/opt/history-bot/data/history.db',
            os.getenv('HISTORY_ECHO_WORK_DIR', '/opt/history-bot/runtime/history_echo')
        )

        # Transient failures (Edge TTS, ffmpeg, YouTube API) must not discard
        # the event. Retry the complete publication with the same idempotency key.
        last_error = None
        for attempt in range(1, 13):
            try:
                ok, vid, err = p.publish_article(
                    raw, Path(image_path).read_bytes(), key, topic, llm
                )
                if ok:
                    topic_key = str(media.get('topic_key') or '').strip()

                    if event_id:
                        e.db.mark_event_platform_posted(int(event_id), "youtube")

                    if topic_key:
                        e.db.remember_topic(topic_key, topic)

                    log.info(
                        'HISTORY EVENT FINALIZED | event_id=%s | topic=%s',
                        event_id, topic
                    )

                    print({'ok': True, 'video_id': vid, 'error': None, 'topic': topic,
                           'attempt': attempt})
                    return 0
                last_error = err or 'unknown publication error'
                log.warning('YouTube attempt %s/12 failed: %s', attempt, last_error)
            except Exception as exc:
                last_error = str(exc)
                log.exception('YouTube attempt %s/5 exception', attempt)
            if attempt < 12:
                time.sleep(min(30 * attempt, 300))

        # Keep the event available for the next scheduled run. Never mark it
        # skipped merely because an external service failed temporarily.
        if event_id:
            e.db.set_event_platform_status(
                int(event_id), "youtube", "ready",
                f'YouTube retry scheduled after failure: {last_error}'
            )
        print({'ok': False, 'video_id': None, 'error': last_error,
               'topic': topic, 'retry_later': True})
        # Do not report a terminal process failure: scheduler will retry this event.
        return 0

    except Exception as exc:
        if event_id:
            e.db.set_event_platform_status(
                int(event_id), "youtube", "ready",
                f'YouTube draft retry scheduled: {exc}'
            )
        log.exception('YouTube draft failed; event returned to ready')
        print({'ok': False, 'video_id': None, 'error': str(exc), 'topic': topic, 'retry_later': True})
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
