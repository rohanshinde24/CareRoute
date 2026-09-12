"""provider domain foundation

Revision ID: 0001
Revises:

The provider database has its own lineage on purpose. Sharing the referral
history would mean every migration described tables the other database does not
own, and neither could be migrated independently.
"""

import sqlalchemy as sa
from alembic import op


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

slot_status = sa.Enum("FREE", "HELD", "BUSY", name="slot_status")
appointment_status = sa.Enum("PENDING", "BOOKED", "CANCELLED", name="appointment_status")


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("npi", sa.String(10), unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("specialty", sa.String(120), nullable=False, index=True),
        sa.Column("location", sa.String(200), nullable=False),
        sa.Column("accepting_new_patients", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_evaluation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "provider_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider_id", sa.Uuid(), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(60), nullable=False),
    )
    op.create_table(
        "appointment_slots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schedule_id", sa.Uuid(), sa.ForeignKey("provider_schedules.id"), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", slot_status, nullable=False, server_default="FREE"),
        sa.UniqueConstraint("schedule_id", "start_at"),
    )
    op.create_index("ix_appointment_slots_start_at_id", "appointment_slots", ["start_at", "id"])
    op.create_table(
        "appointments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # No foreign key: the referral lives in the other database.
        sa.Column("referral_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("slot_id", sa.Uuid(), sa.ForeignKey("appointment_slots.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("status", appointment_status, nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "booking_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True, index=True),
        sa.Column("referral_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("slot_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("appointment_id", sa.Uuid(), nullable=True),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_providers_created_at_id", "providers", ["created_at", "id"])


def downgrade() -> None:
    op.drop_table("booking_attempts")
    op.drop_table("appointments")
    op.drop_index("ix_appointment_slots_start_at_id", table_name="appointment_slots")
    op.drop_table("appointment_slots")
    op.drop_table("provider_schedules")
    op.drop_table("providers")
    appointment_status.drop(op.get_bind(), checkfirst=True)
    slot_status.drop(op.get_bind(), checkfirst=True)
