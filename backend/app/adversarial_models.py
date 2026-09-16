"""Scripted models that each break one agent policy rule, plus honest controls.

Each adversarial model returns a decision that is wrong in exactly one respect,
so a run tells you which rule caught it rather than only that something failed.
They cover all three policy surfaces: specialty, document and provider ranking. They are scripted
rather than sampled from a real model on purpose: an adversarial case has to be
reproducible to be worth asserting on, and a real model cannot be relied on to
misbehave the same way twice.

Two of these are not adversarial at all. EscalatingProviderModel and
ClarificationSeekingProviderModel behave, and a harness made only of violations
could not tell "the policy works" apart from "the policy rejects everything";
the accepting cases are what give the rejections meaning.

These live in the application rather than the test suite because the evaluation
harness uses them as scenarios, and a harness that imports test modules is a
harness nobody can run outside pytest.
"""

import uuid

from .model_providers import (
    DocumentInvestigationAction,
    DocumentInvestigationDecision,
    InvestigationAction,
    InvestigationDecision,
    ModelResponseError,
    ProviderInvestigationAction,
    ProviderInvestigationDecision,
    ReferralInterpretation,
)

class PrematureProviderProposalModel:
    name = "premature-provider-proposal"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=step.candidate_providers[0].id)


class OutOfCandidateProviderModel:
    name = "out-of-candidate-provider"

    def __init__(self):
        self.observed = False

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        if not self.observed:
            self.observed = True
            matching = next(candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location)
            return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=matching.id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=uuid.uuid4())


class UnobservedProviderModel:
    name = "unobserved-provider"

    def __init__(self):
        self.observed = False

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        matching = [candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location]
        if not self.observed:
            self.observed = True
            return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=matching[0].id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=matching[1].id)


class EmptyAvailabilityProviderModel:
    name = "empty-availability-provider"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        matching = [candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location]
        observed = step.observations.get("available_slots", {})
        for candidate in matching:
            if str(candidate.id) not in observed:
                return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=candidate.id)
        empty = next(candidate for candidate in matching if "No availability" in candidate.location)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=empty.id)


class LaterAvailabilityProviderModel:
    name = "later-availability-provider"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        matching = [candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location]
        observed = step.observations.get("available_slots", {})
        for candidate in matching:
            if str(candidate.id) not in observed:
                return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=candidate.id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.PROPOSE_PROVIDER, proposed_provider_id=matching[0].id)


class ConflictingModel:
    name = "conflicting-test-model"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Neurology", confidence=1, is_ambiguous=False, evidence=["test"])


class InvalidModel:
    name = "invalid-test-model"

    async def interpret(self, _):
        raise ModelResponseError("Invalid structured model response")


class PrematureProposalModel:
    name = "premature-proposal"

    async def interpret(self, _):
        return ReferralInterpretation(specialty=None, confidence=0, is_ambiguous=True, evidence=[])

    async def investigate(self, _):
        return InvestigationDecision(action=InvestigationAction.PROPOSE_SPECIALTY, proposed_specialty="Cardiology")


class UngroundedDocumentModel:
    name = "ungrounded-document"

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_documents(self, _):
        return DocumentInvestigationDecision(action=DocumentInvestigationAction.PROPOSE_DOCUMENT, proposed_document_type="pathology-report")


class EscalatingProviderModel:
    """Observes, then hands the ranking back rather than guessing.

    Policy permits this, so the run must *not* fail: it degrades to unranked
    human slot selection. This is the control for the provider surface - it
    proves rejections come from the rule and not from a boundary that refuses
    everything a model returns.
    """

    name = "escalating-provider"

    def __init__(self):
        self.observed = False

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        if not self.observed:
            self.observed = True
            matching = next(candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location)
            return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=matching.id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.ESCALATE)


class ClarificationSeekingProviderModel:
    """Same as escalation but asks for clarification; also permitted."""

    name = "clarification-seeking-provider"

    def __init__(self):
        self.observed = False

    async def interpret(self, _):
        return ReferralInterpretation(specialty="Cardiology", confidence=1, is_ambiguous=False, evidence=["Cardiology"])

    async def investigate_providers(self, step):
        if not self.observed:
            self.observed = True
            matching = next(candidate for candidate in step.candidate_providers if "San Francisco" in candidate.location)
            return ProviderInvestigationDecision(action=ProviderInvestigationAction.GET_AVAILABLE_SLOTS, target_provider_id=matching.id)
        return ProviderInvestigationDecision(action=ProviderInvestigationAction.REQUEST_CLARIFICATION)
