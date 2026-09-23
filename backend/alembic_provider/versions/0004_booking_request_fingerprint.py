"""record what each idempotency key was used for

Revision ID: 0004
Revises: 0003

A booking key made a repeated request return its first decision, whatever the
repeat actually asked for. Reused with different arguments - a different slot,
a different referral - the caller would be told about a booking it never
requested. Storing a fingerprint of the request lets the provider domain tell a
genuine retry from a reused key.

Nullable, and existing rows keep NULL: a decision recorded before this migration
has no fingerprint to compare against, and inventing one would be worse than
replaying as before.
"""

import sqlalchemy as sa
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_attempts", sa.Column("request_fingerprint", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("booking_attempts", "request_fingerprint")
