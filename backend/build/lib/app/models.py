import enum
import uuid
from datetime import date, datetime, timezone
from typing import Any
from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

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

class SlotStatus(str, enum.Enum):
    FREE = "FREE"
    BUSY = "BUSY"
    CANCELLED = "CANCELLED"

class AppointmentStatus(str, enum.Enum):
    PENDING = "PENDING"
    BOOKED = "BOOKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

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
    state: Mapped[ReferralState] = mapped_column(Enum(ReferralState, name="referral_state"), default=ReferralState.RECEIVED)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    is_evaluation: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    selected_slot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appointment_slots.id"), nullable=True)
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

class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    npi: Mapped[str | None] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str] = mapped_column(String(120), index=True)
    location: Mapped[str] = mapped_column(String(200))
    accepting_new_patients: Mapped[bool] = mapped_column(Boolean, default=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    is_evaluation: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    schedules: Mapped[list["ProviderSchedule"]] = relationship(back_populates="provider")

class ProviderSchedule(Base):
    __tablename__ = "provider_schedules"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(60), default="America/Los_Angeles")
    provider: Mapped[Provider] = relationship(back_populates="schedules")
    slots: Mapped[list["AppointmentSlot"]] = relationship(back_populates="schedule")

class AppointmentSlot(Base):
    __tablename__ = "appointment_slots"
    __table_args__ = (UniqueConstraint("schedule_id", "start_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    schedule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_schedules.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SlotStatus] = mapped_column(Enum(SlotStatus, name="slot_status"), default=SlotStatus.FREE)
    schedule: Mapped[ProviderSchedule] = relationship(back_populates="slots")

class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    referral_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("referrals.id"))
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointment_slots.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[AppointmentStatus] = mapped_column(Enum(AppointmentStatus, name="appointment_status"), default=AppointmentStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

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
