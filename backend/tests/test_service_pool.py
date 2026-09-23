"""A burst larger than the connection pool must queue, not deadlock.

Both services, because both serve sync endpoints from a pooled session.

FastAPI serializes a sync endpoint's response on the same thread pool the
endpoints run on, and closes a `yield` dependency only after that. So a request
that has finished its query still holds its connection while it waits for a
thread, and threads are all busy waiting for a connection. Nothing progresses
until pool timeouts fire.

That is what kept the provider service down for 26 seconds after a 60-second
freeze. This reproduces it in miniature: one connection, three threads, eight
concurrent requests. With connections released inside the endpoint every
request succeeds well inside the pool timeout; without, requests fail on it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date

import anyio.to_thread
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.routing import APIRoute

from app import main as referral_api, provider_service
from app.database import Base, get_db
from app.models import Patient, Referral
from app.provider_database import ProviderBase, get_provider_db
from app.sessions import ReleasesSessionRoute

POOL_TIMEOUT = 2.0
REQUESTS = 8


@pytest.fixture
def constrained_service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        connect_args={"check_same_thread": False},
        pool_size=1,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT,
    )
    ProviderBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def session():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    provider_service.app.dependency_overrides[get_provider_db] = session
    yield provider_service.app
    provider_service.app.dependency_overrides.clear()
    engine.dispose()


async def _burst(app, path: str = "/internal/catalog/providers") -> tuple[list[int], float]:
    # Fewer threads than requests, more threads than connections: the shape
    # that deadlocks when a finished request still holds its connection.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 3
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://provider") as client:
        started = time.perf_counter()
        responses = await asyncio.gather(*[
            client.get(path, params={"limit": 5}, headers={"X-CareRoute-Correlation-ID": f"burst-{i}"})
            for i in range(REQUESTS)
        ], return_exceptions=True)
        elapsed = time.perf_counter() - started
    anyio.to_thread.current_default_thread_limiter().total_tokens = 40
    return [r.status_code if isinstance(r, httpx.Response) else type(r).__name__ for r in responses], elapsed


def test_a_burst_larger_than_the_pool_queues_instead_of_deadlocking(constrained_service):
    statuses, elapsed = asyncio.run(_burst(constrained_service))

    assert statuses == [200] * REQUESTS, f"requests failed on pool timeouts: {statuses}"
    assert elapsed < POOL_TIMEOUT, f"the burst took {elapsed:.2f}s, which means something waited out a pool timeout"


@pytest.fixture
def constrained_api(tmp_path):
    """The referral API under the same constraint, on its own store."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'referral.db'}",
        connect_args={"check_same_thread": False},
        pool_size=1,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as seed:
        patient = Patient(external_id="burst", source="test", given_name="Burst", family_name="Case", birth_date=date(1980, 1, 1), is_synthetic=True)
        seed.add(patient)
        seed.flush()
        seed.add(Referral(patient_id=patient.id, requested_specialty="Cardiology", reason="Burst fixture", is_synthetic=True))
        seed.commit()

    def session():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    referral_api.app.dependency_overrides[get_db] = session
    yield referral_api.app
    referral_api.app.dependency_overrides.clear()
    engine.dispose()


def test_a_burst_on_the_referral_api_queues_instead_of_deadlocking(constrained_api):
    statuses, elapsed = asyncio.run(_burst(constrained_api, "/api/referrals"))

    assert statuses == [200] * REQUESTS, f"requests failed on pool timeouts: {statuses}"
    assert elapsed < POOL_TIMEOUT, f"the burst took {elapsed:.2f}s, which means something waited out a pool timeout"


@pytest.mark.parametrize(
    "app, module",
    [(provider_service.app, "app.provider_service"), (referral_api.app, "app.main")],
)
def test_every_sync_endpoint_releases_its_session_before_returning(app, module):
    """The protection is only as good as its coverage, so it is the route class."""
    routes = [
        route for route in app.routes
        if isinstance(route, APIRoute) and getattr(route.endpoint, "__module__", "") == module
    ]
    assert routes
    assert all(isinstance(route, ReleasesSessionRoute) for route in routes)
