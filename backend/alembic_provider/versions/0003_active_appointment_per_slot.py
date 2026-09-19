"""one active appointment per slot, enforced by the database

Revision ID: 0003
Revises: 0002

The booking path serialises contenders with a row lock on the slot, and until
now that lock was the only thing preventing a double booking: with it removed,
80 simultaneous racers put 1,568 extra appointments on 20 slots. The index makes
the invariant a property of the schema rather than of every caller remembering
to take the lock.

Partial, because a cancelled appointment must release its slot.
"""

import sqlalchemy as sa
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ACTIVE = "status IN ('PENDING', 'BOOKED')"


def upgrade() -> None:
    # Refuse rather than repair. If duplicates already exist, deciding which
    # appointment is the real one is a human decision; silently deleting one
    # would be exactly the kind of invented repair the reconciler forbids.
    duplicates = op.get_bind().execute(sa.text(f"""
        SELECT count(*) FROM (
            SELECT slot_id FROM appointments WHERE {ACTIVE}
            GROUP BY slot_id HAVING count(*) > 1
        ) AS doubled
    """)).scalar()
    if duplicates:
        raise RuntimeError(
            f"{duplicates} slot(s) already hold more than one active appointment. "
            "Resolve them before applying this migration; it will not choose which booking to keep."
        )
    op.create_index(
        "uq_active_appointment_per_slot",
        "appointments",
        ["slot_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
    )


def downgrade() -> None:
    op.drop_index("uq_active_appointment_per_slot", table_name="appointments")
