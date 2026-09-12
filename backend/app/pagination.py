"""Keyset (cursor) pagination.

Offset pagination is wrong for these endpoints for two reasons. Referrals are
ordered newest-first and new rows insert at the head, so any offset taken while
rows arrive skips or repeats records. And OFFSET n makes PostgreSQL read and
discard n rows, so deep pages get linearly slower as the table grows - the exact
failure mode that appears with data volume rather than with concurrency.

A keyset cursor encodes the last row's sort position, so the next page is an
index seek of constant cost, and concurrent inserts cannot shift it.

created_at and start_at are not unique, so the primary key is carried as a
tiebreaker and the ordering is over the pair. Without it a page boundary that
falls between two rows sharing a timestamp would drop or duplicate one.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

ItemT = TypeVar("ItemT")


class CursorPage(BaseModel, Generic[ItemT]):
    items: list[ItemT]
    next_cursor: str | None = None


class InvalidCursor(ValueError):
    """Raised for a cursor that is malformed, truncated, or not ours."""


def encode_cursor(sort_value: datetime, row_id: uuid.UUID) -> str:
    raw = f"{sort_value.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        sort_value, _, row_id = raw.partition("|")
        if not sort_value or not row_id:
            raise InvalidCursor("Cursor is missing a component")
        return datetime.fromisoformat(sort_value), uuid.UUID(row_id)
    except InvalidCursor:
        raise
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor("Cursor is not a valid pagination token") from exc


def clamp_limit(limit: int | None) -> int:
    """Bound the page size so a caller cannot ask for the whole table."""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))
