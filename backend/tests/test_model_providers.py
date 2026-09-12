import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.model_providers import GeminiReferralModel, ModelResponseError, OllamaReferralModel, ProviderCandidate, ProviderInvestigationAction, ProviderInvestigationDecision, ProviderInvestigationInput, ReferralInterpretation, ReferralModelInput

def test_ollama_adapter_sends_schema_and_validates_output(monkeypatch):
    captured = {}
    interpretation = {"specialty": "Cardiology", "confidence": 0.9, "is_ambiguous": False, "evidence": ["requested specialty"]}

    def handler(request: httpx.Request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(interpretation)}})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.model_providers.httpx.AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    model = OllamaReferralModel(Settings(ollama_model="test-model"))
    result = asyncio.run(model.interpret(ReferralModelInput(requested_specialty="Cardiology", reason="Synthetic referral")))
    assert result.specialty == "Cardiology"
    assert captured["format"]["type"] == "object"
    assert captured["stream"] is False

def test_gemini_adapter_rejects_nonconforming_output(monkeypatch):
    def handler(_: httpx.Request):
        return httpx.Response(200, json={"output_text": '{"specialty":"Cardiology","unexpected":true}'})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.model_providers.httpx.AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    model = GeminiReferralModel(Settings(gemini_api_key="test-key"))
    with pytest.raises(ModelResponseError):
        asyncio.run(model.interpret(ReferralModelInput(requested_specialty="Cardiology", reason="Synthetic referral")))

def test_structured_interpretation_rejects_uuid_specialty():
    with pytest.raises(ValidationError):
        ReferralInterpretation(specialty="fcc61c2e-6df2-4404-9808-996c6fd18c95", confidence=1, is_ambiguous=False, evidence=[])

def test_ollama_provider_schema_is_restricted_to_current_actions(monkeypatch):
    captured = {}

    def handler(request: httpx.Request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": json.dumps({"action": "REQUEST_CLARIFICATION", "target_provider_id": None, "proposed_provider_id": None})}})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.model_providers.httpx.AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    step = ProviderInvestigationInput(
        referral_id="9559ffcf-376f-41a6-9590-a91e9ef98315",
        location_preference="San Francisco",
        candidate_providers=[ProviderCandidate(id="1a0a44a5-bf31-4b07-a272-a3321d97164d", location="San Francisco, CA")],
        available_actions=[ProviderInvestigationAction.REQUEST_CLARIFICATION, ProviderInvestigationAction.ESCALATE],
        observations={"available_slots": {}},
    )

    result = asyncio.run(OllamaReferralModel(Settings(ollama_model="test-model")).investigate_providers(step))

    assert result == ProviderInvestigationDecision(action=ProviderInvestigationAction.REQUEST_CLARIFICATION)
    assert captured["format"]["$defs"]["ProviderInvestigationAction"]["enum"] == ["REQUEST_CLARIFICATION", "ESCALATE"]
    assert captured["format"]["properties"]["target_provider_id"]["type"] == "null"
