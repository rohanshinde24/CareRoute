"""add synthetic recent procedure metadata

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patient_procedures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("procedure_type", sa.String(120), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_document_type", sa.String(100), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_patient_procedures_patient_id", "patient_procedures", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_patient_procedures_patient_id", table_name="patient_procedures")
    op.drop_table("patient_procedures")
