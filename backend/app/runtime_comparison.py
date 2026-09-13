"""Compare the two provider-investigation runtimes across a range of scenarios.

The first comparison used the benchmark's ranking case, which has exactly one
matching candidate. That is the degenerate path: with a single candidate there
is nothing to rank, so both runtimes spent one tool call and two turns and the
graph's routing was barely exercised.

This harness varies the thing that actually drives the investigation - how many
candidates match the location preference - across the full legal range, and adds
the fail-closed paths. Both runtimes run identical scenarios and must agree on
every outcome; latency is reported as a distribution rather than a single run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .agent import ReferralCoordinator
from .config import settings
from .database import SessionLocal
from .model_providers import configured_model
from .models import AgentEvent, Coverage, Patient, Referral, ReferralDocument
from .provider_database import provider_session
from .provider_models import AppointmentSlot, Provider, ProviderSchedule
from .workflow import latest_workflow

RUNTIMES = ("legacy", "langgraph")
PREFERENCE = "San Francisco"


@dataclass
class Observation:
    """Per-run results, not a single sample.

    With a deterministic model every run of a scenario is identical and a single
    value would do. With a real model the decisions vary, so turns, tool calls
    and outcomes are distributions and reporting one run's value would misstate
    the comparison.
    """

    scenario: str
    runtime: str
    states: list[str] = field(default_factory=list)
    outcomes: list[str | None] = field(default_factory=list)
    turns: list[int | None] = field(default_factory=list)
    tool_calls: list[int | None] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)

    @staticmethod
    def _span(values) -> str:
        present = [v for v in values if v is not None]
        if not present:
            return "-"
        low, high = min(present), max(present)
        return str(low) if low == high else f"{low}-{high}"

    @property
    def turn_span(self) -> str:
        return self._span(self.turns)

    @property
    def tool_span(self) -> str:
        return self._span(self.tool_calls)

    @property
    def outcome_summary(self) -> str:
        counts = Counter(str(o) for o in self.outcomes)
        if len(counts) == 1:
            return next(iter(counts))
        return ", ".join(f"{name}x{count}" for name, count in counts.most_common())

    @property
    def state_summary(self) -> str:
        counts = Counter(self.states)
        if len(counts) == 1:
            return next(iter(counts))
        return ", ".join(f"{name}x{count}" for name, count in counts.most_common())

    def signature(self) -> tuple:
        """What parity is judged on: the distribution, not one sample."""
        return (
            tuple(sorted(Counter(self.states).items())),
            tuple(sorted(Counter(str(o) for o in self.outcomes).items())),
        )


# A real specialty name, because a real model has to interpret it. The first
# version of this harness used a randomised string like "Cardiology-cmp-a1b2c3"
# to isolate each run's candidate set; a deterministic model echoes that back
# happily, but Ollama cannot resolve it and every run died in NEEDS_HUMAN_REVIEW
# before the investigation began. Isolation now comes from tearing the fixtures
# down after each run instead.
SPECIALTY = "Cardiology"


def _build(matching: int, non_matching: int = 1, earliest_last: bool = True) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """One referral with `matching` candidates in the preferred location.

    Slot times are staggered so exactly one candidate holds the earliest
    availability, which is what the ranking policy requires the model to find.
    """
    marker = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        patient = Patient(external_id=f"cmp-{marker}", source="comparison", given_name="Compare", family_name="Runtime", birth_date=date(1980, 1, 1), is_synthetic=True)
        db.add(patient)
        db.flush()
        referral = Referral(
            patient_id=patient.id,
            requested_specialty=SPECIALTY,
            reason="Intermittent palpitations; specialist consultation requested",
            location_preference=PREFERENCE,
            is_synthetic=True,
            is_evaluation=True,
        )
        db.add(referral)
        db.flush()
        db.add(ReferralDocument(referral_id=referral.id, document_type="clinical-note", storage_locator=f"synthetic://cmp/{marker}"))
        db.add(Coverage(patient_id=patient.id, external_id=f"cov-{marker}", source="comparison", payer_name="Synthetic Plan", member_id=f"m-{marker}", status="active", is_synthetic=True))
        db.commit()
        referral_id = referral.id

    provider_ids: list[uuid.UUID] = []
    with provider_session() as pdb:
        base = datetime.now(timezone.utc) + timedelta(days=2)
        for index in range(matching):
            # Descending start times so the last created holds the earliest slot
            # when earliest_last is set; the model must observe all of them first.
            offset = (matching - index) if earliest_last else (index + 1)
            provider_ids.append(_provider(pdb, SPECIALTY, f"{PREFERENCE}, CA", f"M{index}-{marker}", base + timedelta(hours=offset)))
        for index in range(non_matching):
            provider_ids.append(_provider(pdb, SPECIALTY, "Evaluation, CA", f"N{index}-{marker}", base + timedelta(hours=1)))
        pdb.commit()
    return referral_id, provider_ids


def _teardown(referral_id: uuid.UUID, provider_ids: list[uuid.UUID]) -> None:
    """Remove a run's fixtures so the next run sees the candidate set it asked for.

    Every scenario now shares one specialty, so without this the candidate sets
    would accumulate across repetitions and "2 matching candidates" would quietly
    become four, then six.
    """
    from sqlalchemy import text

    with provider_session() as pdb:
        for provider_id in provider_ids:
            pdb.execute(text("DELETE FROM appointment_slots WHERE schedule_id IN (SELECT id FROM provider_schedules WHERE provider_id = :p)"), {"p": provider_id})
            pdb.execute(text("DELETE FROM provider_schedules WHERE provider_id = :p"), {"p": provider_id})
            pdb.execute(text("DELETE FROM providers WHERE id = :p"), {"p": provider_id})
        pdb.commit()


def _provider(pdb, specialty: str, location: str, name: str, start: datetime) -> uuid.UUID:
    record = Provider(name=f"Cmp {name}", specialty=specialty, location=location, is_synthetic=True, is_evaluation=True)
    pdb.add(record)
    pdb.flush()
    schedule = ProviderSchedule(provider_id=record.id, name=f"Schedule {name}", timezone="UTC")
    pdb.add(schedule)
    pdb.flush()
    pdb.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))
    return record.id


def _run(referral_id: uuid.UUID) -> tuple[str, dict, float]:
    with SessionLocal() as db:
        started = time.perf_counter()
        # Honour MODEL_PROVIDER. Hardcoding the deterministic model here would
        # silently report deterministic timings under any model setting.
        result = asyncio.run(ReferralCoordinator(db, configured_model()).process(referral_id))
        elapsed = time.perf_counter() - started
        run = latest_workflow(db, referral_id)
        events = db.query(AgentEvent).filter_by(workflow_run_id=run.id).all()
        completed = [e for e in events if e.event_type == "provider_investigation_completed"]
        return result.state.value, (completed[0].payload if completed else {}), elapsed


# The coordinator only opens a provider investigation when there is an explicit
# location preference AND at least two eligible providers, so every scenario
# carries enough non-matching providers to clear that bar. Without it the
# investigation is skipped entirely and the comparison measures nothing.
#
# max_tool_calls is 3, and a matching set larger than that short-circuits to
# candidate_limit before any model turn. So 1-3 matching candidates is the whole
# legal range of the ranking path, and 4 is the ceiling case.
SCENARIOS = {
    "rank-1-candidate": dict(matching=1, non_matching=1),
    "rank-2-candidates": dict(matching=2, non_matching=1),
    "rank-3-candidates": dict(matching=3, non_matching=1),
    "over-candidate-ceiling": dict(matching=4, non_matching=1),
    "no-location-match": dict(matching=0, non_matching=2),
}


def _purge_stale_fixtures() -> None:
    """Remove evaluation providers left behind by earlier runs.

    Every scenario shares one specialty now, so a crashed run's leftovers would
    silently inflate the next run's candidate set: "2 matching candidates" would
    become four and the comparison would measure a scenario nobody asked for.
    """
    from sqlalchemy import text

    with provider_session() as pdb:
        pdb.execute(text("""
            DELETE FROM appointment_slots WHERE schedule_id IN (
                SELECT s.id FROM provider_schedules s JOIN providers p ON p.id = s.provider_id
                WHERE p.is_evaluation AND p.specialty ILIKE :specialty AND p.name LIKE 'Cmp %'
            )"""), {"specialty": SPECIALTY})
        pdb.execute(text("""
            DELETE FROM provider_schedules WHERE provider_id IN (
                SELECT id FROM providers WHERE is_evaluation AND specialty ILIKE :specialty AND name LIKE 'Cmp %'
            )"""), {"specialty": SPECIALTY})
        removed = pdb.execute(text("""
            DELETE FROM providers WHERE is_evaluation AND specialty ILIKE :specialty AND name LIKE 'Cmp %'
            """), {"specialty": SPECIALTY}).rowcount
        pdb.commit()
    if removed:
        print(f"  (purged {removed} stale comparison providers before starting)")


def compare(repetitions: int) -> list[Observation]:
    _purge_stale_fixtures()
    results: list[Observation] = []
    for name, options in SCENARIOS.items():
        for runtime in RUNTIMES:
            settings.agent_runtime = runtime
            observation = Observation(scenario=name, runtime=runtime)
            for _ in range(repetitions):
                referral_id, provider_ids = _build(**options)
                try:
                    state, payload, elapsed = _run(referral_id)
                finally:
                    _teardown(referral_id, provider_ids)
                observation.states.append(state)
                observation.outcomes.append(payload.get("outcome"))
                observation.turns.append(payload.get("turns"))
                observation.tool_calls.append(payload.get("tool_calls"))
                observation.seconds.append(elapsed)
            results.append(observation)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare provider-investigation runtimes")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = compare(args.repetitions)

    if args.json:
        print(json.dumps([
            {**vars(o), "p50": statistics.median(o.seconds), "mean": statistics.fmean(o.seconds), "model_provider": settings.model_provider}
            for o in results
        ], indent=2, default=str))
        return

    print(f"model provider: {settings.model_provider}    repetitions: {len(results[0].seconds) if results else 0}")
    print()
    print(f"{'scenario':<24}{'runtime':<11}{'state':<28}{'outcome':<26}{'turns':>7}{'tools':>7}{'p50 ms':>10}")
    for observation in results:
        p50 = statistics.median(observation.seconds) * 1000
        print(
            f"{observation.scenario:<24}{observation.runtime:<11}{observation.state_summary:<28}"
            f"{observation.outcome_summary:<26}{observation.turn_span:>7}{observation.tool_span:>7}{p50:>10.1f}"
        )

    print()
    disagreements = []
    by_scenario: dict[str, list[Observation]] = {}
    for observation in results:
        by_scenario.setdefault(observation.scenario, []).append(observation)
    for scenario, pair in by_scenario.items():
        legacy, graph = pair
        if legacy.signature() != graph.signature():
            disagreements.append(scenario)
        overhead = (statistics.median(graph.seconds) - statistics.median(legacy.seconds)) * 1000
        print(f"  {scenario:<24} graph overhead {overhead:+.1f} ms")

    print()
    if disagreements:
        print("  PARITY DIFFERS: " + ", ".join(disagreements))
        print("  With a stochastic model this is expected to be noisy; compare the distributions above")
        print("  rather than treating a single differing run as a regression.")
    else:
        print("  parity: both runtimes produced the same distribution of states and outcomes")


if __name__ == "__main__":
    main()
