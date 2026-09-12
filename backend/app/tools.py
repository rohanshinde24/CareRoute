import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .models import Coverage, Patient, PatientProcedure, Referral, ReferralDocument, ReferralState
# Provider contracts are re-exported: the MCP layer speaks the same shapes the
# provider service serves, so a contract change cannot diverge between them.
from .provider_contracts import ProviderListResult, ProviderSpecialtyListResult, ProviderToolResult, SlotListResult, SlotToolResult  # noqa: F401
from .schemas import SpecialtyName

class RiskClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    ADMINISTRATIVE_WRITE = "ADMINISTRATIVE_WRITE"

class ToolFailure(RuntimeError):
    pass

class IdRequest(BaseModel):
    id: uuid.UUID

class ReferralToolResult(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    requested_specialty: str
    reason: str
    location_preference: str | None
    state: ReferralState

class PatientToolResult(BaseModel):
    id: uuid.UUID
    external_id: str
    given_name: str
    family_name: str
    birth_date: str
    gender: str | None

class CoverageToolResult(BaseModel):
    id: uuid.UUID
    payer_name: str
    member_id: str
    status: str

class CoverageListResult(BaseModel):
    items: list[CoverageToolResult]

class DocumentToolResult(BaseModel):
    id: uuid.UUID
    document_type: str
    received_at: datetime

class DocumentListResult(BaseModel):
    items: list[DocumentToolResult]

class ReferralHistoryItem(BaseModel):
    id: uuid.UUID
    requested_specialty: str
    state: ReferralState

class ReferralHistoryResult(BaseModel):
    items: list[ReferralHistoryItem]

class ProcedureToolResult(BaseModel):
    id: uuid.UUID
    procedure_type: str
    occurred_at: datetime
    report_document_type: str

class ProcedureListResult(BaseModel):
    items: list[ProcedureToolResult]

class RequestDocumentInput(BaseModel):
    referral_id: uuid.UUID
    document_type: str = Field(min_length=2, max_length=100)

class RequestDocumentResult(BaseModel):
    referral_id: uuid.UUID
    document_type: str
    state: ReferralState
    already_waiting: bool

class ProviderSearchInput(BaseModel):
    specialty: SpecialtyName
    include_evaluation: bool = False




class SlotSearchInput(BaseModel):
    provider_id: uuid.UUID



ToolHandler = Callable[[Session, BaseModel], BaseModel]

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    risk: RiskClass
    timeout_seconds: float
    idempotent: bool
    failure_behavior: str

    def invoke(self, db: Session, arguments: dict[str, Any]) -> BaseModel:
        request = self.input_model.model_validate(arguments)
        try:
            response = self.handler(db, request)
        except ToolFailure:
            raise
        except Exception as exc:
            raise ToolFailure(f"{self.name} failed") from exc
        return self.output_model.model_validate(response)

def _get_referral(db: Session, request: IdRequest) -> ReferralToolResult:
    referral = db.get(Referral, request.id)
    if referral is None:
        raise ToolFailure("Referral not found")
    return ReferralToolResult.model_validate(referral, from_attributes=True)

def _get_patient(db: Session, request: IdRequest) -> PatientToolResult:
    patient = db.get(Patient, request.id)
    if patient is None or not patient.is_synthetic:
        raise ToolFailure("Synthetic patient not found")
    return PatientToolResult(id=patient.id, external_id=patient.external_id, given_name=patient.given_name, family_name=patient.family_name, birth_date=patient.birth_date.isoformat(), gender=patient.gender)

def _get_coverage(db: Session, request: IdRequest) -> CoverageListResult:
    records = db.scalars(select(Coverage).where(Coverage.patient_id == request.id, Coverage.is_synthetic.is_(True))).all()
    return CoverageListResult(items=[CoverageToolResult.model_validate(record, from_attributes=True) for record in records])

def _get_documents(db: Session, request: IdRequest) -> DocumentListResult:
    records = db.scalars(select(ReferralDocument).where(ReferralDocument.referral_id == request.id).order_by(ReferralDocument.received_at)).all()
    return DocumentListResult(items=[DocumentToolResult.model_validate(record, from_attributes=True) for record in records])

def _get_referral_history(db: Session, request: IdRequest) -> ReferralHistoryResult:
    referral = db.get(Referral, request.id)
    if referral is None:
        raise ToolFailure("Referral not found")
    records = db.scalars(select(Referral).where(Referral.patient_id == referral.patient_id, Referral.id != referral.id).order_by(Referral.created_at.desc()).limit(10)).all()
    return ReferralHistoryResult(items=[ReferralHistoryItem.model_validate(record, from_attributes=True) for record in records])

def _get_recent_procedures(db: Session, request: IdRequest) -> ProcedureListResult:
    records = db.scalars(select(PatientProcedure).where(PatientProcedure.patient_id == request.id, PatientProcedure.is_synthetic.is_(True)).order_by(PatientProcedure.occurred_at.desc()).limit(10)).all()
    return ProcedureListResult(items=[ProcedureToolResult.model_validate(record, from_attributes=True) for record in records])

def _request_document(db: Session, request: RequestDocumentInput) -> RequestDocumentResult:
    referral = db.get(Referral, request.referral_id)
    if referral is None:
        raise ToolFailure("Referral not found")
    already_waiting = referral.state == ReferralState.WAITING_FOR_DOCUMENTS
    referral.state = ReferralState.WAITING_FOR_DOCUMENTS
    db.commit()
    return RequestDocumentResult(referral_id=referral.id, document_type=request.document_type, state=referral.state, already_waiting=already_waiting)



def _read_tool(name: str, input_model: type[BaseModel], output_model: type[BaseModel], handler: ToolHandler, failure: str) -> ToolDefinition:
    return ToolDefinition(name, input_model, output_model, handler, RiskClass.READ_ONLY, 5.0, True, failure)

TOOLS = {
    tool.name: tool for tool in (
        _read_tool("getReferral", IdRequest, ReferralToolResult, _get_referral, "Fail closed with referral-not-found."),
        _read_tool("getPatient", IdRequest, PatientToolResult, _get_patient, "Fail closed with synthetic-patient-not-found."),
        _read_tool("getCoverage", IdRequest, CoverageListResult, _get_coverage, "Return an empty list when no coverage is recorded."),
        _read_tool("getReferralDocuments", IdRequest, DocumentListResult, _get_documents, "Return an empty list when no documents are recorded."),
        _read_tool("getReferralHistory", IdRequest, ReferralHistoryResult, _get_referral_history, "Return an empty list when no prior referrals are recorded."),
        _read_tool("getRecentProcedures", IdRequest, ProcedureListResult, _get_recent_procedures, "Return an empty list when no recent synthetic procedure is recorded."),
        ToolDefinition("requestMissingDocument", RequestDocumentInput, RequestDocumentResult, _request_document, RiskClass.ADMINISTRATIVE_WRITE, 5.0, True, "Fail closed without changing referral state."),
    )
}

def invoke_tool(name: str, db: Session, arguments: dict[str, Any]) -> BaseModel:
    definition = TOOLS.get(name)
    if definition is None:
        raise ToolFailure(f"Unknown tool: {name}")
    return definition.invoke(db, arguments)
