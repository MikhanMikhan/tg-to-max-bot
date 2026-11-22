"""Точка входа: python -m app.main"""
import asyncio
import logging

from aiogram import Bot, Dispatcher

from .config import load_config
from .db import Database
from ..core.max_client import MaxClient
from .scheduler import Scheduler
from .telegram_side import TelegramSide
from .tg_session import TelegramSession


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler("main.log", encoding="utf-8"),
                  logging.StreamHandler()])
    log = logging.getLogger(__name__)

    config = load_config()
    if not config.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан — заполните .env")

    db = Database(config.db_path)
    if config.max_channel_id:
        # одноканальная конфигурация -> тенант (идемпотентно)
        db.migrate_legacy_tenant(config.max_channel_id,
                                 config.default_delay_minutes, str(config.tz))
    bot = Bot(config.telegram_bot_token, session=TelegramSession())
    dp = Dispatcher()

    max_client = None
    if config.max_enabled:
        max_client = MaxClient(config.max_bot_token)
    else:
        log.warning("MAX_BOT_TOKEN не задан — регистрация связок и публикация в MAX "
                    "отключены; дубли будут падать в failed с уведомлением владельцу")

    TelegramSide(bot, dp, db, config, max_client)
    scheduler = Scheduler(db, config, bot, max_client)
    scheduler_task = asyncio.create_task(scheduler.run())

    log.info("Запуск long-polling Telegram...")
    try:
        # drop_pending_updates=False: пропущенные за простой посты догоняются
        # через getUpdates; дубликаты отсекаются UNIQUE-ключом в БД
        await dp.start_polling(bot, drop_pending_updates=False)
    finally:
        scheduler_task.cancel()
        await bot.session.close()
        if max_client:
            await max_client.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
