from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot.backoff import next_retry_at
from app.bot.db import Database

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 4, 14, 0, tzinfo=TZ)
CONTENT = {"text": "привет", "entities": [], "media": [], "media_group_id": None}


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


def test_add_and_dedup_by_chat_and_message(db):
    pid = db.add_post(-100123, 42, None, CONTENT, NOW)
    assert pid is not None
    assert db.add_post(-100123, 42, None, CONTENT, NOW) is None  # дубликат
    # тот же message_id в другом канале — не дубликат
    assert db.add_post(-100999, 42, None, CONTENT, NOW) is not None


def test_dedup_by_media_group_id(db):
    album = {**CONTENT, "media_group_id": "g1"}
    assert db.add_post(-100123, 10, "g1", album, NOW) is not None
    # повторная регистрация той же группы с другим message_id — дубликат
    assert db.add_post(-100123, 11, "g1", album, NOW) is None


def test_due_posts_only_overdue_pending(db):
    db.add_post(-100123, 1, None, CONTENT, NOW - timedelta(minutes=1))  # просрочен
    db.add_post(-100123, 2, None, CONTENT, NOW + timedelta(hours=1))   # будущий
    due = db.due_posts(NOW)
    assert [p["message_id"] for p in due] == [1]


def test_update_content_only_pending(db):
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    new = {**CONTENT, "text": "отредактировано"}
    assert db.update_content(-100123, 1, None, new)
    assert db.get_post(pid)["content"]["text"] == "отредактировано"
    db.mark_published(pid)
    assert not db.update_content(-100123, 1, None, new)  # после публикации правки игнорятся


def test_update_content_via_media_group_id(db):
    pid = db.add_post(-100123, 10, "g1", CONTENT, NOW)
    new = {**CONTENT, "text": "caption правлен"}
    # правка пришла для сообщения 11 из группы g1
    assert db.update_content(-100123, 11, "g1", new)
    assert db.get_post(pid)["content"]["text"] == "caption правлен"


def test_register_failure_backoff_then_failed(db):
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    # 1-я неудача -> остаётся pending, publish_at сдвинут
    status = db.register_failure(pid, "err1", next_retry_at(1, NOW))
    assert status == "pending"
    assert db.get_post(pid)["attempts"] == 1
    # 2-я -> pending
    assert db.register_failure(pid, "err2", next_retry_at(2, NOW)) == "pending"
    # 3-я -> попытки исчерпаны
    assert db.register_failure(pid, "err3", next_retry_at(3, NOW)) == "failed"
    assert db.get_post(pid)["status"] == "failed"
    assert db.get_post(pid)["last_error"] == "err3"


def test_settings_roundtrip(db):
    assert db.get_setting("author_id") is None
    db.set_setting("author_id", "123")
    assert db.get_setting("author_id") == "123"
    db.set_setting("author_id", "456")
    assert db.get_setting("author_id") == "456"


def test_find_pending_by_notify(db):
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    db.set_notify_message_id(pid, 777)
    found = db.find_pending_by_notify(777)
    assert found and found["id"] == pid
    assert db.find_pending_by_notify(999) is None
