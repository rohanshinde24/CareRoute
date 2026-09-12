"""Wire contracts for the provider domain.

These types are the boundary. They are shared by the provider service that
serves them, the gateway that consumes them, and the MCP tools that re-expose
them, so that a change to the contract cannot be made on one side only.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProviderToolResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    npi: str | None = None
    name: str
    specialty: str
    location: str
    accepting_new_patients: bool
    is_synthetic: bool = True


class ProviderListResult(BaseModel):
    items: list[ProviderToolResult]


class ProviderSpecialtyListResult(BaseModel):
    items: list[str]


class SlotToolResult(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    start_at: datetime
    end_at: datetime


class SlotListResult(BaseModel):
    items: list[SlotToolResult]


class SlotDetail(BaseModel):
    """One slot with the eligibility facts only the provider domain can read."""

    id: uuid.UUID
    provider_id: uuid.UUID
    provider_specialty: str
    start_at: datetime
    end_at: datetime
    status: str
    is_free: bool


class ProviderPage(BaseModel):
    items: list[ProviderToolResult]
    next_cursor: str | None = None


class SlotWithProvider(BaseModel):
    id: uuid.UUID
    schedule_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    status: str
    provider: ProviderToolResult


class SlotPage(BaseModel):
    items: list[SlotWithProvider]
    next_cursor: str | None = None


class AppointmentRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    referral_id: uuid.UUID
    slot_id: uuid.UUID
    status: str
    created_at: datetime


class AppointmentListResult(BaseModel):
    items: list[AppointmentRecord]


class BookingRequest(BaseModel):
    """A booking asked for by the referral domain.

    `requested_specialty` travels with the request because the provider domain
    must re-check eligibility itself rather than trust the caller's word: the
    specialty belongs to the provider record, which only this side can read.
    """

    referral_id: uuid.UUID
    slot_id: uuid.UUID
    requested_specialty: str = Field(min_length=2, max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=200)


class BookingResult(BaseModel):
    outcome: str
    appointment_id: uuid.UUID | None = None
    slot_id: uuid.UUID
    detail: str | None = None
    replayed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome in {"booked", "duplicate_suppressed"}
