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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .agent import ReferralCoordinator
from .config import settings
from .database import SessionLocal
from .model_providers import DeterministicReferralModel
from .models import AgentEvent, Coverage, Patient, Referral, ReferralDocument
from .provider_database import provider_session
from .provider_models import AppointmentSlot, Provider, ProviderSchedule
from .workflow import latest_workflow

RUNTIMES = ("legacy", "langgraph")
PREFERENCE = "San Francisco"


@dataclass
class Observation:
    scenario: str
    runtime: str
    state: str
    outcome: str | None
    turns: int | None
    tool_calls: int | None
    seconds: list[float] = field(default_factory=list)


def _build(matching: int, non_matching: int = 1, earliest_last: bool = True) -> uuid.UUID:
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
            requested_specialty=f"Cardiology-cmp-{marker}",
            reason="Synthetic runtime comparison referral",
            location_preference=PREFERENCE,
            is_synthetic=True,
            is_evaluation=True,
        )
        db.add(referral)
        db.flush()
        db.add(ReferralDocument(referral_id=referral.id, document_type="clinical-note", storage_locator=f"synthetic://cmp/{marker}"))
        db.add(Coverage(patient_id=patient.id, external_id=f"cov-{marker}", source="comparison", payer_name="Synthetic Plan", member_id=f"m-{marker}", status="active", is_synthetic=True))
        db.commit()
        specialty, referral_id = referral.requested_specialty, referral.id

    with provider_session() as pdb:
        base = datetime.now(timezone.utc) + timedelta(days=2)
        for index in range(matching):
            # Descending start times so the last created holds the earliest slot
            # when earliest_last is set; the model must observe all of them first.
            offset = (matching - index) if earliest_last else (index + 1)
            _provider(pdb, specialty, f"{PREFERENCE}, CA", f"M{index}-{marker}", base + timedelta(hours=offset))
        for index in range(non_matching):
            _provider(pdb, specialty, "Evaluation, CA", f"N{index}-{marker}", base + timedelta(hours=1))
        pdb.commit()
    return referral_id


def _provider(pdb, specialty: str, location: str, name: str, start: datetime) -> None:
    record = Provider(name=f"Cmp {name}", specialty=specialty, location=location, is_synthetic=True, is_evaluation=True)
    pdb.add(record)
    pdb.flush()
    schedule = ProviderSchedule(provider_id=record.id, name=f"Schedule {name}", timezone="UTC")
    pdb.add(schedule)
    pdb.flush()
    pdb.add(AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30)))


def _run(referral_id: uuid.UUID) -> tuple[str, dict, float]:
    with SessionLocal() as db:
        started = time.perf_counter()
        result = asyncio.run(ReferralCoordinator(db, DeterministicReferralModel()).process(referral_id))
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


def compare(repetitions: int) -> list[Observation]:
    results: list[Observation] = []
    for name, options in SCENARIOS.items():
        for runtime in RUNTIMES:
            settings.agent_runtime = runtime
            observation = Observation(scenario=name, runtime=runtime, state="", outcome=None, turns=None, tool_calls=None)
            for _ in range(repetitions):
                referral_id = _build(**options)
                state, payload, elapsed = _run(referral_id)
                observation.state = state
                observation.outcome = payload.get("outcome")
                observation.turns = payload.get("turns")
                observation.tool_calls = payload.get("tool_calls")
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
            {**vars(o), "p50": statistics.median(o.seconds), "mean": statistics.fmean(o.seconds)}
            for o in results
        ], indent=2, default=str))
        return

    print(f"{'scenario':<24}{'runtime':<11}{'state':<28}{'outcome':<20}{'turns':>6}{'tools':>6}{'p50 ms':>9}")
    for observation in results:
        p50 = statistics.median(observation.seconds) * 1000
        print(
            f"{observation.scenario:<24}{observation.runtime:<11}{observation.state:<28}"
            f"{str(observation.outcome):<20}{str(observation.turns):>6}{str(observation.tool_calls):>6}{p50:>9.1f}"
        )

    print()
    disagreements = []
    by_scenario: dict[str, list[Observation]] = {}
    for observation in results:
        by_scenario.setdefault(observation.scenario, []).append(observation)
    for scenario, pair in by_scenario.items():
        legacy, graph = pair
        if (legacy.state, legacy.outcome, legacy.turns, legacy.tool_calls) != (graph.state, graph.outcome, graph.turns, graph.tool_calls):
            disagreements.append(scenario)
        overhead = (statistics.median(graph.seconds) - statistics.median(legacy.seconds)) * 1000
        print(f"  {scenario:<24} graph overhead {overhead:+.1f} ms")

    print()
    print("  PARITY FAILED: " + ", ".join(disagreements) if disagreements else "  parity: every scenario agreed on state, outcome, turns and tool calls")


if __name__ == "__main__":
    main()
