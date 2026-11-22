"""Конфигурация из переменных окружения (.env)."""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass
class Config:
    telegram_bot_token: str
    max_bot_token: str
    max_channel_id: str  # legacy: только для миграции существующей БД в тенанта
    default_delay_minutes: int
    tz: ZoneInfo
    db_path: str = "queue.db"

    @property
    def max_enabled(self) -> bool:
        return bool(self.max_bot_token)


def load_config(env_file: str = ".env") -> Config:
    load_dotenv(env_file)
    return Config(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        max_bot_token=os.environ.get("MAX_BOT_TOKEN", ""),
        max_channel_id=os.environ.get("MAX_CHANNEL_ID", ""),
        default_delay_minutes=int(os.environ.get("DEFAULT_DELAY_MINUTES", "60")),
        tz=ZoneInfo(os.environ.get("TZ", "Europe/Moscow")),
        db_path=os.environ.get("DB_PATH", "queue.db"),
    )
