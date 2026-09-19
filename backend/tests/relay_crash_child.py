"""A relay process that kills itself with SIGKILL at a chosen point.

Run as a separate process by tests/test_relay_crash.py. SIGKILL, not an
exception: an exception unwinds, runs `finally` blocks and lets SQLAlchemy roll
back politely. A killed process does none of that - the database learns about it
only when the socket drops - and that is the failure the relay's ordering has to
survive.

Two crash points:

  before_publish  killed inside XADD, before the command is sent
  after_publish   killed after Redis acknowledged XADD, before the row is marked

The relay's `drain` runs unmodified. Only row selection is narrowed to the one
fixture row, because a shared database can hold thousands of undispatched rows
from other runs, and relaying those into the test would change what it measures.
"""

import os
import signal
import sys
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import relay
from app.provider_models import ProviderOutbox


def main() -> None:
    provider_url, redis_url, stream, row_id, crash_at = sys.argv[1:6]
    target = uuid.UUID(row_id)
    relay.STREAM = stream

    def only_the_fixture_row(db, model, limit):
        return list(db.scalars(select(model).where(model.id == target).with_for_update(skip_locked=True)))

    relay.pending = only_the_fixture_row

    client = relay.redis_client(redis_url)
    publish = client.xadd

    def xadd(name, fields, *args, **kwargs):
        if crash_at == "before_publish" and fields.get("event_id") == str(target):
            os.kill(os.getpid(), signal.SIGKILL)
        entry = publish(name, fields, *args, **kwargs)
        if crash_at == "after_publish" and fields.get("event_id") == str(target):
            os.kill(os.getpid(), signal.SIGKILL)
        return entry

    if crash_at != "none":
        client.xadd = xadd

    factory = sessionmaker(bind=create_engine(provider_url), expire_on_commit=False)
    with factory() as db:
        delivered = relay.drain(db, ProviderOutbox, client)
    print(delivered)


if __name__ == "__main__":
    main()
