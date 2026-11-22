import asyncio
from types import SimpleNamespace

from app.bot.albums import MediaGroupCollector, sort_album


def msg(message_id):
    return SimpleNamespace(message_id=message_id)


def test_sort_album_by_message_id():
    items = [msg(5), msg(2), msg(9)]
    assert [m.message_id for m in sort_album(items)] == [2, 5, 9]


def test_collector_flushes_group_once_with_all_items():
    async def run():
        loop = asyncio.get_running_loop()
        collector = MediaGroupCollector(delay=0.05)
        flushed = []

        async def cb(group_id, items):
            flushed.append((group_id, items))

        collector.add("g1", msg(1), cb, loop)
        collector.add("g1", msg(2), cb, loop)
        collector.add("g1", msg(3), cb, loop)
        await asyncio.sleep(0.2)
        return flushed

    flushed = asyncio.run(run())
    assert len(flushed) == 1
    group_id, items = flushed[0]
    assert group_id == "g1"
    assert [m.message_id for m in items] == [1, 2, 3]


def test_collector_separate_groups_flush_separately():
    async def run():
        loop = asyncio.get_running_loop()
        collector = MediaGroupCollector(delay=0.05)
        flushed = []

        async def cb(group_id, items):
            flushed.append(group_id)

        collector.add("g1", msg(1), cb, loop)
        collector.add("g2", msg(2), cb, loop)
        await asyncio.sleep(0.2)
        return flushed

    assert sorted(asyncio.run(run())) == ["g1", "g2"]


def test_timer_resets_on_new_item():
    async def run():
        loop = asyncio.get_running_loop()
        collector = MediaGroupCollector(delay=0.1)
        flushed = []

        async def cb(group_id, items):
            flushed.append(items)

        collector.add("g1", msg(1), cb, loop)
        await asyncio.sleep(0.07)
        collector.add("g1", msg(2), cb, loop)  # сбрасывает таймер
        await asyncio.sleep(0.07)
        assert not flushed  # ещё не время
        await asyncio.sleep(0.1)
        return flushed

    flushed = asyncio.run(run())
    assert len(flushed) == 1 and len(flushed[0]) == 2
