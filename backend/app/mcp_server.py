import uuid
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .database import SessionLocal
from .provider_database import ProviderSessionLocal
from .provider_gateway import configured_provider_gateway
from .schemas import SpecialtyName
from .tools import CoverageListResult, DocumentListResult, PatientToolResult, ProcedureListResult, ProviderListResult, ReferralHistoryResult, ReferralToolResult, RequestDocumentResult, SlotListResult, invoke_tool

mcp = MCPServer("CareRoute", version="0.1.0", instructions="Administrative tools for synthetic referral coordination. Never infer clinical facts or bypass human confirmation.")
read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
administrative_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

def _call(name: str, arguments: dict):
    with SessionLocal() as db:
        return invoke_tool(name, db, arguments)


async def _provider_call(operation, correlation_id: uuid.UUID):
    """Provider reads cross the domain boundary like every other caller's do.

    Configured deployments route this over HTTP to the provider service; the
    local gateway is used only when no provider service URL is set, and it holds
    a provider-database session rather than the referral one.
    """
    with ProviderSessionLocal() as provider_db:
        return await operation(configured_provider_gateway(provider_db))

@mcp.tool(name="getReferral", annotations=read_only, structured_output=True)
def get_referral(referral_id: Annotated[uuid.UUID, Field(description="Synthetic referral UUID")]) -> ReferralToolResult:
    return _call("getReferral", {"id": referral_id})

@mcp.tool(name="getPatient", annotations=read_only, structured_output=True)
def get_patient(patient_id: Annotated[uuid.UUID, Field(description="Synthetic patient UUID")]) -> PatientToolResult:
    return _call("getPatient", {"id": patient_id})

@mcp.tool(name="getCoverage", annotations=read_only, structured_output=True)
def get_coverage(patient_id: Annotated[uuid.UUID, Field(description="Synthetic patient UUID")]) -> CoverageListResult:
    return _call("getCoverage", {"id": patient_id})

@mcp.tool(name="getReferralDocuments", annotations=read_only, structured_output=True)
def get_referral_documents(referral_id: Annotated[uuid.UUID, Field(description="Synthetic referral UUID")]) -> DocumentListResult:
    return _call("getReferralDocuments", {"id": referral_id})

@mcp.tool(name="getReferralHistory", annotations=read_only, structured_output=True)
def get_referral_history(referral_id: Annotated[uuid.UUID, Field(description="Synthetic referral UUID")]) -> ReferralHistoryResult:
    return _call("getReferralHistory", {"id": referral_id})

@mcp.tool(name="getRecentProcedures", annotations=read_only, structured_output=True)
def get_recent_procedures(patient_id: Annotated[uuid.UUID, Field(description="Synthetic patient UUID")]) -> ProcedureListResult:
    return _call("getRecentProcedures", {"id": patient_id})

@mcp.tool(name="requestMissingDocument", annotations=administrative_write, structured_output=True)
def request_missing_document(referral_id: Annotated[uuid.UUID, Field(description="Synthetic referral UUID")], document_type: Annotated[str, Field(min_length=2, max_length=100, description="Required document type")]) -> RequestDocumentResult:
    return _call("requestMissingDocument", {"referral_id": referral_id, "document_type": document_type})

@mcp.tool(name="findProviders", annotations=read_only, structured_output=True)
async def find_providers(specialty: Annotated[SpecialtyName, Field(description="Submitted administrative specialty")]) -> ProviderListResult:
    correlation_id = uuid.uuid4()
    return await _provider_call(lambda gateway: gateway.find_providers(specialty, False, correlation_id), correlation_id)

@mcp.tool(name="getAvailableSlots", annotations=read_only, structured_output=True)
async def get_available_slots(provider_id: Annotated[uuid.UUID, Field(description="Synthetic provider UUID")]) -> SlotListResult:
    correlation_id = uuid.uuid4()
    return await _provider_call(lambda gateway: gateway.get_available_slots(provider_id, correlation_id), correlation_id)

if __name__ == "__main__":
    mcp.run("stdio")
