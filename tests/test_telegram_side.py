"""Хендлеры TelegramSide: регистрация связок, фильтрация постов по тенантам.
Сеть замокана: Bot и MaxClient — AsyncMock, aiogram Dispatcher не нужен."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.enums import ChatMemberStatus

from app.bot.config import Config
from app.bot.db import Database
from app.bot.telegram_side import TelegramSide

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


def make_side(db, config, bot=None, max_client=None):
    bot = bot or AsyncMock()
    # уведомление о дубле возвращает Message с message_id
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=555))
    if max_client is None:
        max_client = AsyncMock()
    # _register требует dp, но хендлеры вызываем напрямую
    side = TelegramSide.__new__(TelegramSide)
    side.bot = bot
    side.db = db
    side.config = config
    side.max_client = max_client
    side._bot_id = 999
    from app.bot.albums import MediaGroupCollector
    side.collector = MediaGroupCollector(delay=2.0)
    return side


def msg(user_id=111, text=None, forward_chat=None, chat_id=777, message_id=1,
        reply_to=None):
    m = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type="private"),
        reply_to_message=reply_to,
        forward_origin=(SimpleNamespace(type="channel", chat=forward_chat)
                        if forward_chat else None),
        answer=AsyncMock(),
    )
    return m


def forward_msg(user_id=111, channel_id=-100123, title="Канал"):
    return msg(user_id=user_id,
               forward_chat=SimpleNamespace(id=channel_id, title=title))


# --- регистрация: проверки прав ---

def test_forward_from_non_admin_user_rejected(db, config):
    side = make_side(db, config)
    side.bot.get_chat_member = AsyncMock(side_effect=[
        SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),  # бот
        SimpleNamespace(status=ChatMemberStatus.MEMBER),         # пользователь
    ])
    m = forward_msg()
    run(side.on_forwarded_post(m))
    m.answer.assert_awaited_once()
    assert "не администратор" in m.answer.await_args.args[0]
    assert db.get_registration_state(111) is None


def test_forward_without_bot_admin_rejected(db, config):
    side = make_side(db, config)
    side.bot.get_chat_member = AsyncMock(side_effect=[
        SimpleNamespace(status=ChatMemberStatus.MEMBER),          # бот
        SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),   # пользователь
    ])
    m = forward_msg()
    run(side.on_forwarded_post(m))
    assert "Я не администратор" in m.answer.await_args.args[0]
    assert db.get_registration_state(111) is None


def test_forward_ok_sets_registration_state(db, config):
    side = make_side(db, config)
    admin = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
    side.bot.get_chat_member = AsyncMock(return_value=admin)
    m = forward_msg()
    run(side.on_forwarded_post(m))
    assert db.get_registration_state(111) == -100123
    assert "пришлите id" in m.answer.await_args.args[0]


def test_forward_not_from_channel_ignored(db, config):
    side = make_side(db, config)
    m = msg()
    m.forward_origin = SimpleNamespace(type="user")  # переслано от пользователя
    run(side.on_forwarded_post(m))
    m.answer.assert_not_awaited()
    assert db.get_registration_state(111) is None


# --- регистрация: ввод MAX-канала ---

def test_max_id_creates_tenant(db, config):
    side = make_side(db, config)
    db.set_registration_state(111, -100123)
    m = msg(text="-72075083149532")
    run(side.on_private_text(m))
    side.max_client.get_chat.assert_awaited_once_with("-72075083149532")
    t = db.get_tenant_by_tg_channel(-100123)
    assert t and t["max_channel_id"] == "-72075083149532"
    assert t["tg_user_id"] == 111 and t["delay_minutes"] == 60
    assert db.get_registration_state(111) is None  # состояние очищено
    assert "Связка подключена" in m.answer.await_args.args[0]


def test_max_id_from_link(db, config):
    side = make_side(db, config)
    db.set_registration_state(111, -100123)
    m = msg(text="https://max.ru/-72075083149532")
    run(side.on_private_text(m))
    assert db.get_tenant_by_tg_channel(-100123)["max_channel_id"] == "-72075083149532"


def test_max_chat_unavailable_keeps_state(db, config):
    side = make_side(db, config)
    side.max_client.get_chat = AsyncMock(side_effect=Exception("404"))
    db.set_registration_state(111, -100123)
    m = msg(text="-72075083149532")
    run(side.on_private_text(m))
    assert db.get_tenant_by_tg_channel(-100123) is None
    assert db.get_registration_state(111) == -100123  # можно повторить
    assert "Не смог открыть" in m.answer.await_args.args[0]


def test_registration_without_max_client(db, config):
    side = make_side(db, config)
    side.max_client = None
    db.set_registration_state(111, -100123)
    m = msg(text="-72075083149532")
    run(side.on_private_text(m))
    assert db.get_tenant_by_tg_channel(-100123) is None
    assert "MAX не настроен" in m.answer.await_args.args[0]


def test_text_without_state_passes_through(db, config):
    side = make_side(db, config)
    m = msg(text="-72075083149532")
    from aiogram.dispatcher.event.bases import SkipHandler
    # вне регистрации хендлер пропускает апдейт дальше по цепочке Dispatcher
    with pytest.raises(SkipHandler):
        run(side.on_private_text(m))
    m.answer.assert_not_awaited()


# --- посты каналов ---

def channel_post(chat_id=-100123, message_id=1, text="привет"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        media_group_id=None,
        text=text, caption=None, entities=None, caption_entities=None,
        photo=None, video=None, audio=None, voice=None, document=None,
    )


def test_post_from_unregistered_channel_ignored(db, config):
    side = make_side(db, config)
    run(side.on_channel_post(channel_post(chat_id=-100999)))
    assert db.due_posts(NOW.replace(year=2030)) == []
    side.bot.send_message.assert_not_awaited()


def test_post_from_inactive_tenant_ignored(db, config):
    side = make_side(db, config)
    tid = db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    db.set_tenant_active(tid, False)
    run(side.on_channel_post(channel_post()))
    assert db.due_posts(NOW.replace(year=2030)) == []


def test_post_enqueued_with_tenant_delay(db, config):
    side = make_side(db, config)
    db.add_tenant(111, -100123, "-1", 120, "Europe/Moscow")
    before = datetime.now(TZ)
    run(side.on_channel_post(channel_post()))
    post = db.get_pending(-100123, 1)
    assert post is not None
    publish_at = datetime.fromisoformat(post["publish_at"])
    delta = (publish_at - before).total_seconds()
    assert 119 * 60 < delta <= 121 * 60
    # уведомление ушло владельцу связки
    assert side.bot.send_message.await_args.args[0] == 111


# --- /status и /disconnect ---

def test_status_lists_tenants(db, config):
    side = make_side(db, config)
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    db.add_tenant(111, -100456, "-2", 60, "Europe/Moscow")
    m = msg()
    run(side.on_status(m))
    text = m.answer.await_args.args[0]
    assert "-100123" in text and "-100456" in text


def test_disconnect_single_tenant_no_arg(db, config):
    side = make_side(db, config)
    tid = db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    m = msg(text="/disconnect")
    run(side.on_disconnect(m))
    assert db.get_tenant_by_id(tid)["active"] == 0
    # ожидавший дубль отменён без ретраев
    assert db.get_post(pid)["status"] == "failed"
    assert db.get_post(pid)["last_error"] == "тенант отключён"


def test_disconnect_requires_arg_with_several(db, config):
    side = make_side(db, config)
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    db.add_tenant(111, -100456, "-2", 60, "Europe/Moscow")
    m = msg(text="/disconnect")
    run(side.on_disconnect(m))
    assert "несколько связок" in m.answer.await_args.args[0]
    assert db.get_tenant_by_tg_channel(-100123)["active"] == 1
    m2 = msg(text="/disconnect -100456")
    run(side.on_disconnect(m2))
    assert db.get_tenant_by_tg_channel(-100456)["active"] == 0
    assert db.get_tenant_by_tg_channel(-100123)["active"] == 1


# --- реплай владельца ---

def test_author_reply_only_from_tenant_owner(db, config):
    side = make_side(db, config)
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    db.set_notify_message_id(pid, 555)
    notify = SimpleNamespace(message_id=555)
    # чужой пользователь — игнор
    m = msg(user_id=222, text="18:00", reply_to=notify)
    run(side.on_author_reply(m))
    m.answer.assert_not_awaited()
    # владелец — перенос
    m2 = msg(user_id=111, text="завтра 15:30", reply_to=notify)
    run(side.on_author_reply(m2))
    assert "Перенёс" in m2.answer.await_args.args[0]


# --- маршрутизация через реальный Dispatcher ---

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update, User


class DummySession(BaseSession):
    """Сессия без сети: отвечает на SendMessage минимально валидным Message."""

    async def make_request(self, bot, method, timeout=None):
        from aiogram.methods import SendMessage
        assert isinstance(method, SendMessage), f"неожиданный вызов API: {method}"
        return Message(message_id=999, date=datetime.now(),
                       chat=Chat(id=int(method.chat_id), type="private"))

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError

    async def close(self):
        pass


def tg_message(message_id=7, user_id=111, text=None, reply_to_id=None):
    return Message(
        message_id=message_id, date=datetime.now(),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="A"),
        text=text,
        reply_to_message=(Message(message_id=reply_to_id, date=datetime.now(),
                                  chat=Chat(id=user_id, type="private"))
                          if reply_to_id else None),
    )


def test_dispatch_reply_with_time_reaches_author_reply(db, config):
    """Реплай «завтра 15:30» на уведомление должен дойти до on_author_reply
    через цепочку хендлеров Dispatcher (раньше его съедал on_private_text)."""
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    pid = db.add_post(-100123, 1, None, CONTENT, NOW)
    db.set_notify_message_id(pid, 555)
    bot = Bot(token="42:TEST", session=DummySession())
    dp = Dispatcher()
    TelegramSide(bot, dp, db, config, AsyncMock())
    run(dp.feed_update(bot, Update(update_id=1,
                                   message=tg_message(text="завтра 15:30",
                                                      reply_to_id=555))))
    publish_at = datetime.fromisoformat(db.get_post(pid)["publish_at"])
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    assert (publish_at.date(), publish_at.hour, publish_at.minute) == \
           (tomorrow, 15, 30)


# --- команды оператора ---

def admin_config(config):
    config.admin_user_id = 900
    return config


def test_admin_lists_all_tenants(db, config):
    side = make_side(db, admin_config(config))
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    db.add_tenant(222, -100456, "-2", 30, "Europe/Moscow")
    db.add_post(-100123, 1, None, CONTENT, NOW)
    m = msg(user_id=900)
    run(side.on_admin(m))
    text = m.answer.await_args.args[0]
    assert "111" in text and "222" in text
    assert "-100123" in text and "-100456" in text
    assert "ожидает дублей: 1" in text


def test_admin_commands_ignored_for_non_admin(db, config):
    side = make_side(db, config)
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    m = msg(user_id=111)  # владелец связки, но не оператор
    run(side.on_admin(m))
    m.answer.assert_not_awaited()
    m2 = msg(user_id=111, text="/admin_disconnect 1")
    run(side.on_admin_disconnect(m2))
    m2.answer.assert_not_awaited()
    assert db.get_tenant_by_tg_channel(-100123)["active"] == 1


def test_admin_commands_noop_without_admin_configured(db):
    config = Config(telegram_bot_token="t", max_bot_token="m", max_channel_id="",
                    default_delay_minutes=60, tz=TZ)  # admin_user_id = 0
    side = make_side(db, config)
    db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    m = msg(user_id=111)
    run(side.on_admin(m))
    m.answer.assert_not_awaited()


def test_admin_disconnect_deactivates_and_notifies(db, config):
    side = make_side(db, admin_config(config))
    tid = db.add_tenant(111, -100123, "-1", 60, "Europe/Moscow")
    db.add_post(-100123, 1, None, CONTENT, NOW)
    m = msg(user_id=900, text=f"/admin_disconnect {tid}")
    run(side.on_admin_disconnect(m))
    assert db.get_tenant_by_id(tid)["active"] == 0
    post = db.pending_posts_of_channel(-100123)
    assert post == []
    # оператору — подтверждение, владельцу — уведомление
    assert "отключена" in m.answer.await_args.args[0]
    side.bot.send_message.assert_awaited_once()
    assert side.bot.send_message.await_args.args[0] == 111
