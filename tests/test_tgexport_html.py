"""Тесты парсера HTML-экспорта Telegram Desktop (app/tgexport_html.py)."""
import pytest

from app.backfill.tgexport_html import parse_posts


def _write_export(tmp_path, body: str):
    (tmp_path / "messages.html").write_text(
        f'<html><body><div class="page_wrap">{body}</div></body></html>',
        encoding="utf-8")
    return tmp_path


def _msg(msg_id, inner, cls="message default clearfix"):
    return (f'<div class="{cls}" id="message{msg_id}">'
            f'<div class="body">{inner}</div></div>')


DATE = ('<div class="pull_right date details" '
        'title="10.11.2025 15:31:37 UTC+07:00">15:31</div>')


def test_text_with_formatting_and_link(tmp_path):
    body = _msg(1, DATE + '<div class="text">Привет, <strong>мир</strong>!'
                          '<br>Магазин <a href="https://vk.com/x">Чай и кот'
                          '</a> &laquo;ok&raquo;</div>')
    posts = parse_posts(_write_export(tmp_path, body))
    assert len(posts) == 1
    p = posts[0]
    assert p["id"] == 1
    assert p["text"] == "Привет, мир!\nМагазин Чай и кот «ok»"
    assert p["media"] == []
    ents = {(e["type"], e["offset"], e["length"], e["url"])
            for e in p["entities"]}
    bold_off = p["text"].index("мир")
    link_off = p["text"].index("Чай и кот")
    assert ("bold", bold_off, 3, None) in ents
    assert ("text_link", link_off, len("Чай и кот"), "https://vk.com/x") in ents


def test_photo_message(tmp_path):
    body = _msg(2, DATE + '<div class="media_wrap clearfix">'
                          '<a class="photo_wrap clearfix pull_left" '
                          'href="photos/photo_2.jpg">'
                          '<img class="photo" src="photos/photo_2_thumb.jpg"/>'
                          '</a></div>'
                          '<div class="text">подпись</div>')
    posts = parse_posts(_write_export(tmp_path, body))
    assert posts[0]["media"] == [("image", "photos/photo_2.jpg")]
    assert posts[0]["text"] == "подпись"


def test_album_joined(tmp_path):
    first = _msg(3, DATE + '<div class="media_wrap clearfix">'
                           '<a class="photo_wrap clearfix pull_left" '
                           'href="photos/a.jpg"></a></div>'
                           '<div class="text">альбом</div>')
    joined = _msg(4, DATE + '<div class="media_wrap clearfix">'
                            '<a class="photo_wrap clearfix pull_left" '
                            'href="photos/b.jpg"></a></div>',
                  cls="message default clearfix joined")
    posts = parse_posts(_write_export(tmp_path, first + joined))
    assert len(posts) == 1
    p = posts[0]
    assert (p["id"], p["id_last"]) == (3, 4)
    assert p["media"] == [("image", "photos/a.jpg"), ("image", "photos/b.jpg")]
    assert p["text"] == "альбом"


def test_video_and_service_skipped(tmp_path):
    service = _msg(-1, "10 November 2025", cls="message service")
    created = _msg(1, "Channel created", cls="message service")
    video = _msg(5, DATE + '<div class="media_wrap clearfix">'
                           '<a class="video_file_wrap clearfix pull_left" '
                           'href="video_files/v.mp4">'
                           '<img class="video_file" src="x_thumb.jpg"/></a>'
                           '</div>')
    posts = parse_posts(_write_export(tmp_path, service + created + video))
    assert len(posts) == 1
    assert posts[0]["media"] == [("video", "video_files/v.mp4")]


def test_empty_anchor_ignored(tmp_path):
    body = _msg(6, DATE + '<div class="text">текст<a href="">якорь</a></div>')
    posts = parse_posts(_write_export(tmp_path, body))
    assert posts[0]["entities"] == []
    assert posts[0]["text"] == "текстякорь"


def test_nested_formatting_entities(tmp_path):
    body = _msg(7, DATE + '<div class="text"><strong>жирный <em>курсив</em>'
                          '</strong></div>')
    posts = parse_posts(_write_export(tmp_path, body))
    ents = {(e["type"], e["offset"], e["length"]) for e in posts[0]["entities"]}
    assert ("italic", 7, 6) in ents
    assert ("bold", 0, 13) in ents


def test_export_indentation_stripped_offsets_shifted(tmp_path):
    # в реальном экспорте текст внутри div идёт с перевода строки и отступа —
    # оффсеты entities должны сдвинуться вместе с обрезкой
    body = _msg(8, DATE + '<div class="text">\n       Привет '
                          '<a href="https://vk.com/x">Чай и кот</a> 😊\n       '
                          '</div>')
    posts = parse_posts(_write_export(tmp_path, body))
    p = posts[0]
    assert p["text"] == "Привет Чай и кот 😊"
    (e,) = p["entities"]
    assert e["type"] == "text_link"
    assert p["text"][e["offset"]:e["offset"] + e["length"]] == "Чай и кот"
