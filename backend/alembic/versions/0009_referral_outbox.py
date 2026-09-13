"""add referral outbox

Revision ID: 0009
Revises: 0008

The outbox is written in the same transaction as the state change it describes,
so that "the change happened" and "the event exists" cannot come apart. The
index is on dispatched_at because the relay's only query is for the oldest
undispatched rows.
"""

import sqlalchemy as sa
from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("producer", sa.String(60), nullable=False),
        sa.Column("traceparent", sa.String(120), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_referral_outbox_dispatched_at", "referral_outbox", ["dispatched_at"])


def downgrade() -> None:
    op.drop_index("ix_referral_outbox_dispatched_at", table_name="referral_outbox")
    op.drop_table("referral_outbox")
