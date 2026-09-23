"""Return a request's database connection before its response is serialized.

FastAPI closes a `yield` dependency only after the response has been
serialized, and for a *sync* endpoint that serialization is a second hop onto
the same thread pool the endpoints themselves run on. Under a burst larger than
the connection pool that deadlocks: every thread blocks waiting for a
connection, while the requests already holding connections sit finished, queued
for a thread to serialize on. Nothing moves until pool timeouts break it.

It is not hypothetical. After a 60-second freeze of the provider service, the
400 requests its callers had abandoned arrived at once on thaw and kept the
service unavailable for 26 seconds on this alone, with 167 pool timeouts.

Closing the session in the endpoint's own thread means a request never holds a
connection while waiting for a thread, so a burst queues instead. Applied as a
route class rather than a decorator so a new endpoint cannot forget it.

The requirement it places on endpoints: anything the response model needs must
be loaded before the endpoint returns. Eager-load relationships rather than
leaving them to lazy-load during serialization.
"""

from __future__ import annotations

import functools
import inspect

from fastapi.routing import APIRoute
from sqlalchemy.orm import Session


class ReleasesSessionRoute(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        # Async endpoints serialize on the event loop with no thread hop, so
        # they never hold a connection waiting for a thread.
        if not inspect.iscoroutinefunction(endpoint):
            endpoint = releasing_sessions(endpoint)
        super().__init__(path, endpoint, **kwargs)


def releasing_sessions(endpoint):
    @functools.wraps(endpoint)
    def call(*args, **kwargs):
        try:
            return endpoint(*args, **kwargs)
        finally:
            for value in kwargs.values():
                if isinstance(value, Session):
                    value.close()

    return call
