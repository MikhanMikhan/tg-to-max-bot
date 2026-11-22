"""Парсинг времени из ответа автора: «18:00», «завтра 15:30», «15:30 05.08»."""
import re
from datetime import datetime, timedelta

# разделитель — только ':'; точка зарезервирована за датами («10.08»)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")


def parse_time(text: str, now: datetime) -> datetime | None:
    """Возвращает datetime (в tz из `now`) или None, если не распознано.

    Поддерживаемые форматы: «ЧЧ:ММ», «завтра ЧЧ:ММ», «послезавтра ЧЧ:ММ»,
    «ДД.ММ ЧЧ:ММ», «ДД.ММ.ГГГГ ЧЧ:ММ» (в любом порядке).
    Время без даты, уже прошедшее сегодня, переносится на завтра.
    """
    text = text.strip().lower()
    m_time = _TIME_RE.search(text)
    if not m_time:
        return None
    hour, minute = int(m_time.group(1)), int(m_time.group(2))
    if hour > 23 or minute > 59:
        return None

    day = None  # смещение в днях от даты now
    if "послезавтра" in text:
        day = 2
    elif "завтра" in text:
        day = 1
    else:
        m_date = _DATE_RE.search(text)
        if m_date:
            d, mo = int(m_date.group(1)), int(m_date.group(2))
            y = int(m_date.group(3)) if m_date.group(3) else now.year
            if y < 100:
                y += 2000
            try:
                target = datetime(y, mo, d, hour, minute, tzinfo=now.tzinfo)
            except ValueError:
                return None
            if target <= now:
                return None  # дата в прошлом — не переносим молча
            return target

    base = now.date() + timedelta(days=day) if day is not None else None
    if base is None:
        candidate = datetime(now.year, now.month, now.day, hour, minute, tzinfo=now.tzinfo)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return datetime(base.year, base.month, base.day, hour, minute, tzinfo=now.tzinfo)
