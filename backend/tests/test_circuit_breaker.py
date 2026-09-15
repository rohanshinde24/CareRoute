"""Circuit breaker behaviour.

Retries stop one caller hammering a struggling dependency. The breaker stops
every caller doing it at once, and stops each request paying its full retry
budget before failing on something already known to be down.
"""

import asyncio
import uuid

import httpx
import pytest

from app.circuit_breaker import CircuitBreaker, CircuitOpen, CircuitState, breaker_for, reset_all
from app.config import Settings
from app.provider_gateway import (
    HttpProviderGateway,
    ProviderGatewayProtocolError,
    ProviderGatewayTransientError,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_all()
    yield
    reset_all()


def _breaker(threshold=3, reset_seconds=10.0):
    clock = [0.0]
    breaker = CircuitBreaker("test", failure_threshold=threshold, reset_seconds=reset_seconds, clock=lambda: clock[0])
    return breaker, clock


# --- state machine ----------------------------------------------------------

def test_it_opens_only_after_the_threshold_is_reached():
    breaker, _ = _breaker(threshold=3)

    for _ in range(2):
        breaker.before_call()
        breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED, "two failures must not trip a threshold of three"

    breaker.before_call()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


def test_a_success_resets_the_failure_count():
    """Failures must be consecutive; intermittent errors are not an outage."""
    breaker, _ = _breaker(threshold=3)

    breaker.before_call(); breaker.record_failure()
    breaker.before_call(); breaker.record_failure()
    breaker.before_call(); breaker.record_success()
    breaker.before_call(); breaker.record_failure()

    assert breaker.state is CircuitState.CLOSED


def test_an_open_circuit_rejects_without_attempting_the_call():
    breaker, _ = _breaker(threshold=1)
    breaker.before_call(); breaker.record_failure()

    with pytest.raises(CircuitOpen):
        breaker.before_call()


def test_only_one_request_probes_recovery():
    """A crowd waiting on a recovering dependency must not all be released."""
    breaker, clock = _breaker(threshold=1, reset_seconds=10.0)
    breaker.before_call(); breaker.record_failure()
    clock[0] = 11.0

    breaker.before_call()  # the probe
    with pytest.raises(CircuitOpen):
        breaker.before_call()  # everyone else still waits


def test_a_failed_probe_reopens_immediately():
    breaker, clock = _breaker(threshold=3, reset_seconds=10.0)
    for _ in range(3):
        breaker.before_call(); breaker.record_failure()
    clock[0] = 11.0
    assert breaker.state is CircuitState.HALF_OPEN

    breaker.before_call()
    breaker.record_failure()

    assert breaker.state is CircuitState.OPEN, "one failed probe is enough; the dependency is still down"


def test_a_successful_probe_closes_the_circuit():
    breaker, clock = _breaker(threshold=1, reset_seconds=10.0)
    breaker.before_call(); breaker.record_failure()
    clock[0] = 11.0

    breaker.before_call()
    breaker.record_success()

    assert breaker.state is CircuitState.CLOSED


# --- registry ---------------------------------------------------------------

def test_breakers_are_shared_per_process_not_per_instance():
    """A gateway is constructed per request.

    If the breaker lived on the gateway its state would reset constantly and it
    would never trip, which is the whole reason the registry exists.
    """
    assert breaker_for("provider-service") is breaker_for("provider-service")
    assert breaker_for("provider-service") is not breaker_for("something-else")


# --- integration with the gateway -------------------------------------------

def _settings(**overrides):
    base = dict(
        provider_service_url="http://provider",
        provider_retry_attempts=1,
        provider_retry_backoff_seconds=0,
        provider_breaker_enabled=True,
        provider_breaker_failure_threshold=2,
        provider_breaker_reset_seconds=60.0,
    )
    base.update(overrides)
    return Settings(**base)


def _gateway(handler, **overrides):
    return HttpProviderGateway(_settings(**overrides), transport=httpx.MockTransport(handler))


def test_the_gateway_trips_after_repeated_transient_failures():
    calls = []

    def unavailable(request):
        calls.append(request.url.path)
        return httpx.Response(503)

    for _ in range(2):
        with pytest.raises(ProviderGatewayTransientError):
            asyncio.run(_gateway(unavailable).list_specialties(uuid.uuid4()))

    attempted = len(calls)

    # A new gateway instance, as a real request would construct.
    with pytest.raises(ProviderGatewayTransientError, match="circuit is open"):
        asyncio.run(_gateway(unavailable).list_specialties(uuid.uuid4()))

    assert len(calls) == attempted, "an open circuit must not reach the network at all"


def test_a_protocol_error_never_trips_the_circuit():
    """A 4xx means this system's request was wrong.

    Tripping on that would turn a bug on our side into an apparent outage on
    theirs, and would take the dependency out of service for everyone else.
    """
    def bad_request(request):
        return httpx.Response(400)

    for _ in range(5):
        with pytest.raises(ProviderGatewayProtocolError):
            asyncio.run(_gateway(bad_request).list_specialties(uuid.uuid4()))

    assert breaker_for("provider-service").state is CircuitState.CLOSED


def test_the_breaker_can_be_disabled():
    calls = []

    def unavailable(request):
        calls.append(1)
        return httpx.Response(503)

    for _ in range(4):
        with pytest.raises(ProviderGatewayTransientError):
            asyncio.run(_gateway(unavailable, provider_breaker_enabled=False).list_specialties(uuid.uuid4()))

    assert len(calls) == 4, "with the breaker off every call should still reach the network"


def test_reconfiguring_a_breaker_takes_effect_rather_than_being_ignored():
    """The registry keys by name, so a second call carries different settings.

    Discarding them would mean a configuration change silently did nothing, and
    the breaker would fail to trip when it mattered. This was a real bug: the
    gateway test above appeared to prove the breaker never tripped, when in fact
    it had inherited a threshold of five from an earlier caller.
    """
    first = breaker_for("reconfig-target", failure_threshold=5, reset_seconds=30.0)
    second = breaker_for("reconfig-target", failure_threshold=2, reset_seconds=5.0)

    assert first is second
    assert second.failure_threshold == 2
    assert second.reset_seconds == 5.0
