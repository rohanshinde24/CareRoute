"""Provider-owned tables: providers, schedules, slots, appointments, reservations.

Appointments live here rather than with referrals because booking must stay one
local transaction. Booking locks a slot, checks the owning provider, marks the
slot busy and writes the appointment; a transaction cannot span two databases,
so the appointment must sit beside the slot it consumes. `referral_id` is
therefore a validated UUID reference to a row in the other database, with no
foreign key, because PostgreSQL cannot enforce one across the boundary.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .events import OutboxMixin
from .provider_database import ProviderBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SlotStatus(str, enum.Enum):
    FREE = "FREE"
    HELD = "HELD"
    BUSY = "BUSY"


class AppointmentStatus(str, enum.Enum):
    PENDING = "PENDING"
    BOOKED = "BOOKED"
    CANCELLED = "CANCELLED"


class Provider(ProviderBase):
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


class ProviderSchedule(ProviderBase):
    __tablename__ = "provider_schedules"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(60), default="America/Los_Angeles")
    provider: Mapped[Provider] = relationship(back_populates="schedules")
    slots: Mapped[list["AppointmentSlot"]] = relationship(back_populates="schedule")


class AppointmentSlot(ProviderBase):
    __tablename__ = "appointment_slots"
    __table_args__ = (UniqueConstraint("schedule_id", "start_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    schedule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_schedules.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SlotStatus] = mapped_column(Enum(SlotStatus, name="slot_status"), default=SlotStatus.FREE)
    schedule: Mapped[ProviderSchedule] = relationship(back_populates="slots")


# One active appointment per slot, enforced by the database. The booking path
# already serialises contenders with a row lock on the slot; this is the
# backstop for any path that does not - a future code path, a manual fix, a
# migration. Partial rather than a plain UNIQUE(slot_id) because a cancelled
# appointment must not keep its slot occupied forever.
ACTIVE_APPOINTMENT_PER_SLOT = "uq_active_appointment_per_slot"
_ACTIVE = text("status IN ('PENDING', 'BOOKED')")


class Appointment(ProviderBase):
    __tablename__ = "appointments"
    __table_args__ = (
        Index(ACTIVE_APPOINTMENT_PER_SLOT, "slot_id", unique=True, postgresql_where=_ACTIVE, sqlite_where=_ACTIVE),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Cross-boundary reference. Validated as a UUID by the API contract; no
    # foreign key exists because the referral lives in another database.
    referral_id: Mapped[uuid.UUID] = mapped_column(index=True)
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointment_slots.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[AppointmentStatus] = mapped_column(Enum(AppointmentStatus, name="appointment_status"), default=AppointmentStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BookingAttempt(ProviderBase):
    """Deduplication record for booking requests.

    A caller that loses the response must be able to repeat the request with the
    same idempotency key and learn the true outcome. Storing the decision - not
    only the success - is what makes a rejection replayable too, so a retry after
    a lost 409 does not look like a fresh attempt on a now-busy slot.
    """

    __tablename__ = "booking_attempts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    referral_id: Mapped[uuid.UUID] = mapped_column(index=True)
    slot_id: Mapped[uuid.UUID]
    outcome: Mapped[str] = mapped_column(String(40))
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderOutbox(OutboxMixin, ProviderBase):
    """Provider-domain outbox, written inside the booking transaction itself."""

    __tablename__ = "provider_outbox"
