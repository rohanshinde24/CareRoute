"""drop the duplicate unique index on consumed_events.event_id

Revision ID: 0011
Revises: 0010

The column was declared `unique=True, index=True`, which builds two identical
unique indexes: the constraint's own, and a second one. Both are maintained on
every insert, and the consumer inserts one row per event. The constraint is the
one to keep, since dropping it would weaken the guarantee that a redelivered
event cannot be applied twice.
"""

from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_consumed_events_event_id", table_name="consumed_events")


def downgrade() -> None:
    op.create_index("ix_consumed_events_event_id", "consumed_events", ["event_id"], unique=True)
