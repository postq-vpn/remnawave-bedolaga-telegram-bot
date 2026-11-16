"""A photo/video Telegram refuses as such is staged as a document, not a 500."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.cabinet.routes import media
from app.config import settings


def _setup(monkeypatch, refusal: str):
    monkeypatch.setattr(type(settings), 'get_admin_notifications_chat_id', lambda _: -100123)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
    stored = SimpleNamespace(file_id='doc_file_id', file_unique_id='u')
    calls: list[str] = []

    async def refuse(**kwargs):
        calls.append('media')
        raise TelegramBadRequest(method=SendMessage(chat_id=-100123, text='x'), message=refusal)

    async def send_document(**kwargs):
        calls.append('document')
        return SimpleNamespace(message_id=7, document=stored)

    bot = SimpleNamespace(
        send_photo=refuse,
        send_video=refuse,
        send_document=send_document,
        delete_message=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(media, 'create_bot', lambda: bot)
    monkeypatch.setattr(media, '_build_media_url', lambda request, file_id: f'/media/{file_id}')
    return calls


def _file(name: str, content_type: str) -> UploadFile:
    return UploadFile(filename=name, file=BytesIO(b'bytes'), headers=Headers({'content-type': content_type}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('media_type', 'name', 'content_type', 'refusal'),
    [
        ('photo', 'long.png', 'image/png', 'Bad Request: PHOTO_INVALID_DIMENSIONS'),
        ('photo', 'big.jpg', 'image/jpeg', 'Bad Request: IMAGE_PROCESS_FAILED'),
        ('video', 'clip.webm', 'video/webm', 'Bad Request: wrong file identifier/HTTP URL specified'),
    ],
)
async def test_refused_media_is_staged_as_document(monkeypatch, media_type, name, content_type, refusal):
    calls = _setup(monkeypatch, refusal)

    result = await media.upload_media(
        request=SimpleNamespace(),
        user=SimpleNamespace(telegram_id=1),
        file=_file(name, content_type),
        media_type=media_type,
    )

    assert calls == ['media', 'document']
    assert result.media_type == 'document'
    assert result.file_id == 'doc_file_id'


@pytest.mark.asyncio
async def test_unrelated_refusal_is_not_masked(monkeypatch):
    calls = _setup(monkeypatch, 'Bad Request: TOPIC_CLOSED')

    with pytest.raises(HTTPException) as error:
        await media.upload_media(
            request=SimpleNamespace(),
            user=SimpleNamespace(telegram_id=1),
            file=_file('a.png', 'image/png'),
            media_type='photo',
        )

    assert error.value.status_code == 500
    assert calls == ['media']
