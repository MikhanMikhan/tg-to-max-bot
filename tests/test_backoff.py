from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.backoff import MAX_ATTEMPTS, next_retry_at

NOW = datetime(2026, 8, 4, 14, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def test_first_failure_retries_in_5_min():
    assert next_retry_at(1, NOW) == NOW + timedelta(minutes=5)


def test_second_failure_retries_in_30_min():
    assert next_retry_at(2, NOW) == NOW + timedelta(minutes=30)


def test_attempts_exhausted_returns_none():
    assert next_retry_at(MAX_ATTEMPTS, NOW) is None
    assert next_retry_at(MAX_ATTEMPTS + 1, NOW) is None


def test_delays_are_monotonic():
    delays = [next_retry_at(i, NOW) for i in range(1, MAX_ATTEMPTS)]
    assert delays == sorted(delays)
