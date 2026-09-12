"""P1 coverage data for agent tools."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "coverages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("payer_name", sa.String(200), nullable=False),
        sa.Column("member_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("source", "external_id"),
    )

def downgrade() -> None:
    op.drop_table("coverages")
