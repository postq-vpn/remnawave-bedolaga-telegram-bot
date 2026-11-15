"""Telegram-уведомление «обновите Happ»: текст, кнопки, повторы, кто получает."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import client_version_notifier as notifier


RECENT = (datetime.now(UTC) - timedelta(days=1)).isoformat()


def _device(model, platform, ua, hwid=None):
    return {
        'hwid': hwid or model,
        'deviceModel': model,
        'platform': platform,
        'userAgent': ua,
        'updatedAt': RECENT,
    }


IPHONE = _device('iPhone 15', 'iOS', 'Happ/5.7.2/ios/1')
PIXEL = _device('Pixel 8', 'Android', 'Happ/5.6.0/android/1')
FRESH_MAC = _device('MacBook Air', 'macOS', 'Happ/6.4.0/macos/1')


@pytest.fixture(autouse=True)
def happ_settings(monkeypatch):
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION', '5.8.1')
    for platform in ('IOS', 'ANDROID', 'MACOS', 'WINDOWS', 'LINUX'):
        monkeypatch.setattr(settings, f'HAPP_MIN_VERSION_{platform}', None)
    monkeypatch.setattr(settings, 'HAPP_OUTDATED_ACTIVE_DAYS', 30)
    monkeypatch.setattr(settings, 'HAPP_OUTDATED_NOTIFY_ENABLED', True)
    monkeypatch.setattr(settings, 'HAPP_OUTDATED_REMIND_DAYS', 7)
    monkeypatch.setattr(settings, 'HAPP_APP_STORE_REGION_HELP_URL', 'https://example.com/region')
    monkeypatch.setattr(settings, 'HAPP_DOWNLOAD_LINK_IOS', 'https://apps.apple.com/happ')
    monkeypatch.setattr(settings, 'HAPP_DOWNLOAD_LINK_ANDROID', 'https://play.google.com/happ')


class FakeCache:
    def __init__(self):
        self.keys = set()

    async def setnx(self, key, value, expire=None):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def delete(self, key):
        self.keys.discard(key)
        return True


class FakeDelivery:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send_notification(self, **kwargs):
        self.sent.append(kwargs)
        return self.ok


@pytest.fixture
def cache(monkeypatch):
    fake = FakeCache()
    monkeypatch.setattr(notifier, 'cache', fake)
    return fake


@pytest.fixture
def delivery(monkeypatch):
    import app.services.notification_delivery_service as module

    fake = FakeDelivery()
    monkeypatch.setattr(module, 'notification_delivery_service', fake)
    return fake


USER = SimpleNamespace(id=1, telegram_id=100, language='ru')


def _outdated(*devices):
    from app.utils.client_app_version import describe_device_client

    return [(d, describe_device_client(d)) for d in devices]


def _buttons(markup):
    return [(b.text, b.url or b.callback_data) for row in markup.inline_keyboard for b in row]


def test_single_device_message():
    message, markup = notifier.build_message('ru', _outdated(IPHONE))
    assert 'Обновите Happ' in message
    assert '<b>iPhone 15</b>' in message
    assert 'Happ 5.7.2' in message
    assert '5.8.1' in message
    buttons = _buttons(markup)
    assert ('⬆️ Обновить Happ', 'https://apps.apple.com/happ') in buttons
    assert ('🌍 Сменить регион App Store', 'https://example.com/region') in buttons
    assert buttons[-2][1] == 'menu_support'
    assert buttons[-1][1] == 'webhook:close'


def test_multi_device_message_lists_each_device_and_platform_buttons():
    message, markup = notifier.build_message('ru', _outdated(IPHONE, PIXEL))
    assert '• <b>iPhone 15</b>: Happ 5.7.2' in message
    assert '• <b>Pixel 8</b>: Happ 5.6.0' in message
    buttons = _buttons(markup)
    assert ('⬆️ Happ для iOS', 'https://apps.apple.com/happ') in buttons
    assert ('⬆️ Happ для Android', 'https://play.google.com/happ') in buttons


def test_no_region_button_without_apple_devices():
    _, markup = notifier.build_message('ru', _outdated(PIXEL))
    assert all('App Store' not in text for text, _ in _buttons(markup))


def test_every_locale_renders():
    for language in ('ru', 'en', 'ua', 'zh', 'fa'):
        assert notifier.build_message(language, _outdated(IPHONE)) is not None
        assert notifier.build_message(language, _outdated(IPHONE, PIXEL)) is not None


@pytest.mark.asyncio
async def test_notifies_once_per_device_and_version(cache, delivery):
    bot = object()
    assert await notifier.notify_outdated_devices(bot, USER, [IPHONE, FRESH_MAC]) is True
    assert len(delivery.sent) == 1
    assert 'MacBook' not in delivery.sent[0]['telegram_message']
    # Повтор в пределах HAPP_OUTDATED_REMIND_DAYS — молчим.
    assert await notifier.notify_outdated_devices(bot, USER, [IPHONE]) is False
    assert len(delivery.sent) == 1


@pytest.mark.asyncio
async def test_failed_delivery_releases_dedup(cache, delivery):
    delivery.ok = False
    assert await notifier.notify_outdated_devices(object(), USER, [IPHONE]) is False
    assert not cache.keys
    delivery.ok = True
    assert await notifier.notify_outdated_devices(object(), USER, [IPHONE]) is True


@pytest.mark.asyncio
async def test_skips_email_only_and_disabled(cache, delivery, monkeypatch):
    email_only = SimpleNamespace(id=2, telegram_id=None, language='ru')
    assert await notifier.notify_outdated_devices(object(), email_only, [IPHONE]) is False
    monkeypatch.setattr(settings, 'HAPP_OUTDATED_NOTIFY_ENABLED', False)
    assert await notifier.notify_outdated_devices(object(), USER, [IPHONE]) is False
    assert not delivery.sent
