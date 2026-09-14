"""Concurrency stress for the exactly-once booking guarantee.

The unit tests prove the guarantee with a handful of racing writers, which is
enough to catch an obvious mistake and not enough to trust. This drives it at
volume against real PostgreSQL, under two kinds of pressure at once:

  contention - many workers racing for the same slot, where at most one may win
  replay     - the same idempotency key resubmitted, which must return the
               original decision rather than book again

Both failure modes are checked against the database afterwards, not inferred
from what the callers were told: a caller can be told anything, but the number
of appointment rows per slot is the actual guarantee.
"""

from __future__ import annotations

import argparse
import random
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from .config import settings
from .provider_contracts import BookingRequest
from .provider_models import Appointment, AppointmentSlot, Provider, ProviderSchedule, SlotStatus
from .provider_queries import book_slot

SPECIALTY = "Cardiology"
MARKER = "Stress"


def _setup(factory, slots: int) -> list[uuid.UUID]:
    """One provider, one schedule, `slots` free slots to contend over."""
    with factory() as db:
        provider = Provider(name=f"{MARKER} Clinic {uuid.uuid4().hex[:6]}", specialty=SPECIALTY, location="Testville", is_synthetic=True, is_evaluation=True)
        db.add(provider)
        db.flush()
        schedule = ProviderSchedule(provider_id=provider.id, name=f"{MARKER} schedule", timezone="UTC")
        db.add(schedule)
        db.flush()
        base = datetime.now(timezone.utc) + timedelta(days=900)
        created = []
        for index in range(slots):
            start = base + timedelta(minutes=30 * index)
            slot = AppointmentSlot(schedule_id=schedule.id, start_at=start, end_at=start + timedelta(minutes=30), status=SlotStatus.FREE)
            db.add(slot)
            db.flush()
            created.append(slot.id)
        db.commit()
        return created, provider.id, schedule.id


def _teardown(factory, provider_id, schedule_id) -> None:
    with factory() as db:
        db.execute(text("DELETE FROM booking_attempts WHERE slot_id IN (SELECT id FROM appointment_slots WHERE schedule_id = :s)"), {"s": schedule_id})
        db.execute(text("DELETE FROM provider_outbox WHERE payload->>'slot_id' IN (SELECT id::text FROM appointment_slots WHERE schedule_id = :s)"), {"s": schedule_id})
        db.execute(text("DELETE FROM appointments WHERE slot_id IN (SELECT id FROM appointment_slots WHERE schedule_id = :s)"), {"s": schedule_id})
        db.execute(text("DELETE FROM appointment_slots WHERE schedule_id = :s"), {"s": schedule_id})
        db.execute(text("DELETE FROM provider_schedules WHERE id = :s"), {"s": schedule_id})
        db.execute(text("DELETE FROM providers WHERE id = :p"), {"p": provider_id})
        db.commit()


def _attempt(factory, slot_ids: list[uuid.UUID], replay_rate: float) -> str:
    slot_id = random.choice(slot_ids)
    referral_id = uuid.uuid4()
    # A share of attempts deliberately reuse a key already in flight, which is
    # what a retry after a lost response looks like from the provider's side.
    key = f"stress:{slot_id}" if random.random() < replay_rate else f"stress:{referral_id}:{slot_id}"
    request = BookingRequest(referral_id=referral_id, slot_id=slot_id, requested_specialty=SPECIALTY, idempotency_key=key)
    with factory() as db:
        try:
            result = book_slot(db, request)
            # A replay returns the original decision, so reporting only the
            # outcome would show more "booked" than there are appointments and
            # look like a violation when it is the guarantee working.
            return f"{result.outcome} (replayed)" if result.replayed else result.outcome
        except Exception as exc:  # surfaced rather than swallowed
            db.rollback()
            return f"error:{type(exc).__name__}"


def run_contention(rounds: int, racers: int) -> dict:
    """Maximum contention: every racer hits ONE slot at the same instant.

    Two deliberate choices make this the sharpest test of the row lock.

    A barrier releases every worker simultaneously, so the attempts genuinely
    overlap rather than arriving in a stream spread over time.

    Every racer carries a *distinct* idempotency key. With shared keys the
    idempotency layer alone would prevent duplicates and a broken lock would go
    unnoticed; distinct keys mean the row lock is the only thing standing
    between the racers and a double booking.
    """
    engine = create_engine(settings.provider_database_url, pool_size=racers + 4, max_overflow=8, pool_timeout=30)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.connect() as probe:
        ceiling = int(probe.execute(text("SHOW max_connections")).scalar())
    if racers + 12 > ceiling:
        raise SystemExit(f"  {racers} racers needs ~{racers + 12} connections but the server allows {ceiling}.")

    slot_ids, provider_id, schedule_id = _setup(factory, rounds)
    try:
        outcomes = Counter()
        started = time.perf_counter()
        for slot_id in slot_ids:
            gate = threading.Barrier(racers)

            def race(_):
                gate.wait()
                request = BookingRequest(
                    referral_id=uuid.uuid4(),
                    slot_id=slot_id,
                    requested_specialty=SPECIALTY,
                    # Distinct per racer: the lock is the only guard.
                    idempotency_key=f"race:{slot_id}:{uuid.uuid4()}",
                )
                with factory() as db:
                    try:
                        return book_slot(db, request).outcome
                    except Exception as exc:
                        db.rollback()
                        return f"error:{type(exc).__name__}"

            with ThreadPoolExecutor(max_workers=racers) as pool:
                outcomes.update(pool.map(race, range(racers)))
        elapsed = time.perf_counter() - started

        with factory() as db:
            per_slot = db.execute(
                select(Appointment.slot_id, func.count(Appointment.id))
                .where(Appointment.slot_id.in_(slot_ids))
                .group_by(Appointment.slot_id)
            ).all()
            over_booked = [(str(s), c) for s, c in per_slot if c > 1]

        return {
            "rounds": rounds,
            "racers_per_round": racers,
            "attempts": rounds * racers,
            "seconds": round(elapsed, 1),
            "outcomes": dict(outcomes),
            "slots_booked": sum(c for _, c in per_slot),
            "over_booked_slots": over_booked,
            "duplicate_bookings": sum(c - 1 for _, c in per_slot if c > 1),
        }
    finally:
        _teardown(factory, provider_id, schedule_id)


def run(attempts: int, workers: int, slots: int, replay_rate: float) -> dict:
    engine = create_engine(settings.provider_database_url, pool_size=workers + 4, max_overflow=8, pool_timeout=30)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # Asking for more connections than the server allows produces a flood of
    # OperationalErrors that look like booking failures and are not. Fail loudly
    # up front instead of polluting the result.
    with engine.connect() as probe:
        ceiling = int(probe.execute(text("SHOW max_connections")).scalar())
    requested = workers + 12
    if requested > ceiling:
        raise SystemExit(
            f"  {workers} workers needs ~{requested} connections but the server allows {ceiling}.\n"
            f"  Use --workers {ceiling - 12} or fewer."
        )

    slot_ids, provider_id, schedule_id = _setup(factory, slots)
    try:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = Counter(pool.map(lambda _: _attempt(factory, slot_ids, replay_rate), range(attempts)))
        elapsed = time.perf_counter() - started

        # The guarantee is a property of the database, not of what callers heard.
        with factory() as db:
            per_slot = db.execute(
                select(Appointment.slot_id, func.count(Appointment.id))
                .where(Appointment.slot_id.in_(slot_ids))
                .group_by(Appointment.slot_id)
            ).all()
            total_appointments = sum(count for _, count in per_slot)
            over_booked = [(str(slot), count) for slot, count in per_slot if count > 1]
            busy = db.scalar(select(func.count(AppointmentSlot.id)).where(AppointmentSlot.id.in_(slot_ids), AppointmentSlot.status == SlotStatus.BUSY))

        return {
            "attempts": attempts,
            "workers": workers,
            "slots": slots,
            "seconds": round(elapsed, 1),
            "attempts_per_second": round(attempts / elapsed, 1),
            "outcomes": dict(outcomes),
            "slots_booked": total_appointments,
            "slots_marked_busy": busy,
            "over_booked_slots": over_booked,
            "duplicate_bookings": sum(count - 1 for _, count in per_slot if count > 1),
        }
    finally:
        _teardown(factory, provider_id, schedule_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stress the exactly-once booking guarantee")
    parser.add_argument("--attempts", type=int, default=10_000)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--slots", type=int, default=100)
    parser.add_argument("--replay-rate", type=float, default=0.25)
    parser.add_argument("--contention", action="store_true", help="maximum contention: every racer hits one slot simultaneously")
    parser.add_argument("--rounds", type=int, default=500)
    parser.add_argument("--racers", type=int, default=80)
    args = parser.parse_args()

    if args.contention:
        report = run_contention(args.rounds, args.racers)
        print(f"  rounds          {report['rounds']:,} slots, {report['racers_per_round']} racers released simultaneously at each")
        print(f"  attempts        {report['attempts']:,} in {report['seconds']}s, every racer with a distinct idempotency key")
        print(f"  outcomes        {report['outcomes']}")
        print(f"  appointments    {report['slots_booked']} across {report['rounds']} contested slots")
        print(f"  DUPLICATES      {report['duplicate_bookings']}")
        if report["over_booked_slots"]:
            raise SystemExit(f"  GUARANTEE VIOLATED: {report['over_booked_slots']}")
        print("  guarantee held under maximum contention")
        return

    report = run(args.attempts, args.workers, args.slots, args.replay_rate)

    print(f"  attempts        {report['attempts']:,} across {report['workers']} workers over {report['slots']} slots")
    print(f"  throughput      {report['attempts_per_second']:,} attempts/s in {report['seconds']}s")
    print(f"  outcomes        {report['outcomes']}")
    fresh = report["outcomes"].get("booked", 0)
    replayed = sum(v for k, v in report["outcomes"].items() if "(replayed)" in k)
    print(f"  fresh bookings  {fresh} | replays returning the original decision: {replayed}")
    print(f"  appointments    {report['slots_booked']} rows across {report['slots_marked_busy']} slots marked busy")
    print(f"  DUPLICATES      {report['duplicate_bookings']}")
    if report["over_booked_slots"]:
        raise SystemExit(f"  GUARANTEE VIOLATED: {report['over_booked_slots']}")
    print("  guarantee held: no slot has more than one appointment")


if __name__ == "__main__":
    main()
