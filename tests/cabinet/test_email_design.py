"""Вид писем postq: обёртка, словарь классов тел и перенос стилей в style-атрибуты.

Письма стилизуются классами из <style> обёртки, а многие клиенты (Gmail для
части аккаунтов, Outlook) <style> выбрасывают — поэтому при отправке стили
переносятся в атрибуты. Тела писем не должны нести своих цветов: старые
инлайн-цвета Bootstrap перебивали бы обёртку.
"""

import re
from types import SimpleNamespace

import pytest

from app.cabinet.routes.admin_email_templates import AVAILABLE_LANGUAGES, _build_sample_context
from app.cabinet.services import email_layout
from app.cabinet.services.email_css import inline_email_css
from app.cabinet.services.email_templates import EmailNotificationTemplates


def _all_renders():
    templates = EmailNotificationTemplates()
    for notification_type in templates.supported_types():
        for language in AVAILABLE_LANGUAGES:
            rendered = templates.get_template(
                notification_type, language, _build_sample_context(notification_type.value)
            )
            yield notification_type.value, language, rendered


def test_inliner_moves_layout_styles_into_attributes_and_keeps_media_queries():
    body = EmailNotificationTemplates()._get_base_template('<p><a href="#" class="button">Go</a></p>', 'en')
    inlined = inline_email_css(body)
    button = re.search(r'<a [^>]*class="button"[^>]*>', inlined).group(0)
    assert 'background-color: #ff2181' in button
    # @media для телефона и тёмной темы инлайном не выразить — остаются в <style>.
    assert '@media (prefers-color-scheme: dark)' in inlined


def test_inliner_returns_original_html_on_failure(monkeypatch):
    class Broken:
        def inline(self, _html):
            raise ValueError('bad html')

    monkeypatch.setattr('app.cabinet.services.email_css._inliner', Broken())
    assert inline_email_css('<style>p{}</style><p>x</p>') == '<style>p{}</style><p>x</p>'


def test_html_without_style_is_left_alone():
    assert inline_email_css('<p>plain</p>') == '<p>plain</p>'


def test_logo_comes_from_the_cabinet_and_disappears_without_it():
    block = email_layout.logo_block('https://cab.example/', 'PostqVPN')
    assert 'src="https://cab.example/brand/email-logo.png"' in block
    assert 'src="https://cab.example/brand/email-logo-dark.png"' in block
    assert 'alt="PostqVPN"' in block
    assert email_layout.logo_block('', 'PostqVPN') == '<span class="brand">PostqVPN</span>'


def test_footer_links_support_when_it_is_configured(monkeypatch):
    monkeypatch.setattr(email_layout, 'settings', SimpleNamespace(get_support_contact_url=lambda: 'https://t.me/x'))
    assert 'href="https://t.me/x"' in email_layout.support_block('ru')
    monkeypatch.setattr(email_layout, 'settings', SimpleNamespace(get_support_contact_url=lambda: None))
    assert email_layout.support_block('ru') == ''


@pytest.mark.parametrize('legacy', ['#007bff', '#dc3545', '#28a745', 'color: #666', 'font-family: monospace'])
def test_bodies_carry_no_colours_of_their_own(legacy):
    leaking = [
        (notification_type, language)
        for notification_type, language, rendered in _all_renders()
        if legacy in email_layout.extract_content_fragment(rendered['body_html'])
    ]
    assert not leaking, f'{legacy} в теле письма: {leaking[:5]}'


def test_no_email_shows_the_same_button_twice():
    for notification_type, language, rendered in _all_renders():
        labels = re.findall(r'class="button">([^<]+)<', rendered['body_html'])
        assert len(labels) == len(set(labels)), (notification_type, language, labels)
