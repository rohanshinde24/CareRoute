import enum
import uuid
from datetime import date, datetime, timezone
from typing import Any
from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base
# Provider-owned state enums are defined with the tables they describe and
# re-exported here so referral-side imports have one source of truth.
from .provider_models import AppointmentStatus, SlotStatus  # noqa: F401
from .events import OutboxMixin

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class ReferralState(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PARSING = "PARSING"
    VALIDATING = "VALIDATING"
    WAITING_FOR_DOCUMENTS = "WAITING_FOR_DOCUMENTS"
    CHECKING_COVERAGE = "CHECKING_COVERAGE"
    MATCHING_PROVIDER = "MATCHING_PROVIDER"
    WAITING_FOR_SLOT_SELECTION = "WAITING_FOR_SLOT_SELECTION"
    BOOKING = "BOOKING"
    CONFIRMED = "CONFIRMED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    COVERAGE_UNVERIFIED = "COVERAGE_UNVERIFIED"
    BOOKING_FAILED = "BOOKING_FAILED"
    CANCELLED = "CANCELLED"



class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(50))
    given_name: Mapped[str] = mapped_column(String(100))
    family_name: Mapped[str] = mapped_column(String(100))
    birth_date: Mapped[date] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(30))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    referrals: Mapped[list["Referral"]] = relationship(back_populates="patient")
    coverages: Mapped[list["Coverage"]] = relationship(back_populates="patient")

class Coverage(Base):
    __tablename__ = "coverages"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"))
    external_id: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(50))
    payer_name: Mapped[str] = mapped_column(String(200))
    member_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="active")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    patient: Mapped[Patient] = relationship(back_populates="coverages")

class PatientProcedure(Base):
    __tablename__ = "patient_procedures"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"), index=True)
    procedure_type: Mapped[str] = mapped_column(String(120))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    report_document_type: Mapped[str] = mapped_column(String(100))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)

class Referral(Base):
    __tablename__ = "referrals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"))
    requested_specialty: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)
    location_preference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[ReferralState] = mapped_column(Enum(ReferralState, name="referral_state"), default=ReferralState.RECEIVED)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    is_evaluation: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # Cross-boundary reference to a provider-owned slot. No foreign key exists
    # because PostgreSQL cannot enforce one across databases.
    selected_slot_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    patient: Mapped[Patient] = relationship(back_populates="referrals")
    documents: Mapped[list["ReferralDocument"]] = relationship(back_populates="referral", cascade="all, delete-orphan")

class ReferralDocument(Base):
    __tablename__ = "referral_documents"
    __table_args__ = (UniqueConstraint("referral_id", "document_type", "storage_locator"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    referral_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("referrals.id"))
    document_type: Mapped[str] = mapped_column(String(100))
    storage_locator: Mapped[str] = mapped_column(String(500))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    referral: Mapped[Referral] = relationship(back_populates="documents")





class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    referral_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("referrals.id"))
    state: Mapped[ReferralState] = mapped_column(Enum(ReferralState, name="referral_state", create_type=False))
    engine_run_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class AgentEvent(Base):
    __tablename__ = "agent_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id"))
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(200), unique=True)
    event_type: Mapped[str] = mapped_column(String(100))
    referral_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("referrals.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    ground_truth: Mapped[dict[str, Any]] = mapped_column(JSON)

class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_cases.id"))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReferralOutbox(OutboxMixin, Base):
    """Referral-domain outbox. Written in the same transaction as the state change."""

    __tablename__ = "referral_outbox"


class ConsumedEvent(Base):
    """Dedup record for domain events this service has already handled.

    Delivery is at-least-once, so the same event will arrive again after a relay
    crash or a redelivered stream entry. Consumers are required to absorb that,
    and this table is how: the handler and this row commit together, so an event
    is marked consumed only if its effect landed.

    Separate from `processed_events`, which deduplicates inbound *commands* from
    users. Different producers, different lifetimes, and collapsing them would
    let a command id collide with an event id.
    """

    __tablename__ = "consumed_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    consumer: Mapped[str] = mapped_column(String(60))
    outcome: Mapped[str] = mapped_column(String(40))
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
