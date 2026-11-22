"""Очередь дублей в SQLite. Синхронный sqlite3 — операции короткие,
блокировка event loop ничтожна; потокобезопасность через Lock."""
import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    media_group_id TEXT,
    content_json TEXT NOT NULL,
    publish_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/published/failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    notify_message_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (channel_chat_id, message_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_media_group
    ON posts (channel_chat_id, media_group_id) WHERE media_group_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER NOT NULL,           -- владелец связки
    tg_channel_id INTEGER NOT NULL UNIQUE, -- TG-канал-источник
    max_channel_id TEXT NOT NULL,          -- chat_id канала в MAX
    delay_minutes INTEGER NOT NULL DEFAULT 60,
    tz TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
-- незавершённая регистрация (ждём id MAX-канала), переживает рестарт
CREATE TABLE IF NOT EXISTS registration_state (
    tg_user_id INTEGER PRIMARY KEY,
    tg_channel_id INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str = "queue.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self):
        self._conn.close()

    # --- посты ---

    def add_post(self, channel_chat_id: int, message_id: int, media_group_id: str | None,
                 content: dict, publish_at: datetime) -> int | None:
        """Вставляет пост в очередь. Возвращает id или None, если дубликат."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO posts
                   (channel_chat_id, message_id, media_group_id, content_json, publish_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (channel_chat_id, message_id, media_group_id,
                 json.dumps(content, ensure_ascii=False),
                 publish_at.isoformat(), datetime.now(publish_at.tzinfo).isoformat()),
            )
            self._conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def update_content(self, channel_chat_id: int, message_id: int,
                       media_group_id: str | None, content: dict) -> bool:
        """Обновляет слепок контента (правка поста до публикации).
        Только для pending. True, если запись найдена и обновлена."""
        with self._lock:
            row = self._find_pending(channel_chat_id, message_id, media_group_id)
            if not row:
                return False
            self._conn.execute(
                "UPDATE posts SET content_json = ? WHERE id = ?",
                (json.dumps(content, ensure_ascii=False), row["id"]))
            self._conn.commit()
            return True

    def get_pending(self, channel_chat_id: int, message_id: int,
                    media_group_id: str | None = None) -> dict | None:
        with self._lock:
            row = self._find_pending(channel_chat_id, message_id, media_group_id)
            return self._to_dict(row) if row else None

    def _find_pending(self, channel_chat_id, message_id, media_group_id):
        cur = self._conn.execute(
            """SELECT * FROM posts WHERE status = 'pending' AND channel_chat_id = ?
               AND (message_id = ? OR (media_group_id IS NOT NULL AND media_group_id = ?))
               ORDER BY id LIMIT 1""",
            (channel_chat_id, message_id, media_group_id or ""))
        return cur.fetchone()

    def set_publish_at(self, post_id: int, publish_at: datetime):
        with self._lock:
            self._conn.execute(
                "UPDATE posts SET publish_at = ? WHERE id = ? AND status = 'pending'",
                (publish_at.isoformat(), post_id))
            self._conn.commit()

    def set_notify_message_id(self, post_id: int, notify_message_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE posts SET notify_message_id = ? WHERE id = ?",
                (notify_message_id, post_id))
            self._conn.commit()

    def due_posts(self, now: datetime) -> list[dict]:
        """Просроченные pending-записи (включая пропущенные за простой)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM posts WHERE status = 'pending' AND publish_at <= ? ORDER BY publish_at",
                (now.isoformat(),))
            return [self._to_dict(r) for r in cur.fetchall()]

    def mark_published(self, post_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE posts SET status = 'published', last_error = NULL WHERE id = ?",
                (post_id,))
            self._conn.commit()

    def register_failure(self, post_id: int, error: str,
                         next_attempt_at: datetime | None) -> str:
        """Фиксирует неудачную попытку. next_attempt_at=None -> статус failed."""
        with self._lock:
            self._conn.execute(
                "UPDATE posts SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error, post_id))
            if next_attempt_at is None:
                self._conn.execute(
                    "UPDATE posts SET status = 'failed' WHERE id = ?", (post_id,))
                status = "failed"
            else:
                self._conn.execute(
                    "UPDATE posts SET publish_at = ? WHERE id = ?",
                    (next_attempt_at.isoformat(), post_id))
                status = "pending"
            self._conn.commit()
            return status

    def get_post(self, post_id: int) -> dict | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
            row = cur.fetchone()
            return self._to_dict(row) if row else None

    def find_pending_by_notify(self, notify_message_id: int) -> dict | None:
        """Pending-запись по id уведомления в личке автора."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM posts WHERE status = 'pending' AND notify_message_id = ?",
                (notify_message_id,))
            row = cur.fetchone()
            return self._to_dict(row) if row else None

    # --- настройки (author_id и пр.) ---

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value))
            self._conn.commit()

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["content"] = json.loads(d.pop("content_json"))
        return d

    # --- тенанты (связки TG-канал → MAX-канал) ---

    def add_tenant(self, tg_user_id: int, tg_channel_id: int, max_channel_id: str,
                   delay_minutes: int, tz: str) -> int:
        """Создаёт связку. tg_channel_id уникален — IntegrityError при повторе."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO tenants
                   (tg_user_id, tg_channel_id, max_channel_id, delay_minutes, tz, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tg_user_id, tg_channel_id, max_channel_id, delay_minutes, tz,
                 datetime.now().isoformat()))
            self._conn.commit()
            return cur.lastrowid

    def get_tenant_by_tg_channel(self, tg_channel_id: int) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tenants WHERE tg_channel_id = ?", (tg_channel_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_tenant_by_id(self, tenant_id: int) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (tenant_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def all_tenants(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT t.*, "
                "(SELECT COUNT(*) FROM posts p "
                " WHERE p.channel_chat_id = t.tg_channel_id AND p.status = 'pending') "
                "AS pending_count "
                "FROM tenants t ORDER BY t.id")
            return [dict(r) for r in cur.fetchall()]

    def tenants_of_user(self, tg_user_id: int) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tenants WHERE tg_user_id = ? ORDER BY id", (tg_user_id,))
            return [dict(r) for r in cur.fetchall()]

    def set_tenant_active(self, tenant_id: int, active: bool):
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET active = ? WHERE id = ?",
                (1 if active else 0, tenant_id))
            self._conn.commit()

    def update_tenant_max_channel(self, tenant_id: int, max_channel_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET max_channel_id = ? WHERE id = ?",
                (max_channel_id, tenant_id))
            self._conn.commit()

    def get_tenant_for_post(self, post_id: int) -> dict | None:
        """Тенант поста по привязке posts.channel_chat_id = tenants.tg_channel_id."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT t.* FROM tenants t
                   JOIN posts p ON p.channel_chat_id = t.tg_channel_id
                   WHERE p.id = ?""", (post_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def pending_posts_of_channel(self, tg_channel_id: int) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM posts WHERE status = 'pending' AND channel_chat_id = ?",
                (tg_channel_id,))
            return [self._to_dict(r) for r in cur.fetchall()]

    def migrate_legacy_tenant(self, legacy_max_channel_id: str,
                              legacy_delay: int, legacy_tz: str) -> int | None:
        """Идемпотентная миграция одноканальной конфигурации: если тенантов
        ещё нет, но есть settings.author_id и посты одного канала — создаём
        тенанта на этот канал с legacy-значениями MAX_CHANNEL_ID и задержки."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM tenants")
            if cur.fetchone()["n"] > 0:
                return None
            author = self._conn.execute(
                "SELECT value FROM settings WHERE key = 'author_id'").fetchone()
            if not author:
                return None
            row = self._conn.execute(
                "SELECT DISTINCT channel_chat_id FROM posts LIMIT 1").fetchone()
            if not row:
                return None
        return self.add_tenant(int(author["value"]), row["channel_chat_id"],
                               legacy_max_channel_id, legacy_delay, legacy_tz)

    # --- незавершённая регистрация ---

    def set_registration_state(self, tg_user_id: int, tg_channel_id: int):
        with self._lock:
            self._conn.execute(
                """INSERT INTO registration_state (tg_user_id, tg_channel_id)
                   VALUES (?, ?)
                   ON CONFLICT(tg_user_id) DO UPDATE SET tg_channel_id = excluded.tg_channel_id""",
                (tg_user_id, tg_channel_id))
            self._conn.commit()

    def get_registration_state(self, tg_user_id: int) -> int | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT tg_channel_id FROM registration_state WHERE tg_user_id = ?",
                (tg_user_id,))
            row = cur.fetchone()
            return row["tg_channel_id"] if row else None

    def clear_registration_state(self, tg_user_id: int):
        with self._lock:
            self._conn.execute(
                "DELETE FROM registration_state WHERE tg_user_id = ?", (tg_user_id,))
            self._conn.commit()
