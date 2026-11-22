"""Парсер HTML-экспорта Telegram Desktop (messages.html) в посты для backfill.

Telegram Desktop при экспорте без галочки «машиночитаемый JSON» даёт только
messages.html — этот модуль разбирает его stdlib-парсером. Возвращает посты
в унифицированном виде: {"id", "text", "entities", "media": [(max_type,
относительный путь)]}. Сообщения с классом joined приклеиваются к предыдущему
посту (альбомы), служебные (service) пропускаются.

Разметка из HTML конвертируется в entities для render_max_markdown:
<strong> -> bold, <em> -> italic, <u> -> underline, <s> -> strikethrough,
<code>/<pre> -> code, <a href> -> text_link, <br> -> перевод строки.
Вложенные теги дают пересекающиеся entities — внутренние молча деградируют
в plain text (так же, как в основном пайплайне). Оффсеты в кодпоинтах Python,
согласовано с render_max_markdown.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# тег -> тип entity для render_max_markdown
_TAG_ENTITIES = {
    "strong": "bold", "b": "bold",
    "em": "italic", "i": "italic",
    "u": "underline",
    "s": "strikethrough", "del": "strikethrough",
    "code": "code", "pre": "code",
}
# тег -> ключ entity в стеке (a -> text_link)
_TAG_KEY = {**{t: t for t in _TAG_ENTITIES}, "a": "a"}

# класс ссылки на медиа -> тип вложения для MAX
_MEDIA_CLASSES = {
    "photo_wrap": "image",
    "video_file_wrap": "video",
    "animated_wrap": "video",
    "audio_wrap": "audio",
    "document": "file",
    "sticker_wrap": "image",
}

_MSG_ID_RE = re.compile(r"^message(-?\d+)$")


class _ExportParser(HTMLParser):
    """Конечный автомат по messages.html: собирает сообщения по одному."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages: list[dict] = []
        self._msg: dict | None = None   # текущее сообщение
        self._div_depth = 0             # вложенность div внутри сообщения
        self._text_depth = 0            # глубина div class="text" (0 = вне текста)
        # открытые entity: (ключ тега, тип entity, начало, url)
        self._open: list[tuple[str, str, int, str | None]] = []

    @staticmethod
    def _classes(attrs) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    @property
    def _in_text(self) -> bool:
        return self._text_depth > 0

    @property
    def _pos(self) -> int:
        return len(self._msg["text"])

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = self._classes(attrs)

        if tag == "div" and "message" in classes:
            msg_id = _MSG_ID_RE.match(attrs_dict.get("id", ""))
            self._msg = {
                "id": int(msg_id.group(1)) if msg_id else 0,
                "service": "service" in classes,
                "joined": "joined" in classes,
                "text": "",
                "entities": [],
                "media": [],
            }
            self._div_depth = 1
            return
        if self._msg is None:
            return

        if tag == "div":
            self._div_depth += 1
            if "text" in classes and not self._in_text:
                self._text_depth = self._div_depth
            return

        if self._msg["service"]:
            return

        if tag == "a" and not self._in_text:
            media_type = next((t for c, t in _MEDIA_CLASSES.items()
                               if c in classes), None)
            href = attrs_dict.get("href") or ""
            if media_type and href:
                self._msg["media"].append((media_type, href))
            return

        if self._in_text:
            if tag == "br":
                self._msg["text"] += "\n"
            elif tag == "a":
                href = attrs_dict.get("href") or ""
                if href:  # пустые якоря экспорта игнорируем
                    self._open.append(("a", "text_link", self._pos, href))
            elif tag in _TAG_ENTITIES:
                self._open.append((tag, _TAG_ENTITIES[tag], self._pos, None))

    def handle_endtag(self, tag):
        if self._msg is None:
            return
        if tag == "div":
            if self._in_text and self._div_depth == self._text_depth:
                self._text_depth = 0
            self._div_depth -= 1
            if self._div_depth == 0:  # сообщение закончилось
                self.messages.append(self._msg)
                self._msg = None
                self._open.clear()
                self._text_depth = 0
            return
        if self._in_text and tag in _TAG_KEY:
            key = _TAG_KEY[tag]
            for i in range(len(self._open) - 1, -1, -1):
                if self._open[i][0] == key:
                    _, etype, start, url = self._open.pop(i)
                    if self._pos > start:
                        self._msg["entities"].append({
                            "type": etype, "offset": start,
                            "length": self._pos - start, "url": url})
                    break

    def handle_data(self, data):
        if self._msg is not None and self._in_text:
            self._msg["text"] += data


def _strip_text(text: str, entities: list[dict]) -> tuple[str, list[dict]]:
    """Убирает пробельные отступы экспорта по краям, сдвигая оффсеты entities."""
    leading = len(text) - len(text.lstrip())
    stripped = text.strip()
    shifted = []
    for e in entities:
        start = e["offset"] - leading
        end = start + e["length"]
        start, end = max(start, 0), min(end, len(stripped))
        if end > start:
            shifted.append({**e, "offset": start, "length": end - start})
    return stripped, shifted


def parse_posts(export_dir) -> list[dict]:
    """messages.html -> список постов (альбомы склеены) по возрастанию id.

    Пост: {"id", "id_last", "text", "entities",
           "media": [(max_type, rel_path)]}.
    Текст поста — из первого сообщения группы, где он есть (как caption
    альбома в TG).
    """
    parser = _ExportParser()
    with open(export_dir / "messages.html", encoding="utf-8") as f:
        parser.feed(f.read())
    parser.close()

    posts: list[dict] = []
    for msg in parser.messages:
        if msg["service"] or msg["id"] < 0:
            continue
        text, entities = _strip_text(msg["text"], msg["entities"])
        if msg["joined"] and posts:
            post = posts[-1]
            post["media"].extend(msg["media"])
            if not post["text"] and text:
                post["text"], post["entities"] = text, entities
            post["id_last"] = msg["id"]
        else:
            posts.append({
                "id": msg["id"], "id_last": msg["id"],
                "text": text, "entities": entities,
                "media": list(msg["media"]),
            })
    return posts
