"""The relay killed mid-dispatch: nothing lost, nothing applied twice.

The relay publishes and then marks the outbox row dispatched, in that order, on
the argument that a crash between the two should cost a duplicate rather than a
lost event. Until this test that was a claim in a docstring. Here a real relay
process is killed with SIGKILL at each point where the ordering matters, a clean
relay is started afterwards, and the real consumer reads the result.

  before_publish  nothing reached the broker; the restart publishes once
  after_publish   the broker has the event but the mark died with the process;
                  the restart publishes it again, and the consumer must absorb it

In both cases the referral must end CONFIRMED exactly once, with one audit row
and one consumed-event record. The first case is also what would catch the
ordering being reversed: with mark-then-publish, a crash there marks an event
that was never sent, and it is gone for good.

Requires both PostgreSQL databases and Redis; fails rather than skips in CI.
"""

from __future__ import annotations

import pathlib
import signal
import subprocess
import sys
import time
import uuid

import pytest
from sqlalchemy import delete, select, text

from app import consumer
from app.models import AgentEvent, ConsumedEvent, Referral, ReferralState, WorkflowRun
from app.provider_models import Appointment, ProviderOutbox
from tests.test_reconciler_event_loss import (  # noqa: F401 - fixtures
    PROVIDER_URL,
    REDIS_URL,
    _book,
    broker,
    provider_factory,
    referral_factory,
    scenario,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _relay(row_id: uuid.UUID, stream: str, crash_at: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tests.relay_crash_child", PROVIDER_URL, REDIS_URL, stream, str(row_id), crash_at],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _restart_until_dispatched(row_id: uuid.UUID, stream: str) -> None:
    """Start a clean relay, allowing for PostgreSQL to notice the dead one.

    The killed relay held the row under FOR UPDATE. PostgreSQL releases that
    lock when it sees the connection drop, which on a local socket is prompt but
    not synchronous with the kill, and SKIP LOCKED will pass over the row until
    then. A restart that finds nothing is retried; one that never finds it fails.
    """
    for _ in range(20):
        run = _relay(row_id, stream, "none")
        assert run.returncode == 0, f"clean relay failed: {run.stderr[-2000:]}"
        if run.stdout.strip() == "1":
            return
        time.sleep(0.25)
    pytest.fail("the restarted relay never picked the row up; its lock was never released")


def _deliveries(broker, stream: str, row_id: uuid.UUID) -> int:
    return sum(1 for _id, fields in broker.xrange(stream) if fields.get("event_id") == str(row_id))


def _outbox_row(provider_factory, scenario) -> uuid.UUID:
    with provider_factory() as pdb:
        row_id = pdb.execute(
            text("SELECT id FROM provider_outbox WHERE payload->>'slot_id' = :s AND event_type = 'appointment.booked'"),
            {"s": str(scenario["slot"])},
        ).scalar_one()
    return row_id


@pytest.mark.parametrize(
    "crash_at, expected_deliveries",
    [("before_publish", 1), ("after_publish", 2)],
)
def test_a_relay_killed_mid_dispatch_loses_nothing_and_applies_once(
    referral_factory, provider_factory, broker, scenario, monkeypatch, crash_at, expected_deliveries
):
    _book(provider_factory, scenario)
    row_id = _outbox_row(provider_factory, scenario)
    # A stream of its own, so the consumer reads this event and nothing else.
    stream = f"careroute.events.crashtest.{scenario['marker']}"

    try:
        killed = _relay(row_id, stream, crash_at)
        assert killed.returncode == -signal.SIGKILL, f"the relay should have been killed, got {killed.returncode}: {killed.stderr[-2000:]}"

        with provider_factory() as pdb:
            assert pdb.get(ProviderOutbox, row_id).dispatched_at is None, "the mark must have died with the process"
        assert _deliveries(broker, stream, row_id) == expected_deliveries - 1

        _restart_until_dispatched(row_id, stream)

        with provider_factory() as pdb:
            assert pdb.get(ProviderOutbox, row_id).dispatched_at is not None
        assert _deliveries(broker, stream, row_id) == expected_deliveries, "an event must be neither lost nor published a third time"

        monkeypatch.setattr(consumer, "STREAM", stream)
        consumer.ensure_group(broker)
        outcomes = consumer.poll(referral_factory, broker, block_ms=100)

        assert outcomes.get("reconciled", 0) == 1, f"exactly one delivery should take effect, got {outcomes}"
        assert outcomes.get("duplicate", 0) == expected_deliveries - 1, f"every redelivery must be absorbed, got {outcomes}"
        assert broker.xpending(stream, consumer.GROUP)["pending"] == 0, "every delivery, duplicates included, must be acknowledged"

        with referral_factory() as rdb:
            assert rdb.get(Referral, scenario["referral"]).state is ReferralState.CONFIRMED
            consumed = rdb.scalars(select(ConsumedEvent).where(ConsumedEvent.event_id == row_id)).all()
            assert len(consumed) == 1
            run = rdb.scalar(select(WorkflowRun).where(WorkflowRun.referral_id == scenario["referral"]))
            reconciled = [
                e for e in rdb.scalars(select(AgentEvent).where(AgentEvent.workflow_run_id == run.id)).all()
                if e.event_type == "booking_reconciled"
            ]
            assert len(reconciled) == 1, "the effect must be applied once, not once per delivery"
            assert reconciled[0].payload.get("source") == "appointment.booked"

        with provider_factory() as pdb:
            assert len(pdb.scalars(select(Appointment).where(Appointment.slot_id == scenario["slot"])).all()) == 1
    finally:
        broker.delete(stream)
        with referral_factory() as rdb:
            rdb.execute(delete(ConsumedEvent).where(ConsumedEvent.event_id == row_id))
            rdb.commit()
