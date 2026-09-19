"""Freeze the provider service under load and measure what the API does.

A stopped dependency is the easy case: the connection is refused at once and
every retry fails in microseconds. A *hung* dependency is the one that hurts.
`docker compose pause` freezes the process while the kernel keeps accepting TCP
connections, so each request connects and then waits for a response that never
comes - the full timeout, on every retry. That is what this drill produces.

It runs two kinds of traffic concurrently for the whole drill:

  provider-dependent  GET /api/providers, which calls the provider service
  unrelated           GET /api/referrals, which only touches the referral
                      database, to show whether the outage spreads

and three phases: healthy, frozen, recovered. Every request is recorded with
its start time, latency and status, so the result is a distribution per phase
and a timeline, not an average.

The breaker is a property of the API process, so comparing with and without it
means restarting the API with PROVIDER_BREAKER_ENABLED set accordingly; the
drill reports which configuration it observed rather than assuming.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import subprocess
import time
from dataclasses import dataclass

import httpx

PROVIDER_PATH = "/api/providers?limit=5"
UNRELATED_PATH = "/api/referrals?limit=5"
COMPOSE_DIR = pathlib.Path(__file__).resolve().parents[2]


@dataclass
class Sample:
    kind: str
    started: float
    latency: float
    status: int


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=COMPOSE_DIR, check=True, capture_output=True)


def _breaker_enabled() -> str:
    run = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-c", "from app.config import settings as s; print(s.provider_breaker_enabled)"],
        cwd=COMPOSE_DIR, capture_output=True, text=True,
    )
    return run.stdout.strip() or "unknown"


async def _client(http: httpx.AsyncClient, kind: str, path: str, origin: float, stop: asyncio.Event, samples: list[Sample]) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        try:
            response = await http.get(path)
            status = response.status_code
        except httpx.HTTPError:
            status = 0  # the client gave up; recorded, not hidden
        samples.append(Sample(kind, started - origin, time.perf_counter() - started, status))


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _phase(samples: list[Sample], kind: str, start: float, end: float) -> dict:
    chosen = [s for s in samples if s.kind == kind and start <= s.started < end]
    latencies = [s.latency for s in chosen]
    ok = sum(1 for s in chosen if s.status == 200)
    return {
        "requests": len(chosen),
        "per_second": round(len(chosen) / (end - start), 1) if end > start else 0,
        "success_rate": round(ok / len(chosen), 4) if chosen else None,
        "p50_ms": round(_percentile(latencies, 0.50) * 1000, 1),
        "p95_ms": round(_percentile(latencies, 0.95) * 1000, 1),
        "p99_ms": round(_percentile(latencies, 0.99) * 1000, 1),
        "max_ms": round(max(latencies) * 1000, 1) if latencies else None,
    }


async def drill(base_url: str, provider_clients: int, unrelated_clients: int, healthy: float, outage: float, recovery: float, service: str) -> dict:
    samples: list[Sample] = []
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=provider_clients + unrelated_clients + 4)
    marks: dict[str, float] = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0, limits=limits) as http:
        origin = time.perf_counter()
        tasks = [asyncio.create_task(_client(http, "provider", PROVIDER_PATH, origin, stop, samples)) for _ in range(provider_clients)]
        tasks += [asyncio.create_task(_client(http, "unrelated", UNRELATED_PATH, origin, stop, samples)) for _ in range(unrelated_clients)]
        try:
            await asyncio.sleep(healthy)
            # Marked after the command returns, not before: `docker compose
            # pause` takes a moment, and requests issued meanwhile still succeed.
            await asyncio.to_thread(_compose, "pause", service)
            marks["paused"] = time.perf_counter() - origin
            await asyncio.sleep(outage)
        finally:
            # Always thaw, even if the drill is interrupted: a frozen dependency
            # left behind would break whatever runs next.
            await asyncio.to_thread(_compose, "unpause", service)
            marks["unpaused"] = time.perf_counter() - origin
        await asyncio.sleep(recovery)
        marks["end"] = time.perf_counter() - origin
        stop.set()
        # In-flight requests finish on their own; a request frozen at the moment
        # of pause can take a full retry budget to return.
        await asyncio.gather(*tasks)

    paused, unpaused, end = marks["paused"], marks["unpaused"], marks["end"]
    provider_during = sorted((s for s in samples if s.kind == "provider" and paused <= s.started < unpaused), key=lambda s: s.started)
    provider_after = sorted((s for s in samples if s.kind == "provider" and s.started >= unpaused), key=lambda s: s.started)

    # "Fast" is well under one attempt's timeout: the breaker answered, not the network.
    fast_failure = next((s for s in provider_during if s.status != 200 and s.latency < 0.5), None)
    recovered = next((s for s in provider_after if s.status == 200), None)

    timeline = []
    bucket = 5.0
    t = 0.0
    while t < end:
        window = [s for s in samples if s.kind == "provider" and t <= s.started < t + bucket]
        timeline.append({
            "from_s": round(t, 1),
            "requests": len(window),
            "ok": sum(1 for s in window if s.status == 200),
            "median_ms": round(statistics.median([s.latency for s in window]) * 1000, 1) if window else None,
        })
        t += bucket

    return {
        "breaker_enabled": _breaker_enabled(),
        "clients": {"provider": provider_clients, "unrelated": unrelated_clients},
        "marks_s": {k: round(v, 1) for k, v in marks.items()},
        "phases": {
            name: {kind: _phase(samples, kind, start, stop_at) for kind in ("provider", "unrelated")}
            for name, start, stop_at in (("healthy", 0.0, paused), ("frozen", paused, unpaused), ("recovered", unpaused, end))
        },
        "frozen_calls_paying_full_timeout": sum(1 for s in provider_during if s.latency >= 5.0),
        "frozen_calls_total": len(provider_during),
        "seconds_to_first_fast_failure": round(fast_failure.started - paused, 2) if fast_failure else None,
        "seconds_to_first_success_after_thaw": round(recovered.started - unpaused, 2) if recovered else None,
        "timeline": timeline,
    }


def _print(report: dict) -> None:
    print(f"  breaker enabled: {report['breaker_enabled']}    clients: {report['clients']}    marks: {report['marks_s']}")
    print()
    print(f"  {'phase':<11}{'traffic':<11}{'req':>7}{'req/s':>8}{'ok':>9}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}")
    for phase, kinds in report["phases"].items():
        for kind, row in kinds.items():
            rate = f"{row['success_rate']:.1%}" if row["success_rate"] is not None else "-"
            print(f"  {phase:<11}{kind:<11}{row['requests']:>7}{row['per_second']:>8}{rate:>9}{row['p50_ms']:>10}{row['p95_ms']:>10}{row['p99_ms']:>10}{str(row['max_ms']):>10}")
    print()
    print(f"  frozen provider calls that paid the full timeout   {report['frozen_calls_paying_full_timeout']} of {report['frozen_calls_total']}")
    print(f"  seconds from freeze to first fast failure          {report['seconds_to_first_fast_failure']}")
    print(f"  seconds from thaw to first success                 {report['seconds_to_first_success_after_thaw']}")
    print()
    print("  provider timeline (5 s buckets by request start):")
    for row in report["timeline"]:
        print(f"    {row['from_s']:>6}s  requests {row['requests']:>5}  ok {row['ok']:>5}  median {row['median_ms']} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze the provider service under load and measure the API")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--provider-clients", type=int, default=20)
    parser.add_argument("--unrelated-clients", type=int, default=5)
    parser.add_argument("--healthy", type=float, default=15)
    parser.add_argument("--outage", type=float, default=60)
    parser.add_argument("--recovery", type=float, default=45)
    parser.add_argument("--service", default="provider-service")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(drill(args.base_url, args.provider_clients, args.unrelated_clients, args.healthy, args.outage, args.recovery, args.service))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)


if __name__ == "__main__":
    main()
