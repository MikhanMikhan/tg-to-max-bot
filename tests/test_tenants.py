from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.bot.db import Database

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 4, 14, 0, tzinfo=TZ)
CONTENT = {"text": "привет", "entities": [], "media": [], "media_group_id": None}


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


def test_tenant_crud(db):
    tid = db.add_tenant(111, -100123, "-72075083149532", 60, "Europe/Moscow")
    t = db.get_tenant_by_id(tid)
    assert t["tg_user_id"] == 111
    assert t["tg_channel_id"] == -100123
    assert t["max_channel_id"] == "-72075083149532"
    assert t["delay_minutes"] == 60
    assert t["active"] == 1
    assert db.get_tenant_by_tg_channel(-100123)["id"] == tid
    assert [x["id"] for x in db.tenants_of_user(111)] == [tid]
    assert db.tenants_of_user(222) == []

    db.set_tenant_active(tid, False)
    assert db.get_tenant_by_id(tid)["active"] == 0
    db.set_tenant_active(tid, True)
    assert db.get_tenant_by_id(tid)["active"] == 1

    db.update_tenant_max_channel(tid, "-999")
    assert db.get_tenant_by_id(tid)["max_channel_id"] == "-999"


def test_tenant_tg_channel_unique(db):
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    with pytest.raises(Exception):
        db.add_tenant(222, -100123, "-2", 60, "Europe/Moscow")


def test_get_tenant_for_post(db):
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    pid = db.add_post(-100123, 42, None, CONTENT, NOW)
    tenant = db.get_tenant_for_post(pid)
    assert tenant and tenant["tg_user_id"] == 111
    # пост из канала без тенанта -> None
    pid2 = db.add_post(-100999, 1, None, CONTENT, NOW)
    assert db.get_tenant_for_post(pid2) is None


def test_pending_posts_of_channel(db):
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    p1 = db.add_post(-100123, 1, None, CONTENT, NOW)
    p2 = db.add_post(-100123, 2, None, CONTENT, NOW)
    db.add_post(-100999, 3, None, CONTENT, NOW)  # другой канал
    db.mark_published(p2)
    assert [p["id"] for p in db.pending_posts_of_channel(-100123)] == [p1]


def test_registration_state_roundtrip(db):
    assert db.get_registration_state(111) is None
    db.set_registration_state(111, -100123)
    assert db.get_registration_state(111) == -100123
    db.set_registration_state(111, -100456)  # перезапись
    assert db.get_registration_state(111) == -100456
    db.clear_registration_state(111)
    assert db.get_registration_state(111) is None


def test_migrate_legacy_tenant(db):
    db.set_setting("author_id", "111")
    db.add_post(-100123, 1, None, CONTENT, NOW)
    tid = db.migrate_legacy_tenant("-72075083149532", 90, "Europe/Moscow")
    assert tid is not None
    t = db.get_tenant_by_id(tid)
    assert (t["tg_user_id"], t["tg_channel_id"]) == (111, -100123)
    assert t["max_channel_id"] == "-72075083149532"
    assert t["delay_minutes"] == 90
    assert t["tz"] == "Europe/Moscow"
    # идемпотентно: повторный вызов ничего не создаёт
    assert db.migrate_legacy_tenant("-72075083149532", 90, "Europe/Moscow") is None
    assert len(db.tenants_of_user(111)) == 1


def test_migrate_legacy_no_author_no_posts(db):
    # ни author_id, ни постов -> миграция не выполняется
    assert db.migrate_legacy_tenant("-1", 60, "Europe/Moscow") is None
    db.set_setting("author_id", "111")
    assert db.migrate_legacy_tenant("-1", 60, "Europe/Moscow") is None
