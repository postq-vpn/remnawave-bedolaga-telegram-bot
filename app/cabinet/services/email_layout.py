"""
Общая обёртка писем (layout): один редактируемый HTML-каркас для всех писем.

Тексты писем редактировались, а обёртка — шапка с названием сервиса, цвета,
подвал — была зашита в код; поменять её можно было только вставив полный HTML
в каждый из 50 типов на каждом из 5 языков. Теперь обёртка хранится как
псевдо-тип ``email_layout`` в той же таблице email_templates и редактируется в
том же редакторе. Плейсхолдер ``{content}`` обязателен — в него встаёт тело
письма; без сохранённой обёртки работает встроенная DEFAULT_EMAIL_LAYOUT.

Рендер писем синхронный, а хранилище асинхронное, поэтому обёртка живёт в
кэше процесса: его обновляет каждый чокпойнт отправки (get_rendered_override)
не чаще раза в LAYOUT_CACHE_TTL_SECONDS и сразу — сохранение в редакторе.
Ошибка базы — это «обёртки нет», а не потерянное письмо.
"""

import html
import re
import time
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

EMAIL_LAYOUT_TYPE = 'email_layout'
CONTENT_PLACEHOLDER = '{content}'
LAYOUT_CACHE_TTL_SECONDS = 60

# Плейсхолдеры обёртки в порядке подстановки: content первым, чтобы переменные
# внутри вставленного письма ({cabinet_url} и т.п.) тоже подставились.
LAYOUT_CONTEXT_VARS = [
    'content',
    'service_name',
    'footer_text',
    'unsubscribe_block',
    'unsubscribe_url',
    'logo_block',
    'support_block',
    'preheader',
    'year',
    'cabinet_url',
    'support_username',
    # Данные получателя: рендер письма передаёт их в обёртку вместе с телом.
    'username',
    'email',
    'date',
]
# HTML-значения — не экранируем.
_RAW_VARS = frozenset({'content', 'unsubscribe_block', 'logo_block', 'support_block'})

# Маркеры вокруг тела письма: по ним редактор достаёт фрагмент из готового
# письма независимо от того, как выглядит обёртка.
CONTENT_START = '<!-- email-content-start -->'
CONTENT_END = '<!-- email-content-end -->'

# Письма postq уходят только на русском и английском. Тексты на zh/ua/fa в
# шаблонах остались (так проще подтягивать апстрим), но не отправляются:
# русскоязычным — русский, всем остальным — английский.
EMAIL_LANGUAGES = ('ru', 'en')


def email_language(language: str | None) -> str:
    """Язык письма: ru для русского и неизвестного (дефолт бота), иначе en."""
    code = (language or '').strip().lower()
    if not code or code.startswith('ru'):
        return 'ru'
    return 'en'


# Подвал по образцу писем крупных сервисов: зачем пришло письмо, а не
# «не отвечайте на это письмо». {service} — название сервиса.
FOOTER_TEXTS = {
    'ru': 'Вы получили это письмо, потому что у вас есть аккаунт {service}.',
    'en': 'You’re receiving this email because you have a {service} account.',
}

UNSUBSCRIBE_TEXTS = {
    'ru': 'Отписаться от рассылок',
    'en': 'Unsubscribe from marketing emails',
}

CABINET_LINK_TEXTS = {'ru': 'Личный кабинет', 'en': 'Account'}
SUPPORT_LINK_TEXTS = {'ru': 'Поддержка', 'en': 'Support'}

# Каркас писем postq по образцу писем крупных сервисов (Stripe, Apple):
# белая страница без «карточки», знак и название слева, колонка 560px,
# заголовок без восклицаний, данные — строками чека (название слева,
# значение справа, тонкие разделители), кнопка слева, подвал из ссылок.
# Цвета — из кабинета: текст #212023 / #4d4d4f / #7a797b, акцент #ff2181.
# Вёрстка таблицами — Outlook не умеет max-width у div. Стили из <style>
# переносятся в атрибуты style при отправке (email_css.inline_email_css),
# @media для телефона и тёмной темы остаются в <style> и бьют через !important.
# Словарь тел писем: h2 — заголовок; table.details — строки чека (строит
# structure_email_body из .highlight с «Название: значение»); .highlight —
# блок текста, .warning / .danger — предупреждение; .actions .button —
# кнопка; .muted — второстепенный текст; .code-box .code — код подтверждения;
# code — логин/пароль.
DEFAULT_EMAIL_LAYOUT = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light dark">
    <meta name="supported-color-schemes" content="light dark">
    <style>
        body { margin: 0; padding: 0; background-color: #ffffff; -webkit-text-size-adjust: 100%; }
        .wrapper { background-color: #ffffff; }
        .container { width: 100%; max-width: 560px; }
        .header { padding: 40px 0 36px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
        .logo { display: block; width: 28px; height: 28px; border: 0; }
        .brand { font-size: 22px; line-height: 28px; font-weight: 700; letter-spacing: -0.3px; color: #212023; vertical-align: middle; }
        .content { padding: 0 0 8px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 16px; line-height: 26px; color: #4d4d4f; }
        h1, h2 { margin: 0 0 16px; font-size: 28px; line-height: 34px; font-weight: 700; letter-spacing: -0.4px; color: #212023; }
        h3 { margin: 32px 0 8px; font-size: 18px; line-height: 24px; font-weight: 600; color: #212023; }
        p { margin: 0 0 16px; }
        ul, ol { margin: 0 0 16px; padding-left: 24px; }
        li { margin: 0 0 8px; }
        strong, b { font-weight: 600; color: #212023; }
        a { color: #ff2181; text-decoration: none; }
        .details { width: 100%; margin: 24px 0 28px; border-collapse: collapse; }
        .details td { padding: 14px 0; border-top: 1px solid #e9e9e9; font-size: 15px; line-height: 22px; vertical-align: top; }
        .details tr:last-child td { border-bottom: 1px solid #e9e9e9; }
        .details .label { padding-right: 16px; color: #7a797b; }
        .details .value { text-align: right; font-weight: 500; color: #212023; }
        .details .value strong { font-weight: 500; }
        .highlight { margin: 24px 0; padding: 18px 20px; background-color: #f8f8f8; border-radius: 12px; }
        .highlight p { margin: 0 0 8px; }
        .highlight p:last-child { margin-bottom: 0; }
        .highlight.warning { background-color: #fff9e5; }
        .highlight.danger { background-color: #ffeded; }
        .amount { font-weight: 600; color: #212023; white-space: nowrap; }
        .actions { margin: 32px 0 8px; }
        .button { display: inline-block; padding: 14px 24px; background-color: #ff2181; border-radius: 12px; color: #ffffff !important; font-size: 15px; line-height: 20px; font-weight: 600; text-decoration: none; }
        .muted { font-size: 14px; line-height: 22px; color: #7a797b; }
        .code-box { margin: 24px 0; padding: 22px 16px; background-color: #f8f8f8; border-radius: 12px; text-align: center; }
        .code { margin: 0; padding-left: 12px; font-size: 34px; line-height: 40px; font-weight: 600; letter-spacing: 12px; font-variant-numeric: tabular-nums; color: #212023; }
        code { padding: 2px 6px; background-color: #f4f4f4; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; color: #212023; }
        .footer { padding: 28px 0 48px; border-top: 1px solid #e9e9e9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 13px; line-height: 20px; color: #7a797b; }
        .footer p { margin: 0 0 8px; }
        .footer a { color: #4d4d4f; text-decoration: underline; }
        .footer .links a { text-decoration: none; font-weight: 500; }
        .gap { height: 40px; line-height: 40px; font-size: 0; }
        @media (max-width: 600px) {
            .header { padding: 28px 0 28px !important; }
            h1, h2 { font-size: 24px !important; line-height: 30px !important; }
            .button { display: block !important; text-align: center !important; }
            .details td { font-size: 14px !important; }
        }
        @media (prefers-color-scheme: dark) {
            body, .wrapper { background-color: #1a1a1c !important; }
            .content { color: #d3d2d3 !important; }
            .brand, h1, h2, h3, strong, b, .amount, .code, code, .details .value { color: #f8f8f8 !important; }
            .details td, .details tr:last-child td, .footer { border-color: #373639 !important; }
            .highlight, .code-box { background-color: #2b2a2d !important; }
            .highlight.warning { background-color: #4d3d00 !important; }
            .highlight.danger { background-color: #5c1a1a !important; }
            code { background-color: #373639 !important; }
            .muted, .footer, .details .label { color: #a6a6a7 !important; }
            .footer a { color: #d3d2d3 !important; }
        }
    </style>
</head>
<body>
    <div class="preheader" style="display: none; max-height: 0; overflow: hidden; mso-hide: all; font-size: 1px; line-height: 1px; color: transparent; opacity: 0;">{preheader}&#8199;&#65279;&#847;&#8199;&#65279;&#847;&#8199;&#65279;&#847;&#8199;&#65279;&#847;&#8199;&#65279;&#847;&#8199;&#65279;&#847;</div>
    <table role="presentation" class="wrapper" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
            <td align="center" style="padding: 0 24px;">
                <table role="presentation" class="container" width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                        <td class="header">{logo_block}</td>
                    </tr>
                    <tr>
                        <td class="content">
                            {content}
                        </td>
                    </tr>
                    <tr>
                        <td class="gap">&nbsp;</td>
                    </tr>
                    <tr>
                        <td class="footer">
                            {support_block}
                            <p>{footer_text}</p>
                            {unsubscribe_block}
                            <p>&copy; {year} {service_name}</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""


def layout_is_valid(layout_html: str) -> bool:
    """Обёртка без {content} проглотила бы тело каждого письма."""
    return CONTENT_PLACEHOLDER in (layout_html or '')


# Логотип из кабинета (public/brand): PNG, потому что SVG в письмах режут.
# Две версии — тёмная надпись для светлой темы и белая для тёмной; вторая
# скрыта и показывается @media (prefers-color-scheme: dark) там, где он есть.
# Только знак: он розовый и читается на любом фоне. Название рядом — текстом,
# его Gmail и другие клиенты сами перекрашивают в тёмной теме, а картинку нет
# (тёмная надпись логотипа-картинки пропадала на тёмном фоне в Gmail на телефоне).
LOGO_PATH = '/brand/email-mark.png'


def _pick(texts: dict[str, str], language: str) -> str:
    return texts.get(language, texts['ru'])


def logo_block(cabinet_url: str, service_name: str = '') -> str:
    """Знак сервиса и его название текстом; без адреса кабинета — только название."""
    name = html.escape(service_name or '')
    if not cabinet_url:
        return f'<span class="brand">{name}</span>'
    mark = html.escape(cabinet_url.rstrip('/') + LOGO_PATH, quote=True)
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="vertical-align: middle; padding-right: 10px;">'
        f'<img src="{mark}" width="28" height="28" alt="" class="logo"></td>'
        f'<td class="brand">{name}</td>'
        '</tr></table>'
    )


def support_block(language: str, cabinet_url: str = '') -> str:
    """Ссылки подвала: личный кабинет и поддержка (что из них настроено)."""
    url = settings.get_support_contact_url() if hasattr(settings, 'get_support_contact_url') else None
    links = []
    if cabinet_url:
        links.append(f'<a href="{html.escape(cabinet_url, quote=True)}">{_pick(CABINET_LINK_TEXTS, language)}</a>')
    if url:
        links.append(f'<a href="{html.escape(url, quote=True)}">{_pick(SUPPORT_LINK_TEXTS, language)}</a>')
    if not links:
        return ''
    # Без разделителей-точек: ссылки разводит отступ.
    return '<p class="links">' + '<span style="display: inline-block; width: 20px;"></span>'.join(links) + '</p>'


def unsubscribe_block(unsubscribe_url: str, language: str) -> str:
    """Ссылка отписки — только у маркетинговых писем, у остальных пусто."""
    if not unsubscribe_url:
        return ''
    label = _pick(UNSUBSCRIBE_TEXTS, language)
    return f'<p><a href="{html.escape(unsubscribe_url, quote=True)}">{label}</a></p>'


# ============ Строки чека ============

_HIGHLIGHT = re.compile(r'<div class="highlight([^"]*)"([^>]*)>(.*?)</div>', re.DOTALL)
_PARAGRAPH = re.compile(r'<p(?: [^>]*)?>(.*?)</p>', re.DOTALL)
# «Сумма: <strong>500 ₽</strong>» и «<strong>Email:</strong> <code>…</code>».
_ROW_PLAIN = re.compile(r'^\s*([^<>:]{1,48}?)\s*[:：]\s*(\S.*?)\s*$', re.DOTALL)
_ROW_STRONG_LABEL = re.compile(r'^\s*<strong>([^<>:]{1,48}?)\s*[:：]?</strong>\s*(\S.*?)\s*$', re.DOTALL)


def _as_row(paragraph: str) -> tuple[str, str] | None:
    match = _ROW_STRONG_LABEL.match(paragraph) or _ROW_PLAIN.match(paragraph)
    if not match:
        return None
    label, value = match.group(1).strip(), match.group(2).strip()
    # Значение — короткие данные, не продолжение фразы с двоеточием.
    if not value or len(re.sub(r'<[^>]+>', '', value)) > 64 or value.endswith(('.', '!')):
        return None
    return label, value


def structure_email_body(content: str) -> str:
    """Плашки «Название: значение» → строки чека (table.details).

    Тела писем строят данные абзацами внутри .highlight; крупные сервисы
    показывают их строками: название слева, значение справа. Абзацы-фразы
    остаются текстом над строками (в .warning / .danger — блоком
    предупреждения), плашка без пар «название: значение» остаётся как есть.
    """

    def replace(match: re.Match[str]) -> str:
        modifiers, attrs, inner = match.group(1), match.group(2), match.group(3)
        if '<div' in inner:
            return match.group(0)
        paragraphs = _PARAGRAPH.findall(inner)
        if not paragraphs or re.sub(r'<p(?: [^>]*)?>.*?</p>', '', inner, flags=re.DOTALL).strip():
            return match.group(0)
        rows, texts = [], []
        for paragraph in paragraphs:
            row = _as_row(paragraph)
            if row:
                rows.append(row)
            else:
                texts.append(paragraph)
        if not rows:
            return match.group(0)
        alert = 'warning' in modifiers or 'danger' in modifiers
        text_html = ''.join(f'<p>{text}</p>' for text in texts)
        if text_html and alert:
            text_html = f'<div class="highlight{modifiers}"{attrs}>{text_html}</div>'
        table = ''.join(
            f'<tr><td class="label">{label}</td><td class="value">{value}</td></tr>' for label, value in rows
        )
        return (
            f'{text_html}<table role="presentation" class="details" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0">{table}</table>'
        )

    return _HIGHLIGHT.sub(replace, content)


def _preheader(content: str) -> str:
    """Текст превью в списке писем: первый абзац после заголовка."""
    without_heading = re.sub(r'<h[12][^>]*>.*?</h[12]>', '', content, count=1, flags=re.DOTALL)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', without_heading))
    text = ' '.join(text.split())
    return text[:140]


def build_layout_context(language: str, *, content: str = '', unsubscribe_url: str = '') -> dict[str, str]:
    """Реальные значения плейсхолдеров обёртки для языка.

    Данные получателя здесь пустые — их подмешивает рендер письма; пустая
    строка гарантирует, что литерал {username} не уйдёт в письмо.
    """
    from datetime import UTC, datetime

    from app.utils.timezone import format_email_datetime

    cabinet_url = getattr(settings, 'CABINET_URL', '') or ''
    service_name = settings.SMTP_FROM_NAME or 'VPN Service'
    return {
        'content': content,
        'service_name': service_name,
        'preheader': _preheader(content),
        'year': str(datetime.now(UTC).year),
        'logo_block': logo_block(cabinet_url, service_name),
        'support_block': support_block(language, cabinet_url),
        'footer_text': _pick(FOOTER_TEXTS, language).format(service=service_name),
        'unsubscribe_block': unsubscribe_block(unsubscribe_url, language),
        # Голая ссылка — для своего текста отписки вместо готового блока.
        'unsubscribe_url': unsubscribe_url or '',
        'cabinet_url': cabinet_url,
        'support_username': getattr(settings, 'SUPPORT_USERNAME', '') or '',
        'username': '',
        'email': '',
        'date': format_email_datetime(datetime.now(UTC), fmt='%d.%m.%Y'),
    }


def render_email_layout(
    layout_html: str,
    language: str,
    context: dict[str, Any] | None = None,
    *,
    mark_content: bool = True,
) -> str:
    """Подставляет плейсхолдеры обёртки. Недостающие берутся из build_layout_context.

    ``content`` и ``unsubscribe_block`` — HTML, остальное экранируется.
    ``mark_content`` обрамляет тело маркерами для extract_content_fragment;
    редактор обёртки получает дефолт без них.
    """
    language = email_language(language)
    given = context or {}
    values = {
        **build_layout_context(
            language,
            content=str(given.get('content') or ''),
            unsubscribe_url=str(given.get('unsubscribe_url') or ''),
        ),
        **given,
    }
    result = layout_html
    for key in LAYOUT_CONTEXT_VARS:
        raw = values.get(key)
        value = '' if raw is None else str(raw)
        if key == 'content' and mark_content:
            value = f'{CONTENT_START}{value}{CONTENT_END}'
        elif key not in _RAW_VARS:
            value = html.escape(value)
        result = result.replace(f'{{{key}}}', value)
    return result


def extract_content_fragment(rendered_html: str) -> str | None:
    """Тело письма из готового HTML по маркерам, None если маркеров нет."""
    start = rendered_html.find(CONTENT_START)
    end = rendered_html.find(CONTENT_END)
    if start < 0 or end < 0 or end < start:
        return None
    return rendered_html[start + len(CONTENT_START) : end].strip()


# ============ Кэш сохранённой обёртки ============

_cache: dict[str, str] = {}
_loaded_at: float | None = None


def _monotonic() -> float:
    return time.monotonic()


def reset_email_layout_cache() -> None:
    global _cache, _loaded_at
    _cache = {}
    _loaded_at = None


def set_cached_email_layouts(layouts: dict[str, str]) -> None:
    """Заменяет кэш; обёртки без {content} отбрасываются с предупреждением."""
    global _cache, _loaded_at
    valid = {lang: body for lang, body in layouts.items() if layout_is_valid(body)}
    for lang in set(layouts) - set(valid):
        logger.warning('Сохранённая обёртка писем без {content} пропущена', language=lang)
    _cache = valid
    _loaded_at = _monotonic()


def get_cached_email_layout(language: str) -> str | None:
    """Обёртка для языка; нет своей — русская; нет и её — None (встроенная)."""
    return _cache.get(language) or _cache.get('ru')


def resolve_email_layout(language: str) -> str:
    return get_cached_email_layout(language) or DEFAULT_EMAIL_LAYOUT


def is_layout_cache_stale() -> bool:
    return _loaded_at is None or _monotonic() - _loaded_at > LAYOUT_CACHE_TTL_SECONDS


async def _load_layouts(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(
        text(
            'SELECT language, body_html FROM email_templates WHERE notification_type = :ntype AND is_active = :active'
        ),
        {'ntype': EMAIL_LAYOUT_TYPE, 'active': True},
    )
    return {row[0]: row[1] for row in result.fetchall()}


async def refresh_email_layout_cache(db: AsyncSession | None = None, *, force: bool = False) -> None:
    """Подтягивает сохранённые обёртки, если кэш устарел (или force после сохранения)."""
    global _loaded_at
    if not force and not is_layout_cache_stale():
        return
    try:
        if db is not None:
            layouts = await _load_layouts(db)
        else:
            async with AsyncSessionLocal() as session:
                layouts = await _load_layouts(session)
    except Exception as e:
        # Старый кэш остаётся, но время загрузки обновляем — иначе каждое письмо
        # заново долбило бы недоступную базу.
        logger.warning('Не удалось загрузить обёртку писем — остаётся текущая', error=e)
        _loaded_at = _monotonic()
        return
    set_cached_email_layouts(layouts)
