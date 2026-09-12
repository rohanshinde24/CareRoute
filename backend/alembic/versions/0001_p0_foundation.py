"""P0 deterministic foundation."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

states = ("RECEIVED", "PARSING", "VALIDATING", "WAITING_FOR_DOCUMENTS", "CHECKING_COVERAGE", "MATCHING_PROVIDER", "WAITING_FOR_SLOT_SELECTION", "BOOKING", "CONFIRMED", "NEEDS_HUMAN_REVIEW", "PROVIDER_UNAVAILABLE", "COVERAGE_UNVERIFIED", "BOOKING_FAILED", "CANCELLED")

def upgrade() -> None:
    state = sa.Enum(*states, name="referral_state")
    slot_status = sa.Enum("FREE", "BUSY", "CANCELLED", name="slot_status")
    appointment_status = sa.Enum("PENDING", "BOOKED", "CANCELLED", "FAILED", name="appointment_status")
    op.create_table("patients", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("external_id", sa.String(100), nullable=False), sa.Column("source", sa.String(50), nullable=False), sa.Column("given_name", sa.String(100), nullable=False), sa.Column("family_name", sa.String(100), nullable=False), sa.Column("birth_date", sa.Date(), nullable=False), sa.Column("gender", sa.String(30)), sa.Column("is_synthetic", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("source", "external_id"))
    op.create_table("providers", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("npi", sa.String(10), unique=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("specialty", sa.String(120), nullable=False), sa.Column("location", sa.String(200), nullable=False), sa.Column("accepting_new_patients", sa.Boolean(), nullable=False), sa.Column("is_synthetic", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("referrals", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patients.id"), nullable=False), sa.Column("requested_specialty", sa.String(120), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("state", state, nullable=False), sa.Column("is_synthetic", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("referral_documents", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("referral_id", sa.Uuid(), sa.ForeignKey("referrals.id"), nullable=False), sa.Column("document_type", sa.String(100), nullable=False), sa.Column("storage_locator", sa.String(500), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("provider_schedules", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("provider_id", sa.Uuid(), sa.ForeignKey("providers.id"), nullable=False), sa.Column("name", sa.String(120), nullable=False), sa.Column("timezone", sa.String(60), nullable=False))
    op.create_table("appointment_slots", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("schedule_id", sa.Uuid(), sa.ForeignKey("provider_schedules.id"), nullable=False), sa.Column("start_at", sa.DateTime(timezone=True), nullable=False), sa.Column("end_at", sa.DateTime(timezone=True), nullable=False), sa.Column("status", slot_status, nullable=False), sa.UniqueConstraint("schedule_id", "start_at"))
    op.create_table("appointments", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("referral_id", sa.Uuid(), sa.ForeignKey("referrals.id"), nullable=False), sa.Column("slot_id", sa.Uuid(), sa.ForeignKey("appointment_slots.id"), nullable=False), sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True), sa.Column("status", appointment_status, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("workflow_runs", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("referral_id", sa.Uuid(), sa.ForeignKey("referrals.id"), nullable=False), sa.Column("state", state, nullable=False), sa.Column("engine_run_id", sa.String(200), unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("agent_events", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("workflow_run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=False), sa.Column("event_type", sa.String(100), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("evaluation_cases", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("name", sa.String(200), nullable=False, unique=True), sa.Column("input_data", sa.JSON(), nullable=False), sa.Column("ground_truth", sa.JSON(), nullable=False))
    op.create_table("evaluation_runs", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("case_id", sa.Uuid(), sa.ForeignKey("evaluation_cases.id"), nullable=False), sa.Column("result", sa.JSON(), nullable=False), sa.Column("score", sa.Float()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))

def downgrade() -> None:
    for table in ("evaluation_runs", "evaluation_cases", "agent_events", "workflow_runs", "appointments", "appointment_slots", "provider_schedules", "referral_documents", "referrals", "providers", "patients"):
        op.drop_table(table)
    for enum_name in ("appointment_status", "slot_status", "referral_state"):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)

