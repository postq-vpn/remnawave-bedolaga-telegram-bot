"""Remember the device count a Platega SBP subscription amount was priced for.

Platega cannot change the amount of a live subscription, so a binding whose
device count no longer matches the subscription is cancelled and re-created.

Revision ID: 0141
Revises: 0140
"""

import sqlalchemy as sa
from alembic import op


revision = '0141'
down_revision = '0140'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('platega_subscriptions', sa.Column('device_limit', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('platega_subscriptions', 'device_limit')
