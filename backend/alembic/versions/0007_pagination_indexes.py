"""add keyset pagination indexes

Revision ID: 0007
Revises: 0006

Keyset pagination orders by a timestamp with the primary key as tiebreaker.
Without a matching compound index each page is still a full scan plus a sort,
so the index is part of the feature rather than an optimisation of it.

The appointment_slots index moves with that table when provider data ownership
separates; it is created here so the endpoint is not unbounded in the meantime.
"""

from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_referrals_created_at_id",
        "referrals",
        ["created_at", "id"],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_appointment_slots_start_at_id",
        "appointment_slots",
        ["start_at", "id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("ix_appointment_slots_start_at_id", table_name="appointment_slots")
    op.drop_index("ix_referrals_created_at_id", table_name="referrals")
