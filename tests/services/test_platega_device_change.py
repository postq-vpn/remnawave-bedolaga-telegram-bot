"""SBP binding vs device count.

Platega fixes the subscription amount at creation and has no way to change it.
A binding priced for N devices must not keep charging after the subscription
moves to another count: the user would underpay (more devices) or overpay
(fewer). It is closed right after the change, and — as a backstop for paths
without the hook — on the next charge, which is still honoured.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.database.crud import platega_subscription as sub_crud
from app.database.models import PlategaSubscription, Subscription, Transaction
from tests.services.test_platega_subscription_callbacks import _memory_session


async def _binding(db, *, device_limit, status='ACTIVE'):
    return await sub_crud.create_platega_subscription(
        db,
        user_id=1,
        subscription_id=1,
        tariff_id=None,
        interval=3,
        charge_days=30,
        amount_kopeks=34900,
        redirect_url=None,
        platega_subscription_id='ps-1',
        status=status,
        device_limit=device_limit,
    )


def _remote(monkeypatch):
    """Platega API stand-in for the module-level cancel entry point."""
    from app.services.payment import platega as platega_module

    cancel = AsyncMock(return_value={'status': 'cancelled'})
    monkeypatch.setattr(platega_module, 'PlategaService', lambda: SimpleNamespace(cancel_subscription=cancel))
    return cancel


async def test_create_records_device_limit(monkeypatch):
    from app.services.payment.platega import PlategaPaymentMixin

    subscription = SimpleNamespace(id=1, autopay_enabled=True, autopay_period_days=30, device_limit=4)
    tariff = SimpleNamespace(
        id=5,
        is_daily=False,
        name='PostqVPN',
        get_available_periods=lambda: [30],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 34900,
    )

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(
                create_subscription=AsyncMock(return_value={'transactionId': 'tx-1', 'redirect': 'https://pay/1'})
            )

    async with _memory_session(monkeypatch) as db:
        await Svc().create_platega_sbp_subscription(db, user_id=1, subscription=subscription, tariff=tariff)
        stored = await sub_crud.get_active_platega_subscription_by_subscription(db, 1)
        assert stored.device_limit == 4


async def test_device_change_closes_binding(monkeypatch):
    from app.services.payment.platega import cancel_platega_recurring_if_devices_changed

    cancel = _remote(monkeypatch)
    async with _memory_session(monkeypatch) as db:
        rec = await _binding(db, device_limit=3)
        subscription = SimpleNamespace(id=1, device_limit=4)

        assert await cancel_platega_recurring_if_devices_changed(db, subscription) is True

        await db.refresh(rec)
        assert rec.status == 'CANCELLED'
        cancel.assert_awaited_once_with('ps-1')


async def test_fewer_devices_also_close_binding(monkeypatch):
    from app.services.payment.platega import cancel_platega_recurring_if_devices_changed

    _remote(monkeypatch)
    async with _memory_session(monkeypatch) as db:
        rec = await _binding(db, device_limit=5, status='PENDING')

        assert await cancel_platega_recurring_if_devices_changed(db, SimpleNamespace(id=1, device_limit=3)) is True
        await db.refresh(rec)
        assert rec.status == 'CANCELLED'


async def test_same_count_or_unknown_keeps_binding(monkeypatch):
    from app.services.payment.platega import cancel_platega_recurring_if_devices_changed

    cancel = _remote(monkeypatch)
    async with _memory_session(monkeypatch) as db:
        rec = await _binding(db, device_limit=3)
        assert await cancel_platega_recurring_if_devices_changed(db, SimpleNamespace(id=1, device_limit=3)) is False
        await db.refresh(rec)
        assert rec.status == 'ACTIVE'

        # A binding from before the column existed is not second-guessed.
        rec.device_limit = None
        await db.commit()
        assert await cancel_platega_recurring_if_devices_changed(db, SimpleNamespace(id=1, device_limit=9)) is False
        await db.refresh(rec)
        assert rec.status == 'ACTIVE'
        cancel.assert_not_awaited()


async def test_no_binding_is_a_noop(monkeypatch):
    from app.services.payment.platega import cancel_platega_recurring_if_devices_changed

    cancel = _remote(monkeypatch)
    async with _memory_session(monkeypatch) as db:
        assert await cancel_platega_recurring_if_devices_changed(db, SimpleNamespace(id=1, device_limit=4)) is False
        cancel.assert_not_awaited()


def _charge(charge_id='charge-1'):
    return {
        'Status': 'CONFIRMED',
        'Id': charge_id,
        'Amount': 349,
        'Currency': 'RUB',
        'PaymentMethod': 6,
        'SubscriptionId': 'ps-1',
        'NextChargeAt': '2026-12-16T00:00:00Z',
    }


async def test_charge_with_stale_device_count_is_honoured_then_closed(monkeypatch):
    """Backstop: the device count changed on a path without the hook. The money
    of this charge is taken — the renewal counts — but the binding closes, so
    the next month is not charged at the old price."""
    from app.services.payment.platega import PlategaPaymentMixin

    cancel = AsyncMock(return_value={'status': 'cancelled'})

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(cancel_subscription=cancel)

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0, device_limit=6)
        db.add(subscription)
        await db.commit()
        rec = await _binding(db, device_limit=3)

        await Svc().process_platega_subscription_callback(db, _charge())

        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date >= end0 + timedelta(days=29)
        assert rec.status == 'CANCELLED'
        assert rec.charges_success == 1
        cancel.assert_awaited_once_with('ps-1')
        txs = (await db.execute(select(Transaction))).scalars().all()
        # Payments are stored negative (money out of the user's pocket).
        assert [abs(tx.amount_kopeks) for tx in txs] == [34900]

        # A late redelivery of the same charge does not renew twice.
        await Svc().process_platega_subscription_callback(db, _charge())
        await db.refresh(subscription)
        assert len((await db.execute(select(Transaction))).scalars().all()) == 1


async def test_charge_with_matching_device_count_stays_active(monkeypatch):
    from app.services.payment.platega import PlategaPaymentMixin

    cancel = AsyncMock()

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(cancel_subscription=cancel)

    async with _memory_session(monkeypatch) as db:
        db.add(
            Subscription(
                id=1, user_id=1, status='active', end_date=datetime.now(UTC) + timedelta(days=2), device_limit=3
            )
        )
        await db.commit()
        rec = await _binding(db, device_limit=3)

        await Svc().process_platega_subscription_callback(db, _charge())

        await db.refresh(rec)
        assert rec.status == 'ACTIVE'
        cancel.assert_not_awaited()
        assert (await db.scalar(select(PlategaSubscription.status))) == 'ACTIVE'
