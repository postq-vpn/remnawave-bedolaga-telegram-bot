"""Balance autopay vs a live SBP (Platega) binding, on the real autopay pass.

Balance autopay is switched off only when the bank confirms the binding, so a
binding that waits (up to 30 minutes), works or is past due must keep balance
autopay away — otherwise the subscription is renewed twice. A failed or
cancelled attempt must not: the user keeps auto-renewal from the balance.
"""

from datetime import UTC, datetime

import pytest

from app.database.models import PlategaSubscription
from tests.database.test_renewal_survives_stale_panel_snapshot_postgres import (
    BALANCE,
    PRICE,
    TABLES,
    _autopay_pass,
    _paid_transactions,
    _seed,
    single_tariff_mode_quiet,  # noqa: F401 — autouse fixture of that module
)
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('binding', 'charged'),
    [
        (None, True),
        ('FAILED', True),
        ('CANCELLED', True),
        ('PENDING', False),
        ('ACTIVE', False),
        ('PAST_DUE', False),
    ],
)
async def test_balance_autopay_and_sbp_binding(postgres_database, monkeypatch, binding, charged):
    async with postgres_session(postgres_database, TABLES) as db:
        user, subscription, old_end = await _seed(db, datetime.now(UTC))
        if binding is not None:
            db.add(
                PlategaSubscription(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    interval=3,
                    charge_days=30,
                    amount_kopeks=PRICE,
                    status=binding,
                    platega_subscription_id=f'ps-{binding}',
                    device_limit=3,
                )
            )
            await db.commit()

        await _autopay_pass(db, monkeypatch)

        await db.refresh(user)
        await db.refresh(subscription)
        if charged:
            assert user.balance_kopeks == BALANCE - PRICE
            assert subscription.end_date > old_end
            assert len(await _paid_transactions(db, user.id)) == 1
        else:
            assert user.balance_kopeks == BALANCE
            assert subscription.end_date == old_end
            assert await _paid_transactions(db, user.id) == []
