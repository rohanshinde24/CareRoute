"""isolate evaluation fixtures from product-visible records

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("referrals", sa.Column("is_evaluation", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("providers", sa.Column("is_evaluation", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE referrals SET is_evaluation = true FROM patients WHERE referrals.patient_id = patients.id AND patients.source = 'evaluation'")
    op.execute("UPDATE providers SET is_evaluation = true WHERE location = 'Evaluation, CA'")


def downgrade() -> None:
    op.drop_column("providers", "is_evaluation")
    op.drop_column("referrals", "is_evaluation")
