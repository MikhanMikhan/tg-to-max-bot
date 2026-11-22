"""Сборка альбомов: Telegram присылает media_group россыпью сообщений
с общим media_group_id. Ждём `delay` после последнего сообщения группы
и передаём собранную группу в callback один раз."""
import asyncio
from collections.abc import Awaitable, Callable

AlbumCallback = Callable[[str, list], Awaitable[None]]


class MediaGroupCollector:
    def __init__(self, delay: float = 2.0):
        self.delay = delay
        self._groups: dict[str, list] = {}
        self._handles: dict[str, asyncio.TimerHandle] = {}

    def add(self, group_id: str, item, callback: AlbumCallback, loop: asyncio.AbstractEventLoop):
        self._groups.setdefault(group_id, []).append(item)
        if group_id in self._handles:
            self._handles[group_id].cancel()
        self._handles[group_id] = loop.call_later(
            self.delay, lambda: asyncio.ensure_future(self._flush(group_id, callback)))

    async def _flush(self, group_id: str, callback: AlbumCallback):
        items = self._groups.pop(group_id, [])
        self._handles.pop(group_id, None)
        if items:
            await callback(group_id, items)


def sort_album(items: list) -> list:
    """Стабильный порядок альбома — по message_id."""
    return sorted(items, key=lambda m: m.message_id)
