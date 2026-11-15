from datetime import UTC, datetime

import pytest

from app.config import settings
from app.utils.client_app_version import (
    describe_device_client,
    is_version_older,
    parse_client_user_agent,
)


NOW = datetime(2026, 11, 15, tzinfo=UTC)


def test_parses_happ_user_agent():
    app = parse_client_user_agent('Happ/6.4.0/ios/2611051057585')
    assert app is not None
    assert (app.name, app.version, app.platform) == ('happ', '6.4.0', 'ios')


@pytest.mark.parametrize('ua', [None, '', 'Mozilla', '/1.2.3'])
def test_unparseable_user_agent(ua):
    assert parse_client_user_agent(ua) is None


@pytest.mark.parametrize(
    ('version', 'minimum', 'older'),
    [
        ('6.3.9', '6.4.0', True),
        ('6.4', '6.4.0', False),
        ('6.4.0', '6.4.0', False),
        ('6.10.0', '6.9.0', False),
        ('5', '6.0', True),
        ('garbage', '6.0', False),
        ('6.0', None, False),
    ],
)
def test_is_version_older(version, minimum, older):
    assert is_version_older(version, minimum) is older


@pytest.fixture
def min_ios(monkeypatch):
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_IOS', '6.4.0')
    monkeypatch.setattr(settings, 'HAPP_OUTDATED_ACTIVE_DAYS', 30)
    monkeypatch.setattr(settings, 'HAPP_DOWNLOAD_LINK_IOS', 'https://apps.apple.com/happ')


def _device(ua, updated='2026-11-13T10:03:19.000Z', platform='iOS'):
    return {'userAgent': ua, 'platform': platform, 'updatedAt': updated}


def test_outdated_active_device(min_ios):
    info = describe_device_client(_device('Happ/6.3.1/ios/1'), now=NOW)
    assert info['app_outdated'] is True
    assert info['app_version'] == '6.3.1'
    assert info['min_app_version'] == '6.4.0'
    assert info['update_url'] == 'https://apps.apple.com/happ'


def test_current_version_not_outdated(min_ios):
    info = describe_device_client(_device('Happ/6.4.0/ios/2611051057585'), now=NOW)
    assert info['app_outdated'] is False
    assert info['update_url'] is None


def test_stale_device_not_flagged(min_ios):
    info = describe_device_client(_device('Happ/6.0.0/ios/1', updated='2026-09-01T00:00:00Z'), now=NOW)
    assert info['app_outdated'] is False


def test_global_minimum_applies_to_all_platforms(min_ios, monkeypatch):
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION', '5.8.1')
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_ANDROID', None)
    info = describe_device_client(_device('Happ/5.8.0/android/1', platform='Android'), now=NOW)
    assert info['app_outdated'] is True
    assert info['min_app_version'] == '5.8.1'


def test_no_minimum_for_platform(min_ios, monkeypatch):
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION', None)
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_ANDROID', None)
    info = describe_device_client(_device('Happ/1.0.0/android/1', platform='Android'), now=NOW)
    assert info['app_outdated'] is False
    assert info['min_app_version'] is None


def test_other_clients_ignored(min_ios):
    info = describe_device_client(_device('v2raytun/1.0/ios'), now=NOW)
    assert info['app_name'] == 'v2raytun'
    assert info['app_outdated'] is False


def test_mobile_minimum_does_not_flag_desktop(monkeypatch):
    """У десктопного Happ своя нумерация: минимум телефонов к нему не применяется."""
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION', None)
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_IOS', '5.8.1')
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_ANDROID', '5.8.1')
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_MACOS', None)
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_WINDOWS', None)
    monkeypatch.setattr(settings, 'HAPP_OUTDATED_ACTIVE_DAYS', 30)
    for ua, platform in (('Happ/1.2.0/macos/1', 'macOS'), ('Happ/1.2.0/windows/1', 'Windows')):
        info = describe_device_client(_device(ua, platform=platform), now=NOW)
        assert info['app_outdated'] is False
        assert info['min_app_version'] is None
    phone = describe_device_client(_device('Happ/5.7.2/ios/1'), now=NOW)
    assert phone['app_outdated'] is True


def test_desktop_minimum_applies_separately(monkeypatch):
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION', None)
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_IOS', '5.8.1')
    monkeypatch.setattr(settings, 'HAPP_MIN_VERSION_WINDOWS', '1.3.0')
    monkeypatch.setattr(settings, 'HAPP_OUTDATED_ACTIVE_DAYS', 30)
    old = describe_device_client(_device('Happ/1.2.0/windows/1', platform='Windows'), now=NOW)
    new = describe_device_client(_device('Happ/1.3.0/windows/1', platform='Windows'), now=NOW)
    assert (old['app_outdated'], old['min_app_version']) == (True, '1.3.0')
    assert new['app_outdated'] is False
