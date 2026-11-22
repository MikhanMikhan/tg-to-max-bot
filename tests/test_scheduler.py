"""Планировщик: маршрутизация публикации по max_channel_id тенанта."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.config import Config
from app.db import Database
from app.scheduler import Scheduler

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 4, 14, 0, tzinfo=TZ)
CONTENT = {"text": "привет", "entities": [], "media": [], "media_group_id": None}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


@pytest.fixture
def config():
    return Config(telegram_bot_token="t", max_bot_token="m", max_channel_id="",
                  default_delay_minutes=60, tz=TZ)


def make_scheduler(db, config):
    return Scheduler(db, config, AsyncMock(), AsyncMock())


def test_publish_routes_to_tenant_max_channel(db, config):
    db.add_tenant(111, -100123, "-72075083149532", 60, "Europe/Moscow")
    db.add_tenant(222, -100456, "-111111", 60, "Europe/Moscow")
    p1 = db.add_post(-100123, 1, None, CONTENT, NOW)
    p2 = db.add_post(-100456, 1, None, CONTENT, NOW)
    sch = make_scheduler(db, config)
    run(sch.publish(db.get_post(p1)))
    run(sch.publish(db.get_post(p2)))
    calls = sch.max_client.publish.await_args_list
    assert calls[0].kwargs["chat_id"] == "-72075083149532"
    assert calls[1].kwargs["chat_id"] == "-111111"
    assert db.get_post(p1)["status"] == "published"
    assert db.get_post(p2)["status"] == "published"


def test_publish_inactive_tenant_fails_without_retry(db, config):
    tid = db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    db.set_tenant_active(tid, False)
    sch = make_scheduler(db, config)
    run(sch.publish(db.get_post(pid)))
    post = db.get_post(pid)
    assert post["status"] == "failed"
    assert post["last_error"] == "тенант отключён"
    sch.max_client.publish.assert_not_awaited()
    # владелец уведомлён
    assert sch.tg_bot.send_message.await_args.args[0] == 111


def test_publish_post_without_tenant_fails_silently(db, config):
    pid = db.add_post(-100999, 1, None, CONTENT, NOW)
    sch = make_scheduler(db, config)
    run(sch.publish(db.get_post(pid)))
    assert db.get_post(pid)["status"] == "failed"
    sch.max_client.publish.assert_not_awaited()
    sch.tg_bot.send_message.assert_not_awaited()


def test_publish_failure_notifies_tenant_owner(db, config):
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    sch = make_scheduler(db, config)
    sch.max_client.publish = AsyncMock(side_effect=RuntimeError("boom"))
    # исчерпываем попытки: 3-я неудача -> failed + уведомление владельцу
    for _ in range(3):
        run(sch.publish(db.get_post(pid)))
    assert db.get_post(pid)["status"] == "failed"
    assert sch.tg_bot.send_message.await_args.args[0] == 111
    assert "boom" in sch.tg_bot.send_message.await_args.args[1]
