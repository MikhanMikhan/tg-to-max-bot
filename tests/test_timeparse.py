from datetime import datetime
from zoneinfo import ZoneInfo

from app.bot.timeparse import parse_time

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 8, 4, 14, 0, tzinfo=TZ)  # 14:00


def test_plain_time_future_today():
    assert parse_time("18:00", NOW) == datetime(2026, 8, 4, 18, 0, tzinfo=TZ)


def test_plain_time_passed_rolls_to_tomorrow():
    assert parse_time("09:15", NOW) == datetime(2026, 8, 5, 9, 15, tzinfo=TZ)


def test_zavtra():
    assert parse_time("завтра 15:30", NOW) == datetime(2026, 8, 5, 15, 30, tzinfo=TZ)


def test_zavtra_reverse_order():
    assert parse_time("15:30 завтра", NOW) == datetime(2026, 8, 5, 15, 30, tzinfo=TZ)


def test_poslezavtra():
    assert parse_time("послезавтра 8:00", NOW) == datetime(2026, 8, 6, 8, 0, tzinfo=TZ)


def test_explicit_date():
    assert parse_time("10.08 12:00", NOW) == datetime(2026, 8, 10, 12, 0, tzinfo=TZ)


def test_explicit_date_with_year():
    assert parse_time("01.09.2026 07:45", NOW) == datetime(2026, 9, 1, 7, 45, tzinfo=TZ)


def test_past_date_rejected():
    assert parse_time("01.08 10:00", NOW) is None
    assert parse_time("03.08.2026 23:59", NOW) is None


def test_garbage():
    assert parse_time("как-нибудь потом", NOW) is None
    assert parse_time("", NOW) is None


def test_invalid_time():
    assert parse_time("25:00", NOW) is None
    assert parse_time("10:99", NOW) is None


def test_case_insensitive():
    assert parse_time("ЗАВТРА 15:30", NOW) == datetime(2026, 8, 5, 15, 30, tzinfo=TZ)
