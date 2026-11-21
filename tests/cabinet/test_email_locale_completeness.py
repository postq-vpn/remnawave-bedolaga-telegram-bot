"""Render every built-in email and editor metadata in every email language (ru, en)."""

import re

import pytest

from app.cabinet.routes.admin_email_templates import (
    AVAILABLE_LANGUAGES,
    EDITOR_LANGUAGES,
    LAYOUT_TEMPLATE_META,
    SAMPLE_CONTEXTS,
    TEMPLATE_TYPES,
    _get_default_template,
    _placeholder_context,
)
from app.cabinet.services.email_layout import (
    DEFAULT_EMAIL_LAYOUT,
    email_language,
    extract_content_fragment,
    render_email_layout,
)


@pytest.mark.parametrize('language', AVAILABLE_LANGUAGES)
@pytest.mark.parametrize('meta', TEMPLATE_TYPES, ids=lambda meta: meta['type'])
def test_real_email_body_and_editor_metadata_are_localized(meta, language):
    assert language in EDITOR_LANGUAGES
    assert meta['label'][language]
    assert meta['description'][language]
    context = SAMPLE_CONTEXTS[meta['type']]
    rendered = _get_default_template(meta['type'], language, context)
    assert rendered and rendered['subject']
    content = extract_content_fragment(rendered['body_html'])
    assert content
    if language != 'ru':
        ru = _get_default_template(meta['type'], 'ru', context)
        assert content != extract_content_fragment(ru['body_html']), f'{meta["type"]} falls back to RU'
    editor = _get_default_template(meta['type'], language, _placeholder_context(meta['type']))
    english = _get_default_template(meta['type'], 'en', _placeholder_context(meta['type']))
    assert sorted(re.findall(r'href="([^"]+)"', editor['body_html'])) == sorted(
        re.findall(r'href="([^"]+)"', english['body_html'])
    )


def test_emails_go_out_only_in_russian_and_english():
    """ru — русским, пустой язык — дефолтом бота (ru), остальные — английским."""
    assert AVAILABLE_LANGUAGES == ['ru', 'en']
    assert [email_language(code) for code in ('ru', 'RU', '', None, 'en', 'ua', 'zh', 'fa')] == [
        'ru',
        'ru',
        'ru',
        'ru',
        'en',
        'en',
        'en',
        'en',
    ]
    for code in ('ua', 'zh', 'fa'):
        foreign = _get_default_template('subscription_expired', code, {})
        english = _get_default_template('subscription_expired', 'en', {})
        assert foreign == english
    assert 'dir="rtl"' not in render_email_layout(DEFAULT_EMAIL_LAYOUT, 'fa', {'content': 'x'})


def test_custom_layout_is_used_as_is():
    custom = '<html lang="en" dir="ltr"><body>{content}</body></html>'
    assert (
        render_email_layout(custom, 'en', {'content': '<p>Operator content</p>'}, mark_content=False)
        == '<html lang="en" dir="ltr"><body><p>Operator content</p></body></html>'
    )
    assert set(AVAILABLE_LANGUAGES) <= set(LAYOUT_TEMPLATE_META['label'])


@pytest.mark.parametrize(
    ('language', 'plan', 'level', 'bonus'),
    [
        ('en', 'Plan', 'Network level', 'Your bonus'),
    ],
)
def test_optional_nested_blocks_use_recipient_language(language, plan, level, bonus):
    renewed = _get_default_template(
        'subscription_renewed', language, {'tariff_name': 'Operator <plan>', 'new_expires_at': '2030-01-01'}
    )
    assert plan in renewed['body_html']
    assert 'Operator &lt;plan&gt;' in renewed['body_html']
    referral = _get_default_template(
        'referral_bonus', language, {'level': 2, 'formatted_reward': '7 days', 'referral_name': 'Operator'}
    )
    assert level in referral['body_html']
    welcome = _get_default_template(
        'referral_welcome', language, {'referrer_name': 'Operator', 'bonus_promise': 'Custom promise'}
    )
    assert bonus in welcome['body_html']
    assert 'Custom promise' in welcome['body_html']
    promo = _get_default_template(
        'promo_offer', language, {'message_html': '<em>Operator custom</em>', 'valid_hours': 24, 'discount_percent': 0}
    )
    assert '<em>Operator custom</em>' in promo['body_html']


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_referral_reward_plan_is_plain_data_in_email_html(language):
    from app.cabinet.services.email_templates import EmailNotificationTemplates

    reward = '7 days <b>Operator plan</b> & custom'
    context = {'formatted_reward': reward, 'referral_name': 'Example', 'level': 1}
    rendered = EmailNotificationTemplates()._referral_bonus_template(language, context)
    assert '<b>Operator plan</b>' not in rendered['body_html']
    assert '&lt;b&gt;Operator plan&lt;/b&gt; &amp; custom' in rendered['body_html']
    assert reward in rendered['subject']
    assert context['formatted_reward'] == reward
