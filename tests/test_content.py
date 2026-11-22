from app.content import render_max_markdown


def ent(type_, offset, length, url=None):
    return {"type": type_, "offset": offset, "length": length, "url": url}


def test_plain_text_no_format():
    assert render_max_markdown("просто текст", []) == ("просто текст", None)


def test_bold():
    text, fmt = render_max_markdown("привет мир", [ent("bold", 7, 3)])
    assert text == "привет **мир**"
    assert fmt == "markdown"


def test_italic_underline_strikethrough():
    text, _ = render_max_markdown("abc def ghi", [
        ent("italic", 0, 3), ent("underline", 4, 3), ent("strikethrough", 8, 3)])
    assert text == "*abc* ++def++ ~~ghi~~"


def test_code_and_link():
    text, fmt = render_max_markdown("run cmd и сайт", [
        ent("code", 4, 3), ent("text_link", 10, 4, url="https://example.com")])
    assert text == "run `cmd` и [сайт](https://example.com)"
    assert fmt == "markdown"


def test_unmapped_entity_silently_dropped():
    assert render_max_markdown("секрет", [ent("spoiler", 0, 6)]) == ("секрет", None)


def test_overlapping_entities_inner_degrades():
    # пересекающиеся: bold [0:5] и italic [3:8] — italic молча отброшен
    text, fmt = render_max_markdown("абвгдежз", [ent("bold", 0, 5), ent("italic", 3, 5)])
    assert text == "**абвгд**ежз"
    assert fmt == "markdown"


def test_out_of_bounds_entity_ignored():
    assert render_max_markdown("короткий", [ent("bold", 0, 100)]) == ("короткий", None)


def test_utf16_offsets_after_astral_emoji():
    # 😎 — 2 единицы UTF-16; без конверсии span съезжал на символ влево
    text, fmt = render_max_markdown("😎 привет мир", [ent("bold", 10, 3)])
    assert text == "😎 привет **мир**"
    assert fmt == "markdown"


def test_bare_url_entity_becomes_link():
    text, fmt = render_max_markdown("сайт чайикот.рф крут", [ent("url", 5, 10)])
    assert text == "сайт [чайикот.рф](https://чайикот.рф) крут"
    assert fmt == "markdown"


def test_bare_url_with_scheme_kept():
    text, _ = render_max_markdown("на https://example.com/x заходи", [
        ent("url", 3, 21)])
    assert text == "на [https://example.com/x](https://example.com/x) заходи"
