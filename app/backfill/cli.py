"""Перенос архива Telegram-канала в MAX из экспорта Telegram Desktop.

Bot API историю канала не отдаёт, а api_id/api_hash не всегда доступны, поэтому
источник — экспорт канала из Telegram Desktop (меню канала -> «Экспорт истории
чата»). Поддерживаются оба формата: машиночитаемый JSON (result.json) и
человекочитаемый HTML (messages.html, app/tgexport_html.py). Публикация идёт
через тот же MaxClient, что и у основного пайплайна; разметка — через
render_max_markdown.

Состояние (id последнего перенесённого поста) хранится в backfill_state.json:
скрипт можно прерывать и запускать снова — продолжит с места остановки.

Запуск:
    .venv/Scripts/python.exe -m app.backfill --export-dir "путь/к/экспорту" \
        [--interval 60] [--limit 10] [--dry-run]

Переменные окружения (.env): MAX_BOT_TOKEN, MAX_CHANNEL_ID — как в основном
приложении; BACKFILL_INTERVAL_MINUTES — интервал между постами (60 по умолч.).
"""
import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None
    import fcntl  # Linux VPS

from dotenv import load_dotenv

from ..core.content import render_max_markdown
from ..core.max_client import MaxClient
from .tgexport_html import parse_posts

log = logging.getLogger(__name__)

STATE_FILE = "backfill_state.json"
SKIP_FILE = "backfill_skip.json"  # id постов, которые не переносить
PID_FILE = "backfill.pid"  # защита от параллельного запуска второй копии
MAX_ALBUM_ITEMS = 12  # лимит вложений MAX на одно сообщение
# шаг ожидания: monotonic-часы на Windows не тикают, пока машина спит,
# поэтому длинную паузу делаем короткими шагами и сверяемся с wall-clock
WAIT_STEP_SEC = 60

# тип text_entity в экспорте -> тип для render_max_markdown
_ENTITY_TYPES = {
    "bold": "bold", "italic": "italic", "underline": "underline",
    "strikethrough": "strikethrough", "code": "code", "pre": "pre",
}


def _text_and_entities(message: dict) -> tuple[str, list[dict]]:
    """Поле text экспорта (строка или массив кусков) -> (текст, entities).

    Оффсеты считаем в кодпоинтах Python — render_max_markdown режет строку
    срезами, так что это согласовано (в отличие от UTF-16-оффсетов Bot API).
    """
    raw = message.get("text") or ""
    if isinstance(raw, str):
        return raw, []
    text_parts, entities, pos = [], [], 0
    for piece in raw:
        if isinstance(piece, str):
            text_parts.append(piece)
            pos += len(piece)
            continue
        chunk = piece.get("text", "")
        text_parts.append(chunk)
        etype = piece.get("type")
        if etype == "text_link" and piece.get("href"):
            entities.append({"type": "text_link", "offset": pos,
                             "length": len(chunk), "url": piece["href"]})
        elif etype in _ENTITY_TYPES:
            entities.append({"type": _ENTITY_TYPES[etype], "offset": pos,
                             "length": len(chunk), "url": None})
        # mention, spoiler, custom_emoji и пр. — молча в plain text
        pos += len(chunk)
    return "".join(text_parts), entities


def _media(message: dict) -> tuple[str, str] | None:
    """(тип для MAX, относительный путь файла в экспорте) или None."""
    if message.get("photo"):
        return "image", message["photo"]
    media_type = message.get("media_type", "")
    mime = message.get("mime_type", "") or ""
    path = message.get("file")
    if not path:
        return None
    if media_type == "voice_message" or mime.startswith("audio/"):
        return "audio", path
    if media_type in ("video_file", "video_message", "animation") \
            or mime.startswith("video/"):
        return "video", path
    return "file", path


def _load_state() -> tuple[int, list[int]]:
    """(id последнего перенесённого поста, отложенные в конец очереди id)."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("last_message_id", 0), data.get("deferred", [])
    except (OSError, ValueError):
        return 0, []


def _load_skip_ids() -> set[int]:
    """id постов, которые не переносить (устаревшие анонсы/акции)."""
    try:
        with open(SKIP_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("skip_ids", []))
    except (OSError, ValueError):
        return set()


def _load_defer_ids() -> set[int]:
    """id постов, которые переносить в конец очереди (сезонные, например
    зимние — чтобы выйти зимой, а не в порядке исходного архива)."""
    try:
        with open(SKIP_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("defer_ids", []))
    except (OSError, ValueError):
        return set()


def _save_state(last_message_id: int, deferred: list[int] | None = None):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_message_id": last_message_id,
                   "deferred": deferred or [],
                   "updated_at": datetime.now(timezone.utc).isoformat()}, f)


_lock_file = None  # удерживаемый handle backfill.pid


def _acquire_pid_lock() -> bool:
    """Блокируем backfill.pid; если копия уже запущена — False.

    Блокировка msvcrt держится, пока открыт файл, и снимается ОС при смерти
    процесса — надёжнее, чем проверка pid (переиспользование pid и т.п.).
    """
    global _lock_file
    f = open(PID_FILE, "a+b")
    try:
        if msvcrt is not None:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Уже запущена другая копия, выхожу")
        f.close()
        return False
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()).encode())
    f.flush()
    _lock_file = f
    return True


def _release_pid_lock():
    if _lock_file is not None:
        _lock_file.close()  # снимает блокировку


async def _wait_until(deadline: datetime):
    """Ждём до deadline (UTC) короткими шагами, сверяясь с wall-clock.

    asyncio.sleep опирается на monotonic-часы, которые на Windows не идут,
    пока машина в сне/гибернации — длинная пауза растягивалась на дни.
    """
    while True:
        left = (deadline - datetime.now(timezone.utc)).total_seconds()
        if left <= 0:
            return
        await asyncio.sleep(min(left, WAIT_STEP_SEC))


def _next_slot_utc(slots: list[tuple[int, int]], tz) -> datetime:
    """Ближайший будущий слот ЧЧ:ММ в зоне tz, возврат в UTC."""
    now = datetime.now(tz)
    for d in range(8):
        day = now.date() + timedelta(days=d)
        for hh, mm in sorted(slots):
            slot = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if slot > now:
                return slot.astimezone(timezone.utc)
    raise RuntimeError("не удалось вычислить слот")


async def _publish_post(export_dir: Path, maxc: MaxClient | None,
                        post: dict) -> bool:
    """Один пост или альбом -> MAX. Возвращает False, если пост пропущен."""
    text, fmt = render_max_markdown(post["text"], post["entities"])

    media = []
    if len(post["media"]) > MAX_ALBUM_ITEMS:
        log.warning("Пост %s: вложений %s > %s, лишние отброшены",
                    post["id"], len(post["media"]), MAX_ALBUM_ITEMS)
    for mtype, rel in post["media"][:MAX_ALBUM_ITEMS]:
        path = export_dir / rel
        try:
            data = path.read_bytes()
        except OSError as e:
            log.warning("Пост %s: не читается файл %s (%s), вложение пропущено",
                        post["id"], path, e)
            continue
        media.append((mtype, data, path.name))

    if not text and not media:
        log.info("Пост %s: непереносимый тип, пропуск", post["id"])
        return False
    if maxc is None:  # dry-run
        log.info("Пост %s: [dry-run] %s символов текста, вложений: %s (%s)",
                 post["id"], len(text), len(media),
                 ", ".join(m[0] for m in media) or "-")
        return True
    await maxc.publish(text, fmt, media)
    log.info("Пост %s опубликован в MAX", post["id"])
    return True


def _json_posts(export_dir: Path) -> list[dict]:
    """result.json -> список унифицированных постов (альбомы по grouped_id)."""
    with open(export_dir / "result.json", encoding="utf-8") as f:
        data = json.load(f)
    messages = sorted(
        (m for m in data.get("messages", []) if m.get("type") == "message"),
        key=lambda m: m["id"])
    posts, album, album_key = [], [], None
    for m in messages:
        key = m.get("grouped_id")
        if album and key != album_key:
            posts.append(_json_group_to_post(album))
            album = []
        album.append(m)
        album_key = key
    if album:
        posts.append(_json_group_to_post(album))
    return posts


def _json_group_to_post(messages: list[dict]) -> dict:
    text, entities = "", []
    for m in messages:
        t, e = _text_and_entities(m)
        if t:
            text, entities = t, e
            break
    media = [item for m in messages if (item := _media(m))]
    return {"id": messages[0]["id"], "id_last": messages[-1]["id"],
            "text": text, "entities": entities, "media": media}


def _iter_posts(export_dir: Path, after_id: int):
    """Посты (одиночные и альбомы) по возрастанию id, после after_id."""
    if (export_dir / "result.json").exists():
        posts = _json_posts(export_dir)
    elif (export_dir / "messages.html").exists():
        posts = parse_posts(export_dir)
    else:
        raise FileNotFoundError(
            f"в {export_dir} нет ни result.json, ни messages.html")
    return (p for p in posts if p["id"] > after_id)


async def main():
    ap = argparse.ArgumentParser(
        description="Перенос экспорта TG-канала (Telegram Desktop) в MAX")
    ap.add_argument("--export-dir", required=True,
                    help="папка экспорта с result.json или messages.html")
    ap.add_argument("--interval", type=float, default=None,
                    help="интервал между постами, минуты "
                         "(по умолчанию BACKFILL_INTERVAL_MINUTES или 60)")
    ap.add_argument("--times", default=None,
                    help="фиксированное время постов, ЧЧ:ММ через "
                         "запятую (напр. 10:00,20:00); переопределяет --interval")
    ap.add_argument("--tz", default=None,
                    help="часовой пояс для --times (напр. Asia/Tomsk); "
                         "по умолчанию TZ из .env, далее системный")
    ap.add_argument("--limit", type=int, default=None,
                    help="максимум постов за запуск")
    ap.add_argument("--dry-run", action="store_true",
                    help="разобрать экспорт и показать посты без публикации "
                         "(MAX_BOT_TOKEN не нужен, состояние не меняется)")
    args = ap.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    export_dir = Path(args.export_dir)
    if args.dry_run:
        maxc = None
    else:
        maxc = MaxClient(os.environ["MAX_BOT_TOKEN"],
                         os.environ["MAX_CHANNEL_ID"])
    interval_min = args.interval if args.interval is not None else float(
        os.environ.get("BACKFILL_INTERVAL_MINUTES", "60"))
    slots = []
    if args.times:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(args.tz) if args.tz else datetime.now().astimezone().tzinfo
        for part in args.times.split(","):
            hh, mm = part.strip().split(":")
            slots.append((int(hh), int(mm)))
        log.info("Режим расписания: посты в %s (%s)", args.times, tz)

    def _deadline() -> datetime:
        if slots:
            return _next_slot_utc(slots, tz)
        return datetime.now(timezone.utc) + timedelta(minutes=interval_min)
    if not args.dry_run and not _acquire_pid_lock():
        return
    last_id, deferred = (0, []) if args.dry_run else _load_state()
    skip_ids = _load_skip_ids()
    if skip_ids:
        log.info("Skip-лист: %s постов будет пропущено", len(skip_ids))
    defer_ids = _load_defer_ids()
    if defer_ids:
        log.info("Defer-лист: %s постов уйдут в конец очереди", len(defer_ids))
    if deferred:
        log.info("Отложенных постов из прошлых запусков: %s", len(deferred))

    published = 0
    try:
        for post in _iter_posts(export_dir, last_id):
            if post["id"] in skip_ids:
                log.info("Пост %s: в skip-листе, пропуск", post["id"])
                if not args.dry_run:
                    last_id = post["id_last"]
                    _save_state(last_id, deferred)
                continue
            if post["id"] in defer_ids:
                log.info("Пост %s: в defer-листе, в конец очереди", post["id"])
                if not args.dry_run:
                    if post["id"] not in deferred:
                        deferred.append(post["id"])
                    last_id = post["id_last"]
                    _save_state(last_id, deferred)
                continue
            # сеть может отваливаться (сон ноутбука и т.п.) — повторяем
            # попытку каждые 10 мин, после 10 неудач пост пропускаем
            ok, failures = False, 0
            while True:
                try:
                    ok = await _publish_post(export_dir, maxc, post)
                    break
                except Exception:
                    failures += 1
                    log.exception("Пост %s: ошибка публикации (попытка %s/10)",
                                  post["id"], failures)
                    if failures >= 10:
                        log.error("Пост %s: 10 неудачных попыток, пропускаю",
                                  post["id"])
                        break
                    await _wait_until(datetime.now(timezone.utc)
                                      + timedelta(minutes=10))
            if not args.dry_run:
                last_id = post["id_last"]
                _save_state(last_id, deferred)
            if not ok:
                continue
            published += 1
            if args.limit is not None and published >= args.limit:
                break
            if not args.dry_run:
                deadline = _deadline()
                log.info("Пауза до %s...", deadline.isoformat(timespec="minutes"))
                await _wait_until(deadline)
        # основная очередь исчерпана — публикуем отложенные (сезонные) посты
        if deferred and (args.limit is None or published < args.limit):
            log.info("Основная очередь закончена, отложенных постов: %s",
                     len(deferred))
            by_id = {p["id"]: p for p in _iter_posts(export_dir, 0)}
            for pid in list(deferred):
                post = by_id.get(pid)
                if post is None or pid in skip_ids:
                    deferred.remove(pid)
                    if not args.dry_run:
                        _save_state(last_id, deferred)
                    continue
                ok, failures = False, 0
                while True:
                    try:
                        ok = await _publish_post(export_dir, maxc, post)
                        break
                    except Exception:
                        failures += 1
                        log.exception(
                            "Отложенный пост %s: ошибка (попытка %s/10)",
                            pid, failures)
                        if failures >= 10:
                            log.error("Отложенный пост %s: 10 неудач, "
                                      "пропускаю", pid)
                            break
                        await _wait_until(datetime.now(timezone.utc)
                                          + timedelta(minutes=10))
                deferred.remove(pid)
                if not args.dry_run:
                    _save_state(last_id, deferred)
                if not ok:
                    continue
                published += 1
                if args.limit is not None and published >= args.limit:
                    break
                if deferred and not args.dry_run:
                    deadline = _deadline()
                    log.info("Пауза до %s...",
                             deadline.isoformat(timespec="minutes"))
                    await _wait_until(deadline)
        log.info("Готово: перенесено постов за запуск: %s", published)
    finally:
        _release_pid_lock()
        if maxc is not None:
            await maxc.close()


if __name__ == "__main__":
    asyncio.run(main())
