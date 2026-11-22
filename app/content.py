"""Слепок контента поста Telegram и маппинг разметки TG entities -> MAX markdown.
Непереносимое (опросы, inline-кнопки, спойлеры и т.п.) молча отбрасывается."""
from aiogram.types import Message

# TG entity type -> (открывающий, закрывающий) маркер MAX markdown
_ENTITY_MARKERS = {
    "bold": ("**", "**"),
    "italic": ("*", "*"),
    "underline": ("++", "++"),
    "strikethrough": ("~~", "~~"),
    "code": ("`", "`"),
    "pre": ("`", "`"),
}


def media_from_message(message: Message) -> dict | None:
    """Один медиа-элемент сообщения: тип для MAX + file_id из Telegram."""
    if message.photo:
        p = message.photo[-1]  # самое большое превью
        return {"type": "image", "file_id": p.file_id, "name": "photo.jpg",
                "message_id": message.message_id}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id,
                "name": message.video.file_name or "video.mp4",
                "message_id": message.message_id}
    if message.audio:
        return {"type": "audio", "file_id": message.audio.file_id,
                "name": message.audio.file_name or "audio.mp3",
                "message_id": message.message_id}
    if message.voice:
        return {"type": "audio", "file_id": message.voice.file_id,
                "name": "voice.ogg", "message_id": message.message_id}
    if message.document:
        return {"type": "file", "file_id": message.document.file_id,
                "name": message.document.file_name or "file",
                "message_id": message.message_id}
    return None


def snapshot_from_message(message: Message) -> dict:
    """Слепок одного поста (не альбома)."""
    entities = [
        {"type": e.type, "offset": e.offset, "length": e.length, "url": getattr(e, "url", None)}
        for e in (message.entities or message.caption_entities or [])
    ]
    media = media_from_message(message)
    return {
        "text": message.text or message.caption or "",
        "entities": entities,
        "media": [media] if media else [],
        "media_group_id": message.media_group_id,
    }


def snapshot_from_album(messages: list[Message]) -> dict:
    """Слепок альбома: caption берём из первого сообщения, где он есть."""
    text, entities = "", []
    for m in messages:
        if m.caption:
            text = m.caption
            entities = [
                {"type": e.type, "offset": e.offset, "length": e.length,
                 "url": getattr(e, "url", None)}
                for e in (m.caption_entities or [])
            ]
            break
    media = [media_from_message(m) for m in messages]
    return {
        "text": text,
        "entities": entities,
        "media": [m for m in media if m],
        "media_group_id": messages[0].media_group_id,
    }


def _u16_to_py(text: str, u16_offset: int) -> int:
    """Оффсеты TG-entities — в кодовых единицах UTF-16; переводим в индекс
    Python-строки (astral-символы, напр. эмодзи 😎, считаются там за 2)."""
    return len(text.encode("utf-16-le")[: u16_offset * 2].decode("utf-16-le"))


def render_max_markdown(text: str, entities: list[dict]) -> tuple[str, str | None]:
    """(текст, format). format=None — разметки нет, отправлять как plain text.
    Перекрывающиеся entity MAX-markdown не выражают — внутренние молча
    деградируют до plain text."""
    if not text or not entities:
        return text, None

    mapped = []
    u16_len = len(text.encode("utf-16-le")) // 2
    for e in sorted(entities, key=lambda x: x["offset"]):
        if e["offset"] + e["length"] > u16_len:
            continue
        start = _u16_to_py(text, e["offset"])
        end = _u16_to_py(text, e["offset"] + e["length"])
        if start >= end:
            continue
        # пробелы по краям span (часто попадают в ссылки в экспорте) — за маркеры
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            continue
        if e["type"] == "text_link" and e.get("url"):
            mapped.append((start, end, "link", e["url"]))
        elif e["type"] == "url":
            # голая ссылка из текста: URL — сам span; без схемы дописываем https://
            mapped.append((start, end, "url", None))
        elif e["type"] in _ENTITY_MARKERS:
            mapped.append((start, end, "marker", _ENTITY_MARKERS[e["type"]]))
        # spoiler, mention, custom_emoji и пр. — молча в plain text

    # отбрасываем пересекающиеся (оставляем более ранние)
    accepted, last_end = [], -1
    for item in mapped:
        if item[0] >= last_end:
            accepted.append(item)
            last_end = item[1]

    if not accepted:
        return text, None

    out, pos = [], 0
    for start, end, kind, payload in accepted:
        out.append(text[pos:start])
        inner = text[start:end]
        if kind == "link":
            out.append(f"[{inner}]({payload})")
        elif kind == "url":
            href = inner if "://" in inner else f"https://{inner}"
            out.append(f"[{inner}]({href})")
        else:
            open_m, close_m = payload
            out.append(f"{open_m}{inner}{close_m}")
        pos = end
    out.append(text[pos:])
    return "".join(out), "markdown"
