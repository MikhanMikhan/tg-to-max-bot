"""Telegram-сторона: регистрация связок (тенантов), приём постов каналов,
правок, диалог с владельцами."""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .albums import MediaGroupCollector, sort_album
from .config import Config
from .content import snapshot_from_album, snapshot_from_message
from .db import Database
from .max_client import MaxClient
from .timeparse import parse_time

log = logging.getLogger(__name__)

# числовой id MAX-чата или ссылка/строка, содержащая такой id
_MAX_ID_RE = re.compile(r"-?\d{6,}")


class TelegramSide:
    def __init__(self, bot: Bot, dp: Dispatcher, db: Database, config: Config,
                 max_client: MaxClient | None = None):
        self.bot = bot
        self.db = db
        self.config = config
        self.max_client = max_client  # None — MAX не настроен, регистрация невозможна
        self.collector = MediaGroupCollector(delay=2.0)
        self._bot_id: int | None = None
        self._register(dp)

    # --- регистрация хендлеров ---

    def _register(self, dp: Dispatcher):
        dp.message.register(self.on_start, CommandStart(), F.chat.type == "private")
        dp.message.register(self.on_status, Command("status"), F.chat.type == "private")
        dp.message.register(self.on_disconnect, Command("disconnect"), F.chat.type == "private")
        dp.message.register(self.on_forwarded_post, F.chat.type == "private",
                            F.forward_origin)
        dp.message.register(self.on_private_text, F.chat.type == "private", F.text)
        dp.message.register(self.on_author_reply, F.chat.type == "private")
        dp.channel_post.register(self.on_channel_post)
        dp.edited_channel_post.register(self.on_edited_post)

    # --- команды ---

    async def on_start(self, message: Message):
        await message.answer(
            "Я дублирую посты из Telegram-канала в канал MAX.\n\n"
            "Чтобы подключить свою пару каналов:\n"
            "1. Добавьте этого бота администратором вашего Telegram-канала.\n"
            "2. Добавьте MAX-бота администратором вашего MAX-канала.\n"
            "3. Перешлите мне любой пост из вашего Telegram-канала.\n"
            "4. Пришлите id вашего MAX-канала (число или ссылку).\n\n"
            "Команды: /status — ваши связки, /disconnect — отключить связку.")

    async def on_status(self, message: Message):
        tenants = self.db.tenants_of_user(message.from_user.id)
        if not tenants:
            await message.answer("У вас пока нет связок. Перешлите пост из вашего "
                                 "TG-канала, чтобы подключить (см. /start).")
            return
        lines = ["Ваши связки:"]
        for t in tenants:
            state = "активна" if t["active"] else "отключена"
            lines.append(
                f"• TG-канал {t['tg_channel_id']} → MAX-канал {t['max_channel_id']} "
                f"(задержка {t['delay_minutes']} мин, {state})")
        lines.append("Отключить: /disconnect <tg_channel_id>")
        await message.answer("\n".join(lines))

    async def on_disconnect(self, message: Message):
        tenants = self.db.tenants_of_user(message.from_user.id)
        if not tenants:
            await message.answer("У вас нет связок — отключать нечего.")
            return
        arg = (message.text or "").partition(" ")[2].strip()
        if arg:
            try:
                tg_channel_id = int(arg)
            except ValueError:
                await message.answer("Аргумент — числовой id TG-канала, "
                                     "например: /disconnect -1001234567890")
                return
            tenant = next((t for t in tenants if t["tg_channel_id"] == tg_channel_id),
                          None)
            if not tenant:
                await message.answer("Такой связки у вас нет. Список: /status")
                return
        elif len(tenants) == 1:
            tenant = tenants[0]
        else:
            await message.answer(
                "У вас несколько связок — укажите id TG-канала: "
                "/disconnect <tg_channel_id>. Список: /status")
            return
        self.db.set_tenant_active(tenant["id"], False)
        # ожидающие дубли отключённой связки снимаем с публикации
        for post in self.db.pending_posts_of_channel(tenant["tg_channel_id"]):
            self.db.register_failure(post["id"], "тенант отключён", None)
        await message.answer(
            f"Связка TG {tenant['tg_channel_id']} → MAX {tenant['max_channel_id']} "
            "отключена. Ожидавшие публикации дубли отменены.")

    # --- регистрация связки ---

    async def _my_id(self) -> int:
        if self._bot_id is None:
            self._bot_id = (await self.bot.me()).id
        return self._bot_id

    async def on_forwarded_post(self, message: Message):
        """Пересланный пост из TG-канала — первый шаг регистрации связки."""
        origin = message.forward_origin
        if getattr(origin, "type", None) != "channel":
            return
        channel = origin.chat
        user_id = message.from_user.id
        # защита от угона канала: админами должны быть и бот, и пользователь
        try:
            bot_member = await self.bot.get_chat_member(channel.id, await self._my_id())
            user_member = await self.bot.get_chat_member(channel.id, user_id)
        except Exception as e:
            log.warning("Регистрация: не удалось проверить права в канале %s: %s",
                        channel.id, e)
            await message.answer(
                "Не смог проверить права в этом канале. Убедитесь, что бот — "
                "администратор канала, и перешлите пост ещё раз.")
            return
        admins = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        if bot_member.status not in admins:
            await message.answer("Я не администратор этого канала — добавьте меня "
                                 "в администраторы и перешлите пост ещё раз.")
            return
        if user_member.status not in admins:
            await message.answer("Вы не администратор этого канала — связку может "
                                 "подключить только администратор.")
            return
        existing = self.db.get_tenant_by_tg_channel(channel.id)
        if existing and existing["active"]:
            await message.answer("Этот канал уже подключён. Статус: /status")
            return
        self.db.set_registration_state(user_id, channel.id)
        await message.answer(
            f"Канал «{channel.title or channel.id}» принят. Теперь пришлите id "
            "вашего MAX-канала (число вида -72075083149532 или ссылку на канал).")

    async def on_private_text(self, message: Message):
        """Второй шаг регистрации: id/ссылка MAX-канала (если ждём ввода)."""
        user_id = message.from_user.id
        tg_channel_id = self.db.get_registration_state(user_id)
        if tg_channel_id is None:
            return  # не в процессе регистрации — дальше on_author_reply
        m = _MAX_ID_RE.search(message.text.strip())
        if not m:
            await message.answer("Не понял id MAX-канала. Пришлите число вида "
                                 "-72075083149532 или ссылку на канал.")
            return
        if self.max_client is None:
            await message.answer("MAX не настроен на сервере — регистрация "
                                 "временно невозможна.")
            return
        max_channel_id = m.group(0)
        try:
            await self.max_client.get_chat(max_channel_id)
        except Exception as e:
            log.warning("Регистрация: MAX-канал %s недоступен: %s", max_channel_id, e)
            await message.answer(
                "Не смог открыть этот MAX-канал. Проверьте id и что MAX-бот "
                "добавлен в канал, затем пришлите id ещё раз.")
            return
        existing = self.db.get_tenant_by_tg_channel(tg_channel_id)
        if existing:
            # повторная регистрация (была отключена) — обновляем и включаем
            self.db.update_tenant_max_channel(existing["id"], max_channel_id)
            self.db.set_tenant_active(existing["id"], True)
        else:
            self.db.add_tenant(user_id, tg_channel_id, max_channel_id,
                               self.config.default_delay_minutes,
                               str(self.config.tz))
        self.db.clear_registration_state(user_id)
        await message.answer(
            f"Готово! Связка подключена: TG {tg_channel_id} → MAX {max_channel_id}. "
            "Новые посты будут дублироваться автоматически. Статус: /status")

    # --- владелец ---

    async def on_author_reply(self, message: Message):
        """Ответ (reply) владельца связки на уведомление о дубле — новое время."""
        if not message.reply_to_message or not message.text:
            return
        post = self.db.find_pending_by_notify(message.reply_to_message.message_id)
        if not post:
            return  # реплай не на уведомление о дубле — игнорим
        tenant = self.db.get_tenant_for_post(post["id"])
        if not tenant or message.from_user.id != tenant["tg_user_id"]:
            return  # реплай не от владельца связки — игнорим
        tz = ZoneInfo(tenant["tz"])
        now = datetime.now(tz)
        new_time = parse_time(message.text, now)
        if not new_time:
            await message.answer(
                "Не понял время. Примеры: «18:00», «завтра 15:30», «05.08 12:00».")
            return
        self.db.set_publish_at(post["id"], new_time)
        await message.answer(f"Перенёс: дубль выйдет в {new_time.strftime('%H:%M %d.%m')}.")

    # --- посты канала ---

    def _resolve_tenant(self, chat_id: int) -> dict | None:
        tenant = self.db.get_tenant_by_tg_channel(chat_id)
        if not tenant or not tenant["active"]:
            log.info("Пост из неподключённого/отключённого канала %s — игнор", chat_id)
            return None
        return tenant

    async def on_channel_post(self, message: Message):
        tenant = self._resolve_tenant(message.chat.id)
        if not tenant:
            return
        if message.media_group_id:
            loop = asyncio.get_running_loop()
            self.collector.add(message.media_group_id, message, self._on_album_ready, loop)
            return
        await self._enqueue(tenant, message.message_id, None,
                            snapshot_from_message(message))

    async def _on_album_ready(self, group_id: str, messages: list[Message]):
        messages = sort_album(messages)
        first = messages[0]
        tenant = self._resolve_tenant(first.chat.id)
        if not tenant:
            return
        await self._enqueue(tenant, first.message_id, group_id,
                            snapshot_from_album(messages))

    async def on_edited_post(self, message: Message):
        """Правка до публикации обновляет сохранённый слепок.

        Для альбома правка приходит на ОДНО сообщение группы — обновляем
        только его (текст/медиа этого message_id), а не перезаписываем
        весь альбом одиночным слепком (иначе теряются остальные фото).
        """
        if not self._resolve_tenant(message.chat.id):
            return
        if message.media_group_id:
            post = self.db.get_pending(
                message.chat.id, message.message_id, message.media_group_id)
            if not post:
                return
            content = post["content"]
            snap = snapshot_from_message(message)
            if snap["text"]:
                content["text"] = snap["text"]
                content["entities"] = snap["entities"]
            if snap["media"]:
                edited = snap["media"][0]
                content["media"] = [
                    edited if m.get("message_id") == message.message_id else m
                    for m in content["media"]
                ]
            self.db.update_content(
                message.chat.id, message.message_id, message.media_group_id,
                content)
            log.info("Обновлён слепок альбома %s (правка сообщения %s)",
                     message.media_group_id, message.message_id)
            return
        updated = self.db.update_content(
            message.chat.id, message.message_id, message.media_group_id,
            snapshot_from_message(message))
        if updated:
            log.info("Обновлён слепок поста %s (правка до публикации)", message.message_id)

    async def _enqueue(self, tenant: dict, message_id: int, group_id: str | None,
                       content: dict):
        tz = ZoneInfo(tenant["tz"])
        now = datetime.now(tz)
        publish_at = now + timedelta(minutes=tenant["delay_minutes"])
        chat_id = tenant["tg_channel_id"]
        post_id = self.db.add_post(chat_id, message_id, group_id, content, publish_at)
        if post_id is None:
            log.info("Дубликат поста %s/%s — пропущен", chat_id, message_id)
            return
        log.info("Пост %s/%s в очереди, дубль в %s", chat_id, message_id, publish_at)
        await self._notify_author(tenant, post_id, publish_at)

    async def _notify_author(self, tenant: dict, post_id: int, publish_at: datetime):
        try:
            msg = await self.bot.send_message(
                tenant["tg_user_id"],
                f"Дубль выйдет в {publish_at.strftime('%H:%M %d.%m')} — "
                "ответьте на это сообщение другим временем, чтобы изменить.")
            self.db.set_notify_message_id(post_id, msg.message_id)
        except Exception as e:
            log.warning("Не удалось уведомить владельца связки: %s", e)
