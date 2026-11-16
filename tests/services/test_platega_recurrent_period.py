"""The period the buyer picks is the period SBP charges — or the purchase is refused.

Platega charges only per day/week/month/year. Before, a purchase for 3 months
silently became «349 ₽ every 30 days» at the monthly price; now 1 month and
1 year go through as chosen, other periods are refused up front.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud import platega_subscription as sub_crud
from app.services.platega_recurrent import RECURRENT_PERIOD_UNSUPPORTED, platega_supports_period
from tests.services.test_platega_subscription_callbacks import _memory_session


PRICES = {30: 34900, 90: 94200, 180: 167500, 360: 293200}


def _tariff():
    return SimpleNamespace(
        id=5,
        is_daily=False,
        name='PostqVPN',
        get_available_periods=lambda: list(PRICES),
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: PRICES[d],
    )


class _Svc:
    def __init__(self):
        from app.services.payment.platega import PlategaPaymentMixin

        class Svc(PlategaPaymentMixin):
            def __init__(inner):
                inner.platega_service = SimpleNamespace(
                    create_subscription=AsyncMock(return_value={'transactionId': 'tx-1', 'redirect': 'https://b'})
                )

        self.svc = Svc()

    @property
    def remote(self):
        return self.svc.platega_service.create_subscription


@pytest.mark.parametrize(('days', 'ok'), [(30, True), (31, True), (360, True), (365, True), (90, False), (180, False)])
def test_supported_periods(days, ok):
    assert platega_supports_period(days) is ok


@pytest.mark.parametrize(('days', 'interval'), [(30, 3), (360, 4)])
async def test_binding_charges_the_chosen_period(monkeypatch, days, interval):
    holder = _Svc()
    subscription = SimpleNamespace(id=1, autopay_enabled=False, autopay_period_days=None, device_limit=3)
    async with _memory_session(monkeypatch) as db:
        await holder.svc.create_platega_sbp_subscription(
            db, user_id=1, subscription=subscription, tariff=_tariff(), period_days=days
        )
        stored = await sub_crud.get_active_platega_subscription_by_subscription(db, 1)
    assert stored.charge_days == days
    assert stored.interval == interval
    kwargs = holder.remote.await_args.kwargs
    assert kwargs['interval'] == interval


@pytest.mark.parametrize('days', [90, 180])
async def test_unsupported_period_is_refused_before_platega(monkeypatch, days):
    holder = _Svc()
    subscription = SimpleNamespace(id=1, autopay_enabled=False, autopay_period_days=None, device_limit=3)
    async with _memory_session(monkeypatch) as db:
        with pytest.raises(ValueError, match=RECURRENT_PERIOD_UNSUPPORTED):
            await holder.svc.create_platega_sbp_subscription(
                db, user_id=1, subscription=subscription, tariff=_tariff(), period_days=days
            )
        assert await sub_crud.get_active_platega_subscription_by_subscription(db, 1) is None
    holder.remote.assert_not_awaited()


async def test_quoted_terms_drive_the_binding(monkeypatch):
    """A quoted yearly purchase must not be re-derived as the default month
    (which validate_recurrent_setup_terms would reject as «quote_changed»)."""
    holder = _Svc()
    subscription = SimpleNamespace(id=1, autopay_enabled=False, autopay_period_days=None, device_limit=3)
    subscription._recurrent_purchase_terms = {
        'provider': 'platega',
        'amount_kopeks': PRICES[360],
        'charge_days': 360,
        'interval': 4,
        'selected_device_limit': 3,
    }
    async with _memory_session(monkeypatch) as db:
        await holder.svc.create_platega_sbp_subscription(db, user_id=1, subscription=subscription, tariff=_tariff())
        stored = await sub_crud.get_active_platega_subscription_by_subscription(db, 1)
    assert (stored.charge_days, stored.interval, stored.amount_kopeks) == (360, 4, PRICES[360])


async def test_without_a_choice_the_old_cadence_stays(monkeypatch):
    holder = _Svc()
    subscription = SimpleNamespace(id=1, autopay_enabled=False, autopay_period_days=None, device_limit=3)
    async with _memory_session(monkeypatch) as db:
        await holder.svc.create_platega_sbp_subscription(db, user_id=1, subscription=subscription, tariff=_tariff())
        stored = await sub_crud.get_active_platega_subscription_by_subscription(db, 1)
    assert (stored.charge_days, stored.interval) == (30, 3)


async def test_terms_refuse_unsupported_period(monkeypatch):
    from app.services.recurrent_purchase_selection import calculate_recurrent_purchase_terms

    with pytest.raises(ValueError, match=RECURRENT_PERIOD_UNSUPPORTED):
        await calculate_recurrent_purchase_terms(_tariff(), 'platega', 3, period_days=90)


async def test_terms_for_a_year(monkeypatch):
    from app.services import pricing_engine as pricing_module
    from app.services.recurrent_purchase_selection import calculate_recurrent_purchase_terms

    async def price(tariff, days, **kwargs):
        return SimpleNamespace(final_total=PRICES[days])

    monkeypatch.setattr(pricing_module.pricing_engine, 'calculate_tariff_purchase_price', price)
    terms = await calculate_recurrent_purchase_terms(_tariff(), 'platega', 3, period_days=360)
    assert terms['charge_days'] == 360
    assert terms['interval'] == 4
    assert terms['amount_kopeks'] == PRICES[360]
