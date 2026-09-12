"""remove provider-owned tables from the referral database

Revision ID: 0008
Revises: 0007

Providers, schedules, slots and appointments now live in the provider database
with their own lineage. `referrals.selected_slot_id` survives as a plain UUID:
it still identifies a slot, but PostgreSQL cannot enforce a foreign key to a
table in another database, so the constraint is dropped rather than pretended.
"""

import sqlalchemy as sa
from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for constraint in inspector.get_foreign_keys("referrals"):
        if constraint["referred_table"] == "appointment_slots" and constraint["name"]:
            op.drop_constraint(constraint["name"], "referrals", type_="foreignkey")

    op.drop_index("ix_appointment_slots_start_at_id", table_name="appointment_slots")
    op.drop_table("appointments")
    op.drop_table("appointment_slots")
    op.drop_table("provider_schedules")
    op.drop_table("providers")
    sa.Enum(name="appointment_status").drop(bind, checkfirst=True)
    sa.Enum(name="slot_status").drop(bind, checkfirst=True)


def downgrade() -> None:
    raise NotImplementedError(
        "Provider data has moved to its own database; restore it there rather than "
        "recreating empty tables here."
    )
