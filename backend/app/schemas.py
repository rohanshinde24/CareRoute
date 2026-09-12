import uuid
from datetime import date, datetime
from typing import Annotated
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator
from .models import AppointmentStatus, ReferralState, SlotStatus

def reject_uuid_specialty(value: str) -> str:
    try:
        uuid.UUID(value)
    except ValueError:
        return value
    raise ValueError("Specialty must be a specialty name, not an identifier")

SpecialtyName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=120, pattern=r"^[A-Za-z][A-Za-z0-9 .&/()'\-]*$"),
    AfterValidator(reject_uuid_specialty),
]

LocationPreference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]

class PatientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    external_id: str
    source: str
    given_name: str
    family_name: str
    birth_date: date
    gender: str | None
    is_synthetic: bool

class CoverageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    patient_id: uuid.UUID
    external_id: str
    source: str
    payer_name: str
    member_id: str
    status: str
    is_synthetic: bool

class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    document_type: str
    storage_locator: str
    received_at: datetime

class ReferralCreate(BaseModel):
    patient_id: uuid.UUID
    requested_specialty: SpecialtyName
    reason: str = Field(min_length=3, max_length=2000)
    location_preference: LocationPreference | None = None
    is_synthetic: bool

    @model_validator(mode="after")
    def require_synthetic(self):
        if not self.is_synthetic:
            raise ValueError("CareRoute currently accepts synthetic referrals only")
        return self

class ReferralRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    patient_id: uuid.UUID
    requested_specialty: str
    reason: str
    location_preference: str | None
    state: ReferralState
    is_synthetic: bool
    created_at: datetime
    updated_at: datetime
    selected_slot_id: uuid.UUID | None

class ReferralDetail(ReferralRead):
    patient: PatientRead
    documents: list[DocumentRead]

class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    npi: str | None
    name: str
    specialty: str
    location: str
    accepting_new_patients: bool
    is_synthetic: bool

class SlotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    schedule_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    status: SlotStatus
    provider: ProviderRead

class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    referral_id: uuid.UUID
    state: ReferralState
    engine_run_id: str | None
    created_at: datetime
    updated_at: datetime

class AgentEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    workflow_run_id: uuid.UUID
    event_type: str
    payload: dict
    created_at: datetime

class DurableProcessRead(BaseModel):
    workflow_run_id: uuid.UUID
    referral_id: uuid.UUID
    event_id: str
    accepted: bool = True

class DurableProcessCreate(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=200)

class CommandBase(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)

class DocumentCreate(CommandBase):
    document_type: str = Field(min_length=2, max_length=100)
    storage_locator: str = Field(min_length=3, max_length=500)

class SlotSelection(CommandBase):
    slot_id: uuid.UUID

class Confirmation(CommandBase):
    pass

class Cancellation(CommandBase):
    reason: str | None = Field(default=None, max_length=500)

class CommandRead(BaseModel):
    referral_id: uuid.UUID
    state: ReferralState
    duplicate: bool
    event_id: str

class AppointmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    referral_id: uuid.UUID
    slot_id: uuid.UUID
    status: AppointmentStatus
    created_at: datetime
