"""Telegram-уведомление «обновите VPN-клиент» для устройств со старым Happ.

Два входа:

* ``notify_outdated_devices`` — для конкретного пользователя и его устройств;
  вызывается из вебхука ``user_hwid_devices.added`` (новое устройство сразу
  подключилось со старой версией).
* ``sweep_outdated_clients`` — раз в сутки из цикла мониторинга обходит все
  HWID-устройства панели. Вебхука на «устройство обновило подписку» у Remnawave
  нет, поэтому уже подключённые устройства со старой версией ловит только обход.

Решение «устарело ли устройство» — то же, что в кабинете
(``describe_device_client``): Happ, версия ниже HAPP_MIN_VERSION*, устройство
забирало подписку за последние HAPP_OUTDATED_ACTIVE_DAYS дней.

Повторы гасит Redis: ключ на (пользователь, устройство, версия) живёт
HAPP_OUTDATED_REMIND_DAYS дней — пока человек не обновится, напоминание придёт
не чаще этого. Обновился (версия сменилась) — ключ другой, но устройство уже не
устаревшее, и писать нечего.
"""

from __future__ import annotations

import html
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.localization.texts import get_texts
from app.services.notification_types import NotificationType
from app.utils.cache import cache
from app.utils.client_app_version import describe_device_client, normalize_platform, parse_client_user_agent


if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


_PLATFORM_LABELS = {
    'ios': 'iOS',
    'android': 'Android',
    'macos': 'macOS',
    'windows': 'Windows',
    'linux': 'Linux',
}
_APPLE_PLATFORMS = frozenset({'ios', 'macos'})

SWEEP_LOCK_KEY = 'client_outdated:sweep'
# Чуть меньше суток: цикл мониторинга идёт раз в N минут, обход сдвигается
# максимум на один интервал, а не «уползает» на сутки вперёд каждый день.
SWEEP_INTERVAL_SECONDS = 23 * 3600


def is_enabled() -> bool:
    """Уведомления включены и хоть одна минимальная версия задана."""
    if not settings.HAPP_OUTDATED_NOTIFY_ENABLED:
        return False
    return any(settings.get_happ_min_version(p) for p in _PLATFORM_LABELS)


def _device_platform(device: dict[str, Any]) -> str | None:
    app = parse_client_user_agent(device.get('userAgent'))
    return (app.platform if app else None) or normalize_platform(device.get('platform'))


def _device_name(device: dict[str, Any]) -> str:
    raw = (device.get('deviceModel') or device.get('platform') or '').strip()
    return html.escape(raw) if raw else '—'


def _app_label(name: str | None) -> str:
    return (name or 'VPN').capitalize()


def build_message(
    language: str, outdated: list[tuple[dict[str, Any], dict[str, Any]]]
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Текст и клавиатура уведомления. ``outdated`` — пары (устройство панели, describe_device_client)."""
    if not outdated:
        return None
    texts = get_texts(language)
    app = _app_label(outdated[0][1]['app_name'])

    if len(outdated) == 1:
        device, info = outdated[0]
        template = texts.get('CLIENT_OUTDATED_SINGLE')
        if not template:
            return None
        message = template.format(
            app=app,
            device=_device_name(device),
            current=html.escape(info['app_version'] or ''),
            version=html.escape(info['min_app_version'] or ''),
        )
    else:
        template = texts.get('CLIENT_OUTDATED_MULTI')
        line = texts.get('CLIENT_OUTDATED_DEVICE_LINE')
        if not template or not line:
            return None
        lines = '\n'.join(
            line.format(
                device=_device_name(device),
                app=_app_label(info['app_name']),
                current=html.escape(info['app_version'] or ''),
                version=html.escape(info['min_app_version'] or ''),
            )
            for device, info in outdated
        )
        message = template.format(app=app, devices=lines)

    # Кнопки: обновление (по платформе, если их несколько), регион App Store
    # для Apple, поддержка, закрыть.
    rows: list[list[InlineKeyboardButton]] = []
    links: dict[str, str] = {}
    for device, info in outdated:
        platform = _device_platform(device)
        if platform and info.get('update_url') and platform not in links:
            links[platform] = info['update_url']
    if len(links) == 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.get('CLIENT_OUTDATED_UPDATE_BUTTON', '⬆️ Update {app}').format(app=app),
                    url=next(iter(links.values())),
                )
            ]
        )
    else:
        for platform, url in links.items():
            rows.append(
                [
                    InlineKeyboardButton(
                        text=texts.get('CLIENT_OUTDATED_UPDATE_PLATFORM_BUTTON', '⬆️ {app} for {platform}').format(
                            app=app, platform=_PLATFORM_LABELS.get(platform, platform)
                        ),
                        url=url,
                    )
                ]
            )

    has_apple = any(_device_platform(device) in _APPLE_PLATFORMS for device, _ in outdated)
    region_url = (settings.HAPP_APP_STORE_REGION_HELP_URL or '').strip()
    if has_apple and region_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.get('CLIENT_OUTDATED_REGION_BUTTON', '🌍 Change App Store region'),
                    url=region_url,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=texts.get('MENU_SUPPORT', '🛠️ Support'), callback_data='menu_support')])
    rows.append(
        [InlineKeyboardButton(text=texts.get('WEBHOOK_CLOSE_BUTTON', '✖️ Закрыть'), callback_data='webhook:close')]
    )
    return message, InlineKeyboardMarkup(inline_keyboard=rows)


def _dedup_key(user_id: int, device: dict[str, Any], info: dict[str, Any]) -> str:
    hwid = device.get('hwid') or device.get('deviceId') or device.get('id') or ''
    return f'client_outdated:{user_id}:{hwid}:{info.get("app_version") or ""}'


async def notify_outdated_devices(bot: Bot | None, user: Any, devices: list[dict[str, Any]]) -> bool:
    """Отправить пользователю одно сообщение про его устаревшие устройства.

    Только Telegram: у email-only пользователей то же предупреждение есть в
    кабинете (баннер на главной), отдельное письмо не шлём.
    """
    if bot is None or not is_enabled() or not getattr(user, 'telegram_id', None):
        return False

    outdated: list[tuple[dict[str, Any], dict[str, Any]]] = []
    claimed: list[str] = []
    ttl = max(1, int(settings.HAPP_OUTDATED_REMIND_DAYS or 7)) * 86400
    for device in devices:
        info = describe_device_client(device)
        if not info['app_outdated']:
            continue
        key = _dedup_key(user.id, device, info)
        if not await cache.setnx(key, 1, expire=ttl):
            continue  # уже напоминали про эту версию на этом устройстве
        claimed.append(key)
        outdated.append((device, info))

    built = build_message(getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE, outdated)
    if built is None:
        return False
    message, markup = built

    from app.services.notification_delivery_service import notification_delivery_service

    sent = False
    try:
        sent = await notification_delivery_service.send_notification(
            user=user,
            notification_type=NotificationType.CLIENT_OUTDATED,
            context={'text_key': 'CLIENT_OUTDATED', 'devices': len(outdated)},
            bot=bot,
            telegram_message=message,
            telegram_markup=markup,
        )
    except Exception:
        logger.exception('Client-outdated notification failed', user_id=user.id)

    if not sent:
        # Не дошло — отпускаем ключи, чтобы следующий обход попробовал снова.
        for key in claimed:
            await cache.delete(key)
        return False

    logger.info('Client-outdated notification sent', user_id=user.id, devices=len(outdated))
    return True


async def sweep_outdated_clients(db: AsyncSession, bot: Bot | None) -> int:
    """Раз в сутки: обойти все HWID-устройства панели и напомнить владельцам устаревших.

    Возвращает число пользователей, которым ушло уведомление.
    """
    if bot is None or not is_enabled():
        return 0
    # Замок на сутки: без Redis setnx отдаёт False — обход просто не идёт, а не
    # спамит каждый цикл мониторинга.
    if not await cache.setnx(SWEEP_LOCK_KEY, 1, expire=SWEEP_INTERVAL_SECONDS):
        return 0

    from app.database.crud.user import get_user_by_remnawave_id
    from app.services.remnawave_service import RemnaWaveService

    try:
        async with RemnaWaveService().get_api_client() as api:
            response = await api.get_all_hwid_devices()
    except Exception as error:
        await cache.delete(SWEEP_LOCK_KEY)  # панель недоступна — попробуем в следующем цикле
        logger.warning('Client-outdated sweep: panel unavailable', error=str(error)[:200])
        return 0

    by_panel_user: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for device in response.get('devices', []):
        panel_user_id = device.get('userId')
        if isinstance(panel_user_id, int) and describe_device_client(device)['app_outdated']:
            by_panel_user[panel_user_id].append(device)

    notified = 0
    for panel_user_id, devices in by_panel_user.items():
        try:
            user = await get_user_by_remnawave_id(db, panel_user_id)
            if user is None:
                continue
            if await notify_outdated_devices(bot, user, devices):
                notified += 1
        except Exception:
            logger.exception('Client-outdated sweep: user failed', panel_user_id=panel_user_id)

    logger.info(
        'Client-outdated sweep finished',
        outdated_users=len(by_panel_user),
        notified=notified,
    )
    return notified
