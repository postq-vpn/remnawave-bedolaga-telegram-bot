"""Версия VPN-клиента по User-Agent HWID-устройства Remnawave.

Клиент шлёт User-Agent при каждом обновлении подписки, панель хранит последний
у устройства (`userAgent`, рядом `updatedAt`). Happ пишет его так:
``Happ/6.4.0/ios/2611051057585`` — приложение / версия / платформа / сборка.

Сравниваем с МИНИМАЛЬНОЙ поддерживаемой версией (настройки HAPP_MIN_VERSION_*),
а не с последней: иначе после каждого релиза предупреждение висело бы у всех.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


_UA_RE = re.compile(
    r'^\s*(?P<name>[A-Za-z][A-Za-z0-9 ._-]*?)\s*/\s*v?(?P<version>\d+(?:\.\d+){0,3})'
    r'(?:\s*/\s*(?P<platform>[A-Za-z][A-Za-z0-9_-]*))?'
)

_PLATFORM_ALIASES: tuple[tuple[str, str], ...] = (
    # Порядок важен: 'macos' содержит 'os', 'androidtv' — 'android'.
    ('ipados', 'ios'),
    ('iphone', 'ios'),
    ('ipad', 'ios'),
    ('ios', 'ios'),
    ('tvos', 'ios'),
    ('macos', 'macos'),
    ('darwin', 'macos'),
    ('mac', 'macos'),
    ('android', 'android'),
    ('windows', 'windows'),
    ('win', 'windows'),
    ('linux', 'linux'),
)


@dataclass(frozen=True, slots=True)
class ClientApp:
    name: str  # нижний регистр: 'happ'
    version: str  # как прислал клиент: '6.4.0'
    platform: str | None  # нормализованная: ios / android / macos / windows / linux


def normalize_platform(raw: str | None) -> str | None:
    value = (raw or '').strip().lower()
    if not value:
        return None
    for needle, platform in _PLATFORM_ALIASES:
        if needle in value:
            return platform
    return None


def parse_client_user_agent(user_agent: str | None) -> ClientApp | None:
    if not user_agent:
        return None
    match = _UA_RE.match(user_agent)
    if not match:
        return None
    return ClientApp(
        name=match.group('name').strip().lower(),
        version=match.group('version'),
        platform=normalize_platform(match.group('platform')),
    )


def parse_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = re.match(r'^\s*v?(\d+(?:\.\d+){0,3})', value)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split('.'))


def is_version_older(version: str | None, minimum: str | None) -> bool:
    """True, только если обе версии разобрались и ``version < minimum``."""
    current = parse_version(version)
    required = parse_version(minimum)
    if current is None or required is None:
        return False
    width = max(len(current), len(required))
    return current + (0,) * (width - len(current)) < required + (0,) * (width - len(required))


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def describe_device_client(device: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Поля о клиенте для ответа `/cabinet/subscription/devices`.

    ``app_outdated`` ставим только Happ-устройствам, у которых задана минимальная
    версия для их платформы и которые обновляли подписку за последние
    HAPP_OUTDATED_ACTIVE_DAYS дней: давно молчащим устройствам незачем пугать.
    """
    from app.config import settings

    app = parse_client_user_agent(device.get('userAgent'))
    platform = (app.platform if app else None) or normalize_platform(device.get('platform'))

    result: dict[str, Any] = {
        'app_name': app.name if app else None,
        'app_version': app.version if app else None,
        'min_app_version': None,
        'app_outdated': False,
        'update_url': None,
    }
    if not app or app.name != 'happ' or not platform:
        return result

    minimum = settings.get_happ_min_version(platform)
    if not minimum:
        return result
    result['min_app_version'] = minimum

    if not is_version_older(app.version, minimum):
        return result

    active_days = settings.HAPP_OUTDATED_ACTIVE_DAYS or 0
    if active_days > 0:
        seen_at = _parse_timestamp(device.get('updatedAt') or device.get('createdAt'))
        current = now or datetime.now(UTC)
        if seen_at is not None and current - seen_at > timedelta(days=active_days):
            return result

    result['app_outdated'] = True
    result['update_url'] = settings.get_happ_download_link(platform)
    return result
