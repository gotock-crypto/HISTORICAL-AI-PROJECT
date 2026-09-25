import sqlite3
import time
import json
from pathlib import Path


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.execute("PRAGMA busy_timeout=30000")
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fp TEXT UNIQUE,
            text TEXT NOT NULL,
            image_url TEXT,
            source_url TEXT,
            media_path TEXT,
            status TEXT DEFAULT 'draft',
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS destinations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            target TEXT NOT NULL,
            title TEXT,
            enabled INTEGER DEFAULT 1,
            UNIQUE(platform,target)
        );
        CREATE TABLE IF NOT EXISTS publishes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            target TEXT NOT NULL,
            message_id TEXT,
            status TEXT NOT NULL,
            error TEXT,
            created_at REAL,
            UNIQUE(post_id,platform,target)
        );
        CREATE TABLE IF NOT EXISTS topics(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_key TEXT UNIQUE,
            topic TEXT NOT NULL,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS media_memory(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_url TEXT UNIQUE,
            source_url TEXT,
            image_hash TEXT,
            visual_hash TEXT,
            title TEXT,
            topic_key TEXT,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_media_hash ON media_memory(image_hash);
        CREATE INDEX IF NOT EXISTS idx_media_visual_hash ON media_memory(visual_hash);
        CREATE INDEX IF NOT EXISTS idx_topics_created ON topics(created_at);
        """)
        self._ensure("posts", "media_status", "TEXT DEFAULT 'temporary'")
        self._ensure("posts", "daily_event_id", "INTEGER")
        self._ensure("posts", "topic_key", "TEXT")
        self._ensure("posts", "topic", "TEXT")
        self._ensure("media_memory", "source_name", "TEXT DEFAULT ''")
        self._ensure("media_memory", "metadata", "TEXT DEFAULT ''")
        self.ensure_daily_tables()
        self.c.commit()

    def _ensure(self, table, column, sql_type):
        cols = [r[1] for r in self.c.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            self.c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def recent_fp(self, limit=2000):
        return [r[0] for r in self.c.execute(
            "SELECT fp FROM posts WHERE fp IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,)
        )]

    def post_count(self):
        return self.c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    # Topics are permanent memory: a successful topic key is never reused.
    def topic_used(self, key):
        return self.c.execute(
            "SELECT 1 FROM topics WHERE topic_key=? LIMIT 1", (key,)
        ).fetchone() is not None

    def remember_topic(self, key, topic):
        self.c.execute(
            "INSERT OR IGNORE INTO topics(topic_key,topic,created_at) VALUES(?,?,?)",
            (key, topic, time.time())
        )
        self.c.commit()

    def media_used(self, url):
        return bool(url) and self.c.execute(
            "SELECT 1 FROM media_memory WHERE image_url=? LIMIT 1", (url,)
        ).fetchone() is not None

    def media_hash_used(self, image_hash):
        return bool(image_hash) and self.c.execute(
            "SELECT 1 FROM media_memory WHERE image_hash=? LIMIT 1",
            (image_hash,)
        ).fetchone() is not None

    def similar_visual_used(self, visual_hash, max_distance=6):
        if not visual_hash:
            return False
        rows = self.c.execute(
            "SELECT visual_hash FROM media_memory "
            "WHERE visual_hash IS NOT NULL AND visual_hash<>''"
        ).fetchall()
        try:
            value = int(visual_hash, 16)
            bits = len(visual_hash) * 4
            for row in rows:
                other = str(row[0] or "")
                if len(other) != len(visual_hash):
                    continue
                distance = (value ^ int(other, 16)).bit_count()
                if distance <= max_distance:
                    return True
        except Exception:
            return False
        return False

    def remember_media(self, x):
        url = x.get("image_url", "")
        if not url:
            return
        self.c.execute(
            """INSERT OR IGNORE INTO media_memory(
                image_url,source_url,image_hash,visual_hash,title,
                topic_key,source_name,metadata,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                url,
                x.get("source_url", ""),
                x.get("image_hash", ""),
                x.get("visual_hash", ""),
                x.get("title", ""),
                x.get("topic_key", ""),
                x.get("source_name", ""),
                x.get("metadata", ""),
                time.time(),
            )
        )
        self.c.commit()

    def save_post(self, fp, x, text):
        daily_event_id = x.get("daily_event_id")

        if not daily_event_id:
            raise ValueError("Daily post must have daily_event_id")

        media_path = str(x.get("media_path") or "").strip()
        if not media_path or not Path(media_path).is_file():
            raise ValueError(
                "Daily post must have a valid local GigaChat image"
            )

        event = self.c.execute(
            "SELECT id FROM daily_events WHERE id=?",
            (int(daily_event_id),)
        ).fetchone()

        if not event:
            raise ValueError(f"Daily event not found: {daily_event_id}")

        try:
            cur = self.c.execute(
                """INSERT INTO posts(
                    fp,text,image_url,source_url,media_path,status,
                    media_status,daily_event_id,topic_key,topic,created_at
                ) VALUES(?,?,?,?,?,'draft','temporary',?,?,?,?)""",
                (
                    fp, text, x.get("image_url", ""),
                    x.get("source_url", ""), x.get("media_path", ""),
                    x.get("daily_event_id"), x.get("topic_key", ""), x.get("topic", ""), time.time()
                )
            )
            self.c.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            row = self.c.execute(
                "SELECT id FROM posts WHERE fp=? ORDER BY id DESC LIMIT 1", (fp,)
            ).fetchone()
            if not row:
                raise
            self.c.execute(
                """UPDATE posts SET text=?,image_url=?,source_url=?,
                   media_path=?,status='draft',media_status='temporary',daily_event_id=?,topic_key=?,topic=?,
                   created_at=? WHERE id=?""",
                (
                    text, x.get("image_url", ""), x.get("source_url", ""),
                    x.get("media_path", ""), x.get("daily_event_id"), x.get("topic_key", ""), x.get("topic", ""), time.time(), row[0]
                )
            )
            self.c.commit()
            return row[0]

    def update_post_media(self, post_id, media_path, image_url=None):
        if image_url is None:
            self.c.execute("UPDATE posts SET media_path=?,media_status='temporary' WHERE id=?",(media_path,post_id))
        else:
            self.c.execute("UPDATE posts SET media_path=?,image_url=?,media_status='temporary' WHERE id=?",(media_path,image_url,post_id))
        self.c.commit()

    def mark_media_deleted(self, path):
        self.c.execute(
            "UPDATE posts SET media_path='',media_status='deleted' WHERE media_path=?",
            (path,)
        )
        self.c.commit()

    # ---- Daily historical queue -------------------------------------------------

    def ensure_event_platforms(self, event_id=None):
        """Create independent publication states for Telegram/MAX and YouTube."""
        where = ""
        params = ()

        if event_id is not None:
            where = "WHERE id=?"
            params = (event_id,)

        rows = self.c.execute(
            f"SELECT id, status FROM daily_events {where}",
            params
        ).fetchall()

        now = time.time()

        for row in rows:
            event_id_value = row["id"]
            legacy_status = row["status"]

            # Existing 'posted' events are considered completed on both
            # platforms to prevent accidental duplicate publications.
            initial_status = "posted" if legacy_status == "posted" else "ready"

            for platform in ("tgmax", "youtube"):
                self.c.execute(
                    """
                    INSERT OR IGNORE INTO daily_event_platforms
                    (event_id, platform, status, error, created_at, updated_at)
                    VALUES (?, ?, ?, '', ?, ?)
                    """,
                    (
                        event_id_value,
                        platform,
                        initial_status,
                        now,
                        now,
                    ),
                )

        self.c.commit()

    def next_daily_event_for_platform(self, batch_date, platform):
        """Reserve a daily event independently for one platform."""
        if platform not in ("tgmax", "youtube"):
            raise ValueError(f"Unsupported platform: {platform}")

        now = time.time()
        stale_before = now - 45 * 60

        self.c.execute("BEGIN IMMEDIATE")

        try:
            self.c.execute(
                """
                UPDATE daily_event_platforms
                SET status='ready',
                    error='recovered stale processing reservation',
                    updated_at=?
                WHERE platform=?
                  AND status='processing'
                  AND updated_at < ?
                """,
                (now, platform, stale_before),
            )

            row = self.c.execute(
                """
                SELECT e.*
                FROM daily_events e
                JOIN daily_batches b ON b.id=e.batch_id
                JOIN daily_event_platforms ep ON ep.event_id=e.id
                WHERE b.batch_date=?
                  AND ep.platform=?
                  AND ep.status='ready'
                ORDER BY e.id
                LIMIT 1
                """,
                (batch_date, platform),
            ).fetchone()

            if not row:
                self.c.commit()
                return None

            self.c.execute(
                """
                UPDATE daily_event_platforms
                SET status='processing',
                    error='',
                    updated_at=?
                WHERE event_id=?
                  AND platform=?
                  AND status='ready'
                """,
                (now, row["id"], platform),
            )

            self.c.commit()
            return dict(row)

        except Exception:
            self.c.rollback()
            raise

    def set_event_platform_status(
        self, event_id, platform, status, error=""
    ):
        now = time.time()

        self.c.execute(
            """
            INSERT OR IGNORE INTO daily_event_platforms
            (event_id, platform, status, error, created_at, updated_at)
            VALUES (?, ?, 'ready', '', ?, ?)
            """,
            (event_id, platform, now, now),
        )

        self.c.execute(
            """
            UPDATE daily_event_platforms
            SET status=?, error=?, updated_at=?
            WHERE event_id=? AND platform=?
            """,
            (status, error or "", now, event_id, platform),
        )
        self.c.commit()

    def mark_event_platform_posted(self, event_id, platform):
        now = time.time()
        self.c.execute(
            """
            UPDATE daily_event_platforms
            SET status='posted',
                error='',
                updated_at=?,
                posted_at=?
            WHERE event_id=? AND platform=?
            """,
            (now, now, event_id, platform),
        )
        self.c.commit()

    def ensure_daily_tables(self):

        self.c.execute("""
            CREATE TABLE IF NOT EXISTS daily_event_platforms (
                event_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                posted_at REAL,
                PRIMARY KEY (event_id, platform),
                FOREIGN KEY (event_id) REFERENCES daily_events(id)
            )
        """)

        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS daily_batches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month_day TEXT NOT NULL,
            batch_date TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'building',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            month_day TEXT NOT NULL,
            year INTEGER NOT NULL,
            title TEXT NOT NULL,
            event TEXT NOT NULL,
            facts TEXT NOT NULL DEFAULT '[]',
            importance INTEGER NOT NULL DEFAULT 5,
            drama INTEGER NOT NULL DEFAULT 5,
            topic_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'ready',
            error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            posted_at REAL,
            FOREIGN KEY(batch_id) REFERENCES daily_batches(id)
        );
        CREATE TABLE IF NOT EXISTS daily_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            image_url TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(event_id) REFERENCES daily_events(id)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_events_batch_status ON daily_events(batch_id,status);
        CREATE INDEX IF NOT EXISTS idx_daily_media_event_score ON daily_media(event_id,score DESC);
        """)
        self.c.commit()

    def cleanup_old_daily_data(self, current_date):
        """Remove old daily queue rows not referenced by posts."""
        old_event_ids = [
            row[0] for row in self.c.execute(
                """
                SELECT e.id
                FROM daily_events e
                JOIN daily_batches b ON b.id=e.batch_id
                WHERE b.batch_date < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM posts p
                      WHERE p.daily_event_id=e.id
                  )
                """,
                (current_date,),
            ).fetchall()
        ]

        if not old_event_ids:
            return 0

        placeholders = ",".join("?" for _ in old_event_ids)

        self.c.execute(
            f"DELETE FROM daily_media WHERE event_id IN ({placeholders})",
            old_event_ids,
        )
        self.c.execute(
            f"DELETE FROM daily_event_platforms WHERE event_id IN ({placeholders})",
            old_event_ids,
        )
        self.c.execute(
            f"DELETE FROM daily_events WHERE id IN ({placeholders})",
            old_event_ids,
        )
        self.c.commit()

        return len(old_event_ids)

    def daily_batch(self, batch_date):
        row=self.c.execute("SELECT * FROM daily_batches WHERE batch_date=?",(batch_date,)).fetchone()
        return dict(row) if row else None

    def create_daily_batch(self, batch_date, month_day):
        import time as _time
        now=_time.time()
        cur=self.c.execute("INSERT INTO daily_batches(month_day,batch_date,status,created_at,updated_at) VALUES(?,?,?, ?,?) ON CONFLICT(batch_date) DO UPDATE SET updated_at=excluded.updated_at",(month_day,batch_date,'building',now,now))
        self.c.commit()
        row=self.c.execute("SELECT * FROM daily_batches WHERE batch_date=?",(batch_date,)).fetchone()
        return dict(row)

    def set_daily_batch_status(self,batch_id,status):
        self.c.execute("UPDATE daily_batches SET status=?,updated_at=? WHERE id=?",(status,time.time(),batch_id)); self.c.commit()

    def add_daily_event(self,batch_id, item):
        now=time.time()
        cur=self.c.execute("""INSERT OR IGNORE INTO daily_events(batch_id,month_day,year,title,event,facts,importance,drama,topic_key,status,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(batch_id,item['month_day'],item['year'],item['title'],item['event'],json.dumps(item.get('facts') or [],ensure_ascii=False),item.get('importance',5),item.get('drama',5),item['topic_key'],'ready','',now,now))
        self.c.commit()
        row=self.c.execute("SELECT * FROM daily_events WHERE topic_key=?",(item['topic_key'],)).fetchone()
        if row:
            self.ensure_event_platforms(row["id"])
        return dict(row)

    def add_daily_media(self,event_id,x,position):
        now=time.time()
        self.c.execute("""INSERT OR IGNORE INTO daily_media(event_id,image_url,source_url,source_name,title,metadata,score,position,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(event_id,x.get('image_url',''),x.get('source_url',''),x.get('source_name',''),x.get('title',''),x.get('metadata',''),float(x.get('score',0)),position,'candidate',now,now)); self.c.commit()

    def daily_events_count(self,batch_date):
        row=self.c.execute("SELECT COUNT(*) FROM daily_events e JOIN daily_batches b ON b.id=e.batch_id WHERE b.batch_date=?",(batch_date,)).fetchone(); return int(row[0])

    def ready_daily_events(self,batch_date):
        rows=self.c.execute("""SELECT e.* FROM daily_events e JOIN daily_batches b ON b.id=e.batch_id WHERE b.batch_date=? AND e.status='ready' ORDER BY e.id""",(batch_date,)).fetchall(); return [dict(r) for r in rows]

    def daily_event_media(self,event_id):
        rows=self.c.execute("SELECT * FROM daily_media WHERE event_id=? AND status IN ('candidate','failed') ORDER BY score DESC,position ASC",(event_id,)).fetchall(); return [dict(r) for r in rows]

    def set_daily_event_status(self,event_id,status,error=''):
        self.c.execute("UPDATE daily_events SET status=?,error=?,updated_at=? WHERE id=?",(status,str(error)[:1000],time.time(),event_id)); self.c.commit()

    def mark_daily_posted(self,event_id):
        self.c.execute(
            "UPDATE daily_events SET status='posted',error='',posted_at=?,updated_at=? WHERE id=?",
            (time.time(), time.time(), event_id)
        )
        self.c.commit()

    def daily_batch_ready(self,batch_date):
        row=self.c.execute("SELECT status FROM daily_batches WHERE batch_date=?",(batch_date,)).fetchone(); return bool(row and row[0]=='ready')

    def recover_daily_processing(self, batch_date):
        self.c.execute("""UPDATE daily_events SET status='ready', error='recovered after restart', updated_at=? WHERE status IN ('processing','publishing') AND batch_id IN (SELECT id FROM daily_batches WHERE batch_date=?) AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.daily_event_id=daily_events.id AND p.status IN ('draft','partially_published'))""",(time.time(),batch_date)); self.c.commit()

    def daily_event(self,event_id):
        row=self.c.execute("SELECT * FROM daily_events WHERE id=?",(event_id,)).fetchone(); return dict(row) if row else None

    def latest(self):
        row = self.c.execute(
            "SELECT * FROM posts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


    def latest_unfinished(self):
        row = self.c.execute(
            """SELECT * FROM posts
               WHERE status IN ('draft','partially_published')
                 AND daily_event_id IS NOT NULL
               ORDER BY id ASC
               LIMIT 1"""
        ).fetchone()
        return dict(row) if row else None

    def publish_statuses(self, post_id):
        rows = self.c.execute(
            "SELECT platform,status FROM publishes WHERE post_id=?", (post_id,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def set_status(self, post_id, status):
        self.c.execute(
            "UPDATE posts SET status=? WHERE id=?", (status, post_id)
        )
        self.c.commit()

    def publish_log(self, post_id, platform, target, message_id, status, error=""):
        self.c.execute(
            """INSERT INTO publishes(
                post_id,platform,target,message_id,status,error,created_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(post_id,platform,target) DO UPDATE SET
                message_id=excluded.message_id,
                status=excluded.status,
                error=excluded.error,
                created_at=excluded.created_at""",
            (
                post_id, platform, target, str(message_id or ""),
                status, str(error)[:1000], time.time()
            )
        )
        self.c.commit()
