"""Клиент MAX Bot API (httpx + REST напрямую, без сторонних SDK).

Документация: https://dev.max.ru/docs-api
- Базовый URL: https://platform-api2.max.ru
- Авторизация: заголовок `Authorization: <token>`
- POST /messages?chat_id=... — отправка сообщения в канал
- POST /uploads?type=... -> {url, token?} -> POST <url> (multipart, поле data) -> {token}
"""
import asyncio
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://platform-api2.max.ru"
# после загрузки файл обрабатывается сервером; иначе attachment.not.ready
PROCESSING_PAUSE_SEC = 1.0

# MAX API использует ГосТЛС-сертификат Минцифры (Russian Trusted CA), которого
# нет в стандартном bundle certifi — используем certs/ca-bundle.pem
# (certifi + russian_trusted_root_ca.cer + russian_trusted_sub_ca.cer,
# https://gu-st.ru/content/Other/doc/...)
_CA_BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "ca-bundle.pem"
_VERIFY = str(_CA_BUNDLE) if _CA_BUNDLE.exists() else True


class MaxApiError(Exception):
    pass


class MaxClient:
    def __init__(self, token: str, channel_id: str | None = None):
        # channel_id — legacy-дефолт для backfill; мультитенантный пайплайн
        # передаёт chat_id в каждый вызов send_message/publish
        self.channel_id = channel_id
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": token},
            timeout=httpx.Timeout(60.0, connect=15.0),
            verify=_VERIFY,
        )
        # отдельный клиент без авторизации: upload-url ведут на внешние
        # хосты (iu.oneme.ru, vu.okcdn.ru...), токен туда не отправляем
        self._upload_http = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=15.0), verify=_VERIFY)

    async def close(self):
        await self._http.aclose()
        await self._upload_http.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        resp = await self._http.request(method, url, **kwargs)
        if resp.status_code != 200:
            raise MaxApiError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}

    async def upload_media(self, media_type: str, data: bytes, filename: str) -> str:
        """Загружает файл, возвращает token для attachments.payload.token."""
        init = await self._request("POST", "/uploads", params={"type": media_type})
        upload_url = init["url"]
        # для video/audio token приходит сразу; для image/file — в ответе на загрузку
        token = init.get("token")
        resp = await self._upload_http.post(
            upload_url,
            files={"data": (filename, data, "application/octet-stream")},
        )
        if resp.status_code != 200:
            raise MaxApiError(f"upload file -> {resp.status_code}: {resp.text[:500]}")
        try:
            result = resp.json() if resp.content else {}
        except ValueError:
            # видео-хост (omub.okcdn.ru) может ответить 200 с пустым/не-JSON
            # телом — token в этом случае уже был выдан на /uploads
            log.info("upload: не-JSON ответ (%s байт), используем init token",
                     len(resp.content))
            result = {}
        # для image ответ вида {"photos": {"<id>": {"token": ...}}}
        if not token:
            for value in result.values():
                if isinstance(value, dict) and value:
                    inner = next(iter(value.values()))
                    if isinstance(inner, dict) and inner.get("token"):
                        token = inner["token"]
                        break
        token = token or result.get("token")
        if not token:
            raise MaxApiError(f"uploads: не получен token (ответ: {result})")
        await asyncio.sleep(PROCESSING_PAUSE_SEC)
        return token

    async def send_message(self, text: str, attachments: list[dict] | None = None,
                           fmt: str | None = None, chat_id: str | None = None) -> dict:
        chat_id = chat_id or self.channel_id
        if not chat_id:
            raise MaxApiError("send_message: не задан chat_id")
        body: dict = {"text": text[:4000] if text else ""}
        if attachments:
            body["attachments"] = attachments
        if fmt:
            body["format"] = fmt
        result = await self._request(
            "POST", "/messages", params={"chat_id": chat_id}, json=body)
        log.info("MAX: сообщение отправлено в канал %s", chat_id)
        return result

    async def publish(self, text: str, fmt: str | None,
                      media: list[tuple[str, bytes, str]],
                      chat_id: str | None = None) -> dict:
        """text + разметка + [(max_type, байты, имя файла)] -> сообщение в канале."""
        attachments = [
            {"type": mtype, "payload": {"token": await self.upload_media(mtype, data, name)}}
            for mtype, data, name in media
        ]
        return await self.send_message(text, attachments or None, fmt, chat_id=chat_id)

    async def get_chat(self, chat_id: str) -> dict:
        """GET /chats/{chat_id} — проверка доступности канала при регистрации."""
        return await self._request("GET", f"/chats/{chat_id}")
