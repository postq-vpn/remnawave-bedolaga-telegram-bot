"""Вложение тикета, чей file_id другого типа (скриншот, сохранённый файлом, но помеченный фото)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.utils.ticket_media import send_ticket_attachments, send_ticket_media


def _wrong_type(actual: str, wanted: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=f"Bad Request: can't use file of type {actual} as {wanted}")


def _bot() -> MagicMock:
    bot = MagicMock()
    for name in ('send_photo', 'send_video', 'send_document', 'send_media_group'):
        setattr(bot, name, AsyncMock(return_value=MagicMock(name=name)))
    return bot


async def test_photo_that_is_a_document_is_resent_as_document():
    bot = _bot()
    bot.send_photo.side_effect = _wrong_type('Document', 'Photo')

    await send_ticket_media(bot, 1, 'photo', 'FILE', caption='hi')

    bot.send_document.assert_awaited_once_with(chat_id=1, document='FILE', caption='hi')


async def test_other_bad_request_is_raised():
    bot = _bot()
    bot.send_photo.side_effect = TelegramBadRequest(method=MagicMock(), message='Bad Request: chat not found')

    with pytest.raises(TelegramBadRequest):
        await send_ticket_media(bot, 1, 'photo', 'FILE')
    bot.send_document.assert_not_awaited()


async def test_refused_album_is_sent_one_by_one():
    bot = _bot()
    bot.send_media_group.side_effect = _wrong_type('Document', 'Photo')
    bot.send_photo.side_effect = [_wrong_type('Document', 'Photo'), MagicMock(), MagicMock()]
    items = [{'type': 'photo', 'file_id': f'F{i}'} for i in range(3)]

    last = await send_ticket_attachments(bot, 1, items)

    assert bot.send_photo.await_count == 3
    bot.send_document.assert_awaited_once_with(chat_id=1, document='F0')
    assert last is not None
