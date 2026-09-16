"""The evaluation harness is only worth running if it can fail.

The first test says the boundary holds on every scenario. On its own that is a
weak claim: a harness that scored everything "ok" unconditionally would produce
exactly the same output. The second test removes one policy rule and asserts the
harness notices, which is what makes the first result mean something.
"""

import pytest

from app import agent_eval
from app.investigation import InvestigationPolicyError, ProviderInvestigator


@pytest.fixture
def harness(monkeypatch, db):
    """Point the harness at the test referral database.

    provider_session() is already redirected for every test; SessionLocal is not,
    and without this the harness would build its fixtures in the configured
    development database.
    """
    from tests.conftest import _NonClosing

    monkeypatch.setattr(agent_eval, "SessionLocal", lambda: _NonClosing(db))
    return agent_eval


def test_every_policy_scenario_behaves_as_specified(harness):
    results = harness.evaluate()

    failures = [f"{r.scenario.name} ({r.runtime}): {r.verdict} - {r.summary}" for r in results if not r.passed]
    assert not failures, "\n".join(failures)
    # Guards against a silently shrinking scenario list: the value of the run is
    # its coverage, and a harness reduced to one case would still report "all
    # runs behaved as specified".
    assert {r.scenario.surface for r in results} == {"specialty", "document", "provider"}
    assert sum(1 for r in results if r.scenario.disposition == agent_eval.REJECTED) >= 13
    assert sum(1 for r in results if r.scenario.disposition in (agent_eval.ACCEPTED, agent_eval.ABSTAINED)) >= 8
    assert {r.runtime for r in results if r.scenario.runtime_sensitive} == {"legacy", "langgraph"}


def test_harness_reports_a_removed_policy_rule(harness, monkeypatch):
    """Delete the earliest-availability rule; the harness must call it out.

    The verdict matters as much as the failure. A run that loses this rule ends
    in WAITING_FOR_SLOT_SELECTION with the model's pick applied, so the harness
    should report a wrong state rather than a wrong rule.
    """
    original = ProviderInvestigator._validate

    def without_ranking_rule(self, decision, available, candidates, preference, observations):
        try:
            original(self, decision, available, candidates, preference, observations)
        except InvestigationPolicyError as exc:
            if "earliest observed availability" in str(exc):
                return
            raise

    monkeypatch.setattr(ProviderInvestigator, "_validate", without_ranking_rule)

    results = harness.evaluate(["provider-not-earliest-availability"])

    assert results, "scenario selection returned nothing"
    assert all(not r.passed for r in results)
    assert {r.verdict for r in results} == {"WRONG STATE"}


def test_harness_reports_a_violation_caught_by_the_wrong_rule(harness, monkeypatch):
    """Keep the boundary closed but trip the wrong rule.

    This is the case a coarser harness gets wrong: the run still fails closed and
    still lands in NEEDS_HUMAN_REVIEW, so asserting on the state alone would pass
    it. Naming the expected rule is what separates a boundary that works from one
    that happens to refuse for an unrelated reason.
    """
    original = ProviderInvestigator._validate

    def with_swapped_message(self, decision, available, candidates, preference, observations):
        try:
            original(self, decision, available, candidates, preference, observations)
        except InvestigationPolicyError as exc:
            if "no observed free-slot availability" in str(exc):
                raise InvestigationPolicyError("Proposed provider is not in the deterministic candidate set") from exc
            raise

    monkeypatch.setattr(ProviderInvestigator, "_validate", with_swapped_message)

    results = harness.evaluate(["provider-without-free-slots"])

    assert results
    assert all(r.state == "NEEDS_HUMAN_REVIEW" for r in results), "the boundary should still fail closed"
    assert {r.verdict for r in results} == {"WRONG RULE"}
