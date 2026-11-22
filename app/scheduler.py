"""Планировщик: asyncio-цикл с тиком 30с публикует просроченные pending-записи.
Догон после простоя обеспечивается тем же запросом due_posts при старте."""
import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from .backoff import next_retry_at
from .config import Config
from .content import render_max_markdown
from .db import Database
from .max_client import MaxClient

log = logging.getLogger(__name__)

TICK_SECONDS = 30


class Scheduler:
    def __init__(self, db: Database, config: Config, tg_bot: Bot, max_client: MaxClient | None):
        self.db = db
        self.config = config
        self.tg_bot = tg_bot
        self.max_client = max_client  # None — MAX не настроен (заглушка)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка тика планировщика")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self):
        now = datetime.now(self.config.tz)
        for post in self.db.due_posts(now):
            await self.publish(post)

    async def publish(self, post: dict):
        now = datetime.now(self.config.tz)
        tenant = self.db.get_tenant_for_post(post["id"])
        if not tenant or not tenant["active"]:
            # тенант отключён/удалён — без ретраев, сразу failed
            log.info("Пост #%s: тенант отключён — дубль отменён", post["id"])
            self.db.register_failure(post["id"], "тенант отключён", None)
            if tenant:
                await self._notify_failure(post, "тенант отключён", tenant["tg_user_id"])
            return
        try:
            if self.max_client is None:
                raise RuntimeError("MAX не настроен (пустой MAX_BOT_TOKEN)")
            await self._publish_to_max(post, tenant)
        except Exception as e:
            log.warning("Публикация поста #%s не удалась: %s", post["id"], e)
            next_at = next_retry_at(post["attempts"] + 1, now)
            status = self.db.register_failure(post["id"], str(e)[:500], next_at)
            if status == "failed":
                await self._notify_failure(post, str(e), tenant["tg_user_id"])
            else:
                log.info("Пост #%s: следующая попытка в %s", post["id"], next_at)
        else:
            self.db.mark_published(post["id"])
            log.info("Пост #%s опубликован в MAX (канал %s)",
                     post["id"], tenant["max_channel_id"])

    async def _publish_to_max(self, post: dict, tenant: dict):
        content = post["content"]
        text, fmt = render_max_markdown(content["text"], content.get("entities", []))
        media = []
        for item in content.get("media", []):
            buf = await self.tg_bot.download(item["file_id"])
            media.append((item["type"], buf.read(), item["name"]))
        if not text and not media:
            raise RuntimeError("Пустой пост (непереносимый тип контента)")
        await self.max_client.publish(text, fmt, media,
                                      chat_id=tenant["max_channel_id"])

    async def _notify_failure(self, post: dict, error: str, owner_id: int):
        try:
            await self.tg_bot.send_message(
                owner_id,
                f"Дубль поста {post['message_id']} не удалось опубликовать после "
                f"{post['attempts'] + 1} попыток. Причина: {error[:300]}")
        except Exception as e:
            log.warning("Не удалось уведомить владельца связки о сбое: %s", e)
