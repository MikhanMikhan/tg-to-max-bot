"""Сессия aiogram с резервными IP для api.telegram.org.

Часть IP Telegram у провайдера может дропаться по TCP (таймаут connect,
при этом DNS резолвится нормально). Резолвер ниже возвращает обычные
DNS-адреса плюс проверенные рабочие IP — aiohttp перебирает их по очереди,
пока connect не удастся. Когда сеть чинится, работает обычный DNS-путь.
"""
import socket

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession

# проверенный рабочий IP api.telegram.org (149.154.167.0/24 — диапазон TG)
_FALLBACK_IPS = {
    "api.telegram.org": ["149.154.167.220"],
}


class _FallbackResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0,
                      family: int = socket.AF_INET) -> list[dict]:
        try:
            results = await self._inner.resolve(host, port, family)
        except OSError:
            results = []
        known = {r["host"] for r in results}
        for ip in _FALLBACK_IPS.get(host, ()):
            if ip not in known:
                results.append({"hostname": host, "host": ip, "port": port,
                                "family": family, "proto": 0, "flags": 0})
        if not results:
            raise OSError(f"DNS: {host} не резолвится, резервных IP нет")
        return results

    async def close(self):
        await self._inner.close()


class TelegramSession(AiohttpSession):
    """AiohttpSession с _FallbackResolver в коннекторе."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._connector_init["resolver"] = _FallbackResolver()
