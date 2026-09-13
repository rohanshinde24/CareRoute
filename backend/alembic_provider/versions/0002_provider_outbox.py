"""add provider outbox

Revision ID: 0002
Revises: 0001

Written inside the booking transaction itself, so an appointment and the event
announcing it commit together or not at all.
"""

import sqlalchemy as sa
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_outbox",
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
    op.create_index("ix_provider_outbox_dispatched_at", "provider_outbox", ["dispatched_at"])


def downgrade() -> None:
    op.drop_index("ix_provider_outbox_dispatched_at", table_name="provider_outbox")
    op.drop_table("provider_outbox")
