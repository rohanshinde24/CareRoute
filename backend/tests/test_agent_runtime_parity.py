"""The graph runtime must be indistinguishable from the loop, except in shape.

Every case here runs under both AGENT_RUNTIME values. The claim being defended
is that LangGraph changed the orchestration and nothing else: same policy, same
ceilings, same fail-closed behaviour, same outcomes. A parity test is the only
thing that makes "we reimplemented it on a framework" a safe sentence.
"""

import pytest

from app import agent as agent_module
from app.investigation import ProviderInvestigator
from app.provider_graph import ProviderGraphInvestigator

# Reuse the exact scenarios the loop is tested with, so parity is measured
# against the real cases rather than a friendlier copy of them.
from test_agent import (
    LaterAvailabilityProviderModel,
    OutOfCandidateProviderModel,
    PrematureProviderProposalModel,
    add_rankable_provider,
    build_case,
    run,
)
from app.agent import ReferralCoordinator
from app.model_providers import DeterministicReferralModel
from app.models import ReferralState
from app.provider_gateway import LocalProviderGateway

RUNTIMES = ["legacy", "langgraph"]


@pytest.fixture
def runtime(request, monkeypatch):
    monkeypatch.setattr("app.config.settings.agent_runtime", request.param)
    return request.param


def _investigator_class(name):
    chosen = agent_module._provider_investigator(None, None, None, None, None)
    return type(chosen)


@pytest.mark.parametrize("runtime", RUNTIMES, indirect=True)
def test_the_flag_actually_selects_the_implementation(runtime):
    expected = ProviderGraphInvestigator if runtime == "langgraph" else ProviderInvestigator
    assert _investigator_class(runtime) is expected


@pytest.mark.parametrize("runtime", RUNTIMES, indirect=True)
def test_ranking_reorders_candidates_identically_under_both_runtimes(runtime, db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral)

    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.WAITING_FOR_SLOT_SELECTION
    assert len(result.provider_ids) == 2


@pytest.mark.parametrize("runtime", RUNTIMES, indirect=True)
def test_a_proposal_before_the_evidence_fails_closed_under_both_runtimes(runtime, db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral)

    result = run(ReferralCoordinator(db, PrematureProviderProposalModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW
    assert "not allowed by policy" in result.summary


@pytest.mark.parametrize("runtime", RUNTIMES, indirect=True)
def test_a_provider_outside_the_candidate_set_fails_closed_under_both_runtimes(runtime, db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral)

    result = run(ReferralCoordinator(db, OutOfCandidateProviderModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW


@pytest.mark.parametrize("runtime", RUNTIMES, indirect=True)
def test_a_proposal_that_is_not_the_earliest_fails_closed_under_both_runtimes(runtime, db, provider_db):
    referral = build_case(db, provider_db)
    add_rankable_provider(db, provider_db, referral, name="A Later Cardiologist", days_until_slot=3)
    add_rankable_provider(db, provider_db, referral, name="Z Earlier Cardiologist", days_until_slot=2)

    result = run(ReferralCoordinator(db, LaterAvailabilityProviderModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.NEEDS_HUMAN_REVIEW


@pytest.mark.parametrize("runtime", RUNTIMES, indirect=True)
def test_absent_location_preference_abstains_under_both_runtimes(runtime, db, provider_db):
    referral = build_case(db, provider_db)

    result = run(ReferralCoordinator(db, DeterministicReferralModel(), provider_gateway=LocalProviderGateway(provider_db)), referral)

    assert result.state == ReferralState.WAITING_FOR_SLOT_SELECTION


def test_the_graph_shares_the_loops_policy_rather_than_restating_it():
    """The safety argument for the graph rests on this.

    If the graph ever defines its own _available or _validate, the two runtimes
    can drift and the parity above stops meaning anything.
    """
    assert issubclass(ProviderGraphInvestigator, ProviderInvestigator)
    assert ProviderGraphInvestigator._validate is ProviderInvestigator._validate
    assert ProviderGraphInvestigator._available is ProviderInvestigator._available
    assert ProviderGraphInvestigator.max_turns == ProviderInvestigator.max_turns
    assert ProviderGraphInvestigator.max_tool_calls == ProviderInvestigator.max_tool_calls
