"""P2 durable workflow and idempotent commands."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("referrals", sa.Column("selected_slot_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_referrals_selected_slot", "referrals", "appointment_slots", ["selected_slot_id"], ["id"])
    op.create_unique_constraint("uq_referral_document_locator", "referral_documents", ["referral_id", "document_type", "storage_locator"])
    op.create_table(
        "processed_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.String(200), nullable=False, unique=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("referral_id", sa.Uuid(), sa.ForeignKey("referrals.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )

def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_constraint("uq_referral_document_locator", "referral_documents", type_="unique")
    op.drop_constraint("fk_referrals_selected_slot", "referrals", type_="foreignkey")
    op.drop_column("referrals", "selected_slot_id")
