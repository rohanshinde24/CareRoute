"""Keyset pagination semantics.

The properties worth testing are the ones offset pagination gets wrong: a page
boundary must not skip or repeat a row when new rows arrive mid-traversal, and
no caller may retrieve the whole table in one request.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Patient, Referral
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, clamp_limit, decode_cursor, encode_cursor


def _patient(db):
    person = Patient(external_id=f"page-{uuid.uuid4().hex[:8]}", source="test", given_name="Page", family_name="Fixture", birth_date=datetime(1990, 1, 1).date(), is_synthetic=True)
    db.add(person)
    db.flush()
    return person


def _referrals(db, count, base=None):
    person = _patient(db)
    base = base or datetime.now(timezone.utc)
    created = []
    for index in range(count):
        referral = Referral(
            patient_id=person.id,
            requested_specialty="Cardiology",
            reason=f"Synthetic referral {index}",
            is_synthetic=True,
            created_at=base + timedelta(seconds=index),
        )
        db.add(referral)
        created.append(referral)
    db.commit()
    return created


def test_cursor_roundtrips_and_rejects_tampering():
    moment = datetime.now(timezone.utc)
    row_id = uuid.uuid4()
    assert decode_cursor(encode_cursor(moment, row_id)) == (moment, row_id)
    for bad in ["", "!!!!", "bm90LWEtY3Vyc29y", encode_cursor(moment, row_id)[:-6]]:
        with pytest.raises(InvalidCursor):
            decode_cursor(bad)


def test_limit_is_clamped_so_no_caller_can_request_the_whole_table():
    assert clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit(10_000) == MAX_LIMIT
    assert clamp_limit(0) == 1
    assert clamp_limit(-5) == 1


def test_a_malformed_cursor_is_a_client_error_not_a_crash(client, db):
    assert client.get("/api/referrals?cursor=not-a-real-cursor").status_code == 400


def test_traversal_returns_every_row_exactly_once(client, db):
    _referrals(db, 25)
    seen, cursor, pages = [], None, 0
    while True:
        query = f"/api/referrals?limit=10{f'&cursor={cursor}' if cursor else ''}"
        page = client.get(query).json()
        seen.extend(item["id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert pages < 10, "pagination did not terminate"

    assert len(seen) == 25
    assert len(set(seen)) == 25, "a row was returned on more than one page"


def test_default_and_maximum_page_sizes_are_enforced(client, db):
    _referrals(db, 60)
    assert len(client.get("/api/referrals").json()["items"]) == DEFAULT_LIMIT
    assert client.get("/api/referrals?limit=500").status_code == 422
    assert len(client.get("/api/referrals?limit=200").json()["items"]) == 60


def test_inserts_during_traversal_do_not_shift_the_page_boundary(client, db):
    """The property offset pagination cannot provide.

    Referrals are newest-first, so a row inserted mid-traversal lands at the head.
    With OFFSET the second page would slide by one and repeat a row already seen.
    """
    _referrals(db, 20)
    first = client.get("/api/referrals?limit=10").json()
    first_ids = [item["id"] for item in first["items"]]

    _referrals(db, 5, base=datetime.now(timezone.utc) + timedelta(days=1))

    second = client.get(f"/api/referrals?limit=10&cursor={first['next_cursor']}").json()
    second_ids = [item["id"] for item in second["items"]]

    assert not set(first_ids) & set(second_ids), "a row appeared on two pages after concurrent inserts"


def test_pagination_never_returns_evaluation_fixtures(client, db):
    person = _patient(db)
    hidden = Referral(patient_id=person.id, requested_specialty="Evaluation-only", reason="Hidden fixture", is_synthetic=True, is_evaluation=True)
    db.add(hidden)
    _referrals(db, 15)

    seen, cursor = [], None
    while True:
        page = client.get(f"/api/referrals?limit=5{f'&cursor={cursor}' if cursor else ''}").json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert str(hidden.id) not in seen
