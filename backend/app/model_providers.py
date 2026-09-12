import json
import enum
import uuid
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings, settings
from .schemas import SpecialtyName

class ModelResponseError(RuntimeError):
    pass

class TransientModelError(RuntimeError):
    pass

class ReferralModelInput(BaseModel):
    requested_specialty: SpecialtyName
    reason: str

class ReferralInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    specialty: SpecialtyName | None = Field(description="Administrative specialty explicitly supported by the referral")
    confidence: float = Field(ge=0, le=1)
    is_ambiguous: bool
    evidence: list[str] = Field(max_length=3, description="Short evidence phrases; never add facts")

class InvestigationAction(str, enum.Enum):
    GET_DOCUMENTS = "GET_DOCUMENTS"
    GET_REFERRAL_HISTORY = "GET_REFERRAL_HISTORY"
    PROPOSE_SPECIALTY = "PROPOSE_SPECIALTY"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    ESCALATE = "ESCALATE"

class InvestigationInput(BaseModel):
    referral_reason: str
    submitted_specialty: str
    candidate_specialties: list[str]
    available_actions: list[InvestigationAction]
    observations: dict

class InvestigationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: InvestigationAction
    proposed_specialty: SpecialtyName | None = None

class DocumentInvestigationAction(str, enum.Enum):
    GET_RECENT_PROCEDURES = "GET_RECENT_PROCEDURES"
    GET_REFERRAL_HISTORY = "GET_REFERRAL_HISTORY"
    PROPOSE_DOCUMENT = "PROPOSE_DOCUMENT"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    ESCALATE = "ESCALATE"

class DocumentInvestigationInput(BaseModel):
    referral_reason: str
    specialty: str
    current_document_types: list[str]
    candidate_document_types: list[str]
    available_actions: list[DocumentInvestigationAction]
    observations: dict

class DocumentInvestigationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: DocumentInvestigationAction
    proposed_document_type: str | None = Field(default=None, min_length=2, max_length=100)

class ProviderInvestigationAction(str, enum.Enum):
    GET_AVAILABLE_SLOTS = "GET_AVAILABLE_SLOTS"
    PROPOSE_PROVIDER = "PROPOSE_PROVIDER"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    ESCALATE = "ESCALATE"

class ProviderCandidate(BaseModel):
    id: uuid.UUID
    location: str

class ProviderInvestigationInput(BaseModel):
    referral_id: uuid.UUID
    location_preference: str
    candidate_providers: list[ProviderCandidate]
    available_actions: list[ProviderInvestigationAction]
    observations: dict

class ProviderInvestigationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ProviderInvestigationAction
    target_provider_id: uuid.UUID | None = None
    proposed_provider_id: uuid.UUID | None = None

class ReferralModel(Protocol):
    name: str

    async def interpret(self, referral: ReferralModelInput) -> ReferralInterpretation: ...
    async def investigate(self, step: InvestigationInput) -> InvestigationDecision: ...
    async def investigate_documents(self, step: DocumentInvestigationInput) -> DocumentInvestigationDecision: ...
    async def investigate_providers(self, step: ProviderInvestigationInput) -> ProviderInvestigationDecision: ...

class DeterministicReferralModel:
    name = "deterministic"

    async def interpret(self, referral: ReferralModelInput) -> ReferralInterpretation:
        specialty = referral.requested_specialty.strip()
        ambiguous = specialty.casefold() in {"unknown", "unspecified", "other", "tbd"}
        return ReferralInterpretation(specialty=None if ambiguous else specialty, confidence=0 if ambiguous else 1, is_ambiguous=ambiguous, evidence=["submitted requested specialty"] if not ambiguous else [])

    async def investigate(self, step: InvestigationInput) -> InvestigationDecision:
        evidence_text = " ".join([step.referral_reason, json.dumps(step.observations)]).casefold()
        for specialty in step.candidate_specialties:
            if specialty.casefold() in evidence_text and InvestigationAction.PROPOSE_SPECIALTY in step.available_actions:
                return InvestigationDecision(action=InvestigationAction.PROPOSE_SPECIALTY, proposed_specialty=specialty)
        action = next((candidate for candidate in step.available_actions if candidate in {InvestigationAction.GET_DOCUMENTS, InvestigationAction.GET_REFERRAL_HISTORY}), InvestigationAction.REQUEST_CLARIFICATION)
        return InvestigationDecision(action=action)

    async def investigate_documents(self, step: DocumentInvestigationInput) -> DocumentInvestigationDecision:
        if DocumentInvestigationAction.PROPOSE_DOCUMENT in step.available_actions:
            reason = step.referral_reason.casefold()
            for procedure in step.observations.get("recent_procedures", []):
                document_type = procedure["report_document_type"]
                if document_type in step.candidate_document_types and document_type.casefold() not in {item.casefold() for item in step.current_document_types} and procedure["procedure_type"].casefold() in reason:
                    return DocumentInvestigationDecision(action=DocumentInvestigationAction.PROPOSE_DOCUMENT, proposed_document_type=document_type)
        action = next((candidate for candidate in step.available_actions if candidate in {DocumentInvestigationAction.GET_RECENT_PROCEDURES, DocumentInvestigationAction.GET_REFERRAL_HISTORY}), DocumentInvestigationAction.REQUEST_CLARIFICATION)
        return DocumentInvestigationDecision(action=action)

    async def investigate_providers(self, step: ProviderInvestigationInput) -> ProviderInvestigationDecision:
        matching = [candidate for candidate in step.candidate_providers if step.location_preference.casefold() in candidate.location.casefold()]
        for candidate in matching:
            slots = step.observations.get("available_slots", {}).get(str(candidate.id))
            if slots is None and ProviderInvestigationAction.GET_AVAILABLE_SLOTS in step.available_actions:
                return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=candidate.id)
        ranked = [
            (min(slot["start_at"] for slot in step.observations["available_slots"][str(candidate.id)]), candidate.id)
            for candidate in matching
            if step.observations.get("available_slots", {}).get(str(candidate.id))
        ]
        if ranked and ProviderInvestigationAction.PROPOSE_PROVIDER in step.available_actions:
            return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=min(ranked, key=lambda item: (item[0], str(item[1])))[1])
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.REQUEST_CLARIFICATION)

def _prompt(referral: ReferralModelInput) -> str:
    payload = referral.model_dump_json()
    return (
        "Interpret administrative referral intent only. Do not diagnose, recommend treatment, or add facts. "
        "Return a specialty only when supported by the submitted requested specialty and referral reason. "
        "Mark ambiguity when the specialty is absent, conflicting, or unsupported. Referral: " + payload
    )

def _decision_prompt(step: InvestigationInput) -> str:
    return (
        "Investigate ambiguous administrative specialty routing. Choose exactly one semantic action from available_actions. "
        "Gather evidence when needed, propose only a candidate_specialty explicitly supported by the supplied evidence, "
        "or request clarification/escalate. Return only the selected action and proposed_specialty (null unless proposing). "
        "Never diagnose, book, change state, or invent facts. Input: "
        + step.model_dump_json()
    )

def _document_decision_prompt(step: DocumentInvestigationInput) -> str:
    return (
        "Investigate whether an administrative referral references a completed procedure whose report is missing. "
        "Choose exactly one semantic action from available_actions. Gather evidence when needed. Propose only a "
        "candidate_document_type explicitly supported by recent procedure observations and absent from current documents. "
        "Otherwise request clarification or escalate. Return only action and proposed_document_type (null unless proposing). "
        "Never diagnose, book, change state, or invent facts. Input: " + step.model_dump_json()
    )

def _provider_decision_prompt(step: ProviderInvestigationInput) -> str:
    return (
        "Rank deterministically eligible providers using only the explicit administrative location preference and observed availability. "
        "Choose exactly one semantic action from available_actions. With GET_AVAILABLE_SLOTS, target only a matching provider not already present in observations.available_slots. "
        "Inspect every location-matching candidate, then propose the one with the earliest observed free slot. "
        "Otherwise request clarification or escalate. Return null for provider ID fields not used by the selected action. "
        "Never select a slot, persist a patient choice, book, diagnose, change state, or invent facts. Input: "
        + step.model_dump_json()
    )

def _provider_decision_schema(step: ProviderInvestigationInput) -> dict:
    schema = ProviderInvestigationDecision.model_json_schema()
    schema["$defs"]["ProviderInvestigationAction"]["enum"] = [action.value for action in step.available_actions]
    matching = [candidate for candidate in step.candidate_providers if step.location_preference.casefold() in candidate.location.casefold()]
    observed = step.observations.get("available_slots", {})
    target_ids = [str(candidate.id) for candidate in matching if str(candidate.id) not in observed]
    proposed_ids = [str(candidate.id) for candidate in matching if observed.get(str(candidate.id))]
    schema["properties"]["target_provider_id"] = ({"anyOf": [{"type": "string", "format": "uuid", "enum": target_ids}, {"type": "null"}], "default": None} if ProviderInvestigationAction.GET_AVAILABLE_SLOTS in step.available_actions else {"type": "null", "default": None})
    schema["properties"]["proposed_provider_id"] = ({"anyOf": [{"type": "string", "format": "uuid", "enum": proposed_ids}, {"type": "null"}], "default": None} if ProviderInvestigationAction.PROPOSE_PROVIDER in step.available_actions else {"type": "null", "default": None})
    return schema

class OllamaReferralModel:
    name = "ollama"

    def __init__(self, config: Settings = settings):
        self.config = config

    async def interpret(self, referral: ReferralModelInput) -> ReferralInterpretation:
        schema = ReferralInterpretation.model_json_schema()
        body = {"model": self.config.ollama_model, "messages": [{"role": "user", "content": _prompt(referral) + " JSON schema: " + json.dumps(schema)}], "stream": False, "format": schema, "options": {"temperature": 0, "num_predict": 256}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post(f"{self.config.ollama_base_url.rstrip('/')}/api/chat", json=body)
                response.raise_for_status()
            content = response.json()["message"]["content"]
            return ReferralInterpretation.model_validate_json(content)
        except httpx.TimeoutException as exc:
            raise TransientModelError("Ollama request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Ollama returned no valid structured interpretation") from exc

    async def investigate(self, step: InvestigationInput) -> InvestigationDecision:
        schema = InvestigationDecision.model_json_schema()
        body = {"model": self.config.ollama_model, "messages": [{"role": "user", "content": _decision_prompt(step) + " JSON schema: " + json.dumps(schema)}], "stream": False, "format": schema, "options": {"temperature": 0, "num_predict": 256}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post(f"{self.config.ollama_base_url.rstrip('/')}/api/chat", json=body)
                response.raise_for_status()
            return InvestigationDecision.model_validate_json(response.json()["message"]["content"])
        except httpx.TimeoutException as exc:
            raise TransientModelError("Ollama decision request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Ollama returned no valid structured action") from exc

    async def investigate_documents(self, step: DocumentInvestigationInput) -> DocumentInvestigationDecision:
        schema = DocumentInvestigationDecision.model_json_schema()
        body = {"model": self.config.ollama_model, "messages": [{"role": "user", "content": _document_decision_prompt(step) + " JSON schema: " + json.dumps(schema)}], "stream": False, "format": schema, "options": {"temperature": 0, "num_predict": 256}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post(f"{self.config.ollama_base_url.rstrip('/')}/api/chat", json=body)
                response.raise_for_status()
            return DocumentInvestigationDecision.model_validate_json(response.json()["message"]["content"])
        except httpx.TimeoutException as exc:
            raise TransientModelError("Ollama document decision request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Ollama returned no valid structured document action") from exc

    async def investigate_providers(self, step: ProviderInvestigationInput) -> ProviderInvestigationDecision:
        schema = _provider_decision_schema(step)
        body = {"model": self.config.ollama_model, "messages": [{"role": "user", "content": _provider_decision_prompt(step) + " JSON schema: " + json.dumps(schema)}], "stream": False, "format": schema, "options": {"temperature": 0, "num_predict": 256}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post(f"{self.config.ollama_base_url.rstrip('/')}/api/chat", json=body)
                response.raise_for_status()
            return ProviderInvestigationDecision.model_validate_json(response.json()["message"]["content"])
        except httpx.TimeoutException as exc:
            raise TransientModelError("Ollama provider decision request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Ollama returned no valid structured provider action") from exc

class GeminiReferralModel:
    name = "gemini"

    def __init__(self, config: Settings = settings):
        if not config.gemini_api_key:
            raise ModelResponseError("GEMINI_API_KEY is required for the Gemini provider")
        self.config = config

    async def interpret(self, referral: ReferralModelInput) -> ReferralInterpretation:
        body = {"model": self.config.gemini_model, "input": _prompt(referral), "response_format": {"type": "text", "mime_type": "application/json", "schema": ReferralInterpretation.model_json_schema()}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post("https://generativelanguage.googleapis.com/v1beta/interactions", headers={"x-goog-api-key": self.config.gemini_api_key}, json=body)
                response.raise_for_status()
            return ReferralInterpretation.model_validate_json(_gemini_output_text(response.json()))
        except httpx.TimeoutException as exc:
            raise TransientModelError("Gemini request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Gemini returned no valid structured interpretation") from exc

    async def investigate(self, step: InvestigationInput) -> InvestigationDecision:
        body = {"model": self.config.gemini_model, "input": _decision_prompt(step), "response_format": {"type": "text", "mime_type": "application/json", "schema": InvestigationDecision.model_json_schema()}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post("https://generativelanguage.googleapis.com/v1beta/interactions", headers={"x-goog-api-key": self.config.gemini_api_key}, json=body)
                response.raise_for_status()
            return InvestigationDecision.model_validate_json(_gemini_output_text(response.json()))
        except httpx.TimeoutException as exc:
            raise TransientModelError("Gemini decision request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Gemini returned no valid structured action") from exc

    async def investigate_documents(self, step: DocumentInvestigationInput) -> DocumentInvestigationDecision:
        body = {"model": self.config.gemini_model, "input": _document_decision_prompt(step), "response_format": {"type": "text", "mime_type": "application/json", "schema": DocumentInvestigationDecision.model_json_schema()}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post("https://generativelanguage.googleapis.com/v1beta/interactions", headers={"x-goog-api-key": self.config.gemini_api_key}, json=body)
                response.raise_for_status()
            return DocumentInvestigationDecision.model_validate_json(_gemini_output_text(response.json()))
        except httpx.TimeoutException as exc:
            raise TransientModelError("Gemini document decision request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Gemini returned no valid structured document action") from exc

    async def investigate_providers(self, step: ProviderInvestigationInput) -> ProviderInvestigationDecision:
        body = {"model": self.config.gemini_model, "input": _provider_decision_prompt(step), "response_format": {"type": "text", "mime_type": "application/json", "schema": _provider_decision_schema(step)}}
        try:
            async with httpx.AsyncClient(timeout=self.config.model_timeout_seconds) as client:
                response = await client.post("https://generativelanguage.googleapis.com/v1beta/interactions", headers={"x-goog-api-key": self.config.gemini_api_key}, json=body)
                response.raise_for_status()
            return ProviderInvestigationDecision.model_validate_json(_gemini_output_text(response.json()))
        except httpx.TimeoutException as exc:
            raise TransientModelError("Gemini provider decision request timed out") from exc
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            raise ModelResponseError("Gemini returned no valid structured provider action") from exc

def _gemini_output_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for output in payload.get("outputs", payload.get("output", [])):
        if isinstance(output, dict):
            if isinstance(output.get("text"), str):
                return output["text"]
            for content in output.get("content", []):
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    return content["text"]
    raise KeyError("output_text")

def configured_model(config: Settings = settings) -> ReferralModel:
    providers = {"deterministic": DeterministicReferralModel, "ollama": OllamaReferralModel, "gemini": GeminiReferralModel}
    provider = providers.get(config.model_provider.casefold())
    if provider is None:
        raise ModelResponseError(f"Unsupported model provider: {config.model_provider}")
    return provider() if provider is DeterministicReferralModel else provider(config)
