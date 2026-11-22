"""Интервалы повторных попыток публикации: 5 мин -> 30 мин -> 2 ч."""
from datetime import datetime, timedelta

MAX_ATTEMPTS = 3
BACKOFF_DELAYS = [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2)]


def next_retry_at(failed_attempts: int, now: datetime) -> datetime | None:
    """Время следующей попытки после failed_attempts-й неудачи (1-based).
    None — попытки исчерпаны, переводить в failed."""
    if failed_attempts >= MAX_ATTEMPTS:
        return None
    return now + BACKOFF_DELAYS[failed_attempts - 1]
