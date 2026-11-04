"""Регрессия (прод-репорт 2026-11, SALES_MODE=classic): платная покупка после
триала оставляла ``tariff_id`` триал-тарифа (10 ГБ). Пока период оплачен —
незаметно, но автопродление брало лимиты тарифа, и платный безлимит упирался
в 10 ГБ и уходил в грейс. Конверсия должна переводить строку на платный тариф.
"""

from unittest.mock import AsyncMock, MagicMock

from app.database.crud import subscription as sub_crud


def _db(trial_tariff, candidates) -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(return_value=trial_tariff)
    result = MagicMock()
    result.scalars.return_value.all.return_value = candidates
    db.execute = AsyncMock(return_value=result)
    return db


def _classic(monkeypatch):
    monkeypatch.setattr(type(sub_crud.settings), 'is_tariffs_mode', lambda self: False)


async def test_moves_converted_trial_to_single_paid_tariff(monkeypatch):
    _classic(monkeypatch)
    sub = MagicMock(id=651, tariff_id=5)
    db = _db(MagicMock(id=5, is_trial_available=True), [2])

    await sub_crud.move_off_trial_tariff(db, sub)

    assert sub.tariff_id == 2


async def test_keeps_tariff_when_paid_tariff_is_ambiguous(monkeypatch):
    _classic(monkeypatch)
    sub = MagicMock(id=651, tariff_id=5)
    db = _db(MagicMock(id=5, is_trial_available=True), [2, 3])

    await sub_crud.move_off_trial_tariff(db, sub)

    assert sub.tariff_id == 5


async def test_ignores_non_trial_tariff(monkeypatch):
    _classic(monkeypatch)
    sub = MagicMock(id=651, tariff_id=3)
    db = _db(MagicMock(id=3, is_trial_available=False), [2])

    await sub_crud.move_off_trial_tariff(db, sub)

    assert sub.tariff_id == 3
    db.execute.assert_not_awaited()


async def test_noop_in_tariffs_mode(monkeypatch):
    monkeypatch.setattr(type(sub_crud.settings), 'is_tariffs_mode', lambda self: True)
    sub = MagicMock(id=651, tariff_id=5)
    db = _db(MagicMock(id=5, is_trial_available=True), [2])

    await sub_crud.move_off_trial_tariff(db, sub)

    assert sub.tariff_id == 5
    db.get.assert_not_awaited()
