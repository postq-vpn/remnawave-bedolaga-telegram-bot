"""Перенос CSS писем из <style> в атрибуты style перед отправкой.

Обёртка писем (email_layout) задаёт вид классами в <style>, а Gmail (для
части аккаунтов и при пересылке), Outlook и многие веб-клиенты <style>
выбрасывают — письмо приходит голым текстом. Инлайн-стили читают все.

<style> при этом остаётся: в нём живут @media для телефона и тёмной темы,
которые инлайном не выразить; они перебивают инлайн через !important.

Инлайнер — защитная прослойка: если он не справился (битый HTML из
сохранённого в админке шаблона), уходит исходный HTML, а не ничего.
"""

import structlog


logger = structlog.get_logger(__name__)

try:
    import css_inline

    _inliner = css_inline.CSSInliner(keep_style_tags=True, load_remote_stylesheets=False)
except Exception as error:  # pragma: no cover - зависимость объявлена, но письмо важнее
    logger.warning('css-inline недоступен — письма уйдут без инлайн-стилей', error=str(error))
    _inliner = None


def inline_email_css(body_html: str) -> str:
    """HTML письма с инлайн-стилями; при любой ошибке — исходный HTML."""
    if not body_html or _inliner is None or '<style' not in body_html.lower():
        return body_html
    try:
        return _inliner.inline(body_html)
    except Exception as error:
        logger.warning('Не удалось перенести стили письма в style-атрибуты', error=str(error))
        return body_html
