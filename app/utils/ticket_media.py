"""Normalize ticket galleries for Telegram, preserving legacy single attachments."""

import re
from itertools import groupby

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message


logger = structlog.get_logger(__name__)

# Telegram отвечает так, когда file_id принадлежит другому типу: скриншот,
# который при загрузке не прошёл как фото, хранится файлом, а сообщение
# помечено фото (так сохраняли ответы старые версии админки кабинета).
_WRONG_FILE_TYPE = re.compile(r"can't use file of type (\w+) as", re.IGNORECASE)
_SENDABLE_TYPES = {'photo', 'video', 'document', 'animation'}
_INPUT_MEDIA = {'photo': InputMediaPhoto, 'video': InputMediaVideo, 'document': InputMediaDocument}


def get_ticket_media(message) -> list[dict[str, str]]:
    items = getattr(message, 'media_items', None)
    if not items:
        items = [{'type': getattr(message, 'media_type', None), 'file_id': getattr(message, 'media_file_id', None)}]
    return [
        item
        for item in items
        if isinstance(item, dict) and item.get('type') in {'photo', 'video', 'document'} and item.get('file_id')
    ]


def ticket_media_chunks(items: list[dict[str, str]]):
    # Telegram cannot mix documents with photos/videos in one album. Keep order.
    for is_document, group in groupby(items, key=lambda item: item['type'] == 'document'):
        batch = list(group)
        size = 1 if is_document else 10
        for offset in range(0, len(batch), size):
            yield batch[offset : offset + size]


async def send_ticket_media(bot: Bot, chat_id: int, media_type: str, file_id: str, **kwargs) -> Message:
    """Шлёт вложение тикета; если file_id другого типа — его настоящим типом."""
    try:
        return await getattr(bot, f'send_{media_type}')(chat_id=chat_id, **{media_type: file_id}, **kwargs)
    except TelegramBadRequest as error:
        match = _WRONG_FILE_TYPE.search(str(error))
        actual = match.group(1).lower() if match else None
        if actual not in _SENDABLE_TYPES or actual == media_type:
            raise
        logger.info('Ticket media file_id has another type, resending', stored=media_type, actual=actual)
        return await getattr(bot, f'send_{actual}')(chat_id=chat_id, **{actual: file_id}, **kwargs)


async def send_ticket_attachments(bot: Bot, chat_id: int, attachments: list[dict[str, str]]) -> Message | None:
    """Шлёт вложения альбомами; альбом, который Telegram не принял, — по одному.

    Возвращает последнее отправленное сообщение (под ним кнопка «Удалить»).
    """
    last: Message | None = None
    for chunk in ticket_media_chunks(attachments):
        if len(chunk) > 1:
            try:
                media = [_INPUT_MEDIA[m['type']](media=m['file_id']) for m in chunk]
                messages = await bot.send_media_group(chat_id=chat_id, media=media)
                last = messages[-1] if messages else last
                continue
            except TelegramBadRequest as error:
                logger.info('Ticket album refused, sending one by one', error=str(error))
        for item in chunk:
            try:
                last = await send_ticket_media(bot, chat_id, item['type'], item['file_id'])
            except Exception as error:
                logger.warning('Failed to send ticket attachment', media_type=item['type'], error=str(error))
    return last
