"""add consumed events

Revision ID: 0010
Revises: 0009

Delivery is at-least-once, so a consumer must be able to recognise an event it
has already handled. The unique index on event_id is what makes a redelivery a
no-op rather than a second effect.
"""

import sqlalchemy as sa
from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consumed_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("consumer", sa.String(60), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_consumed_events_event_id", "consumed_events", ["event_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_consumed_events_event_id", table_name="consumed_events")
    op.drop_table("consumed_events")
