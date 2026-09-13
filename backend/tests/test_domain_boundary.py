"""Structural proof that the referral and provider domains are actually separate.

These tests are about ownership rather than behaviour. Behavioural tests pass
just as happily when two domains quietly share a database; only a structural
check notices that the boundary has been re-crossed.
"""

import ast
import pathlib

from sqlalchemy import inspect

from app.database import Base
from app.provider_database import ProviderBase

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

# Modules that legitimately own provider data, plus the harnesses that build
# fixtures on both sides of the boundary. Harnesses are not the request path:
# they are allowed two stores precisely because they must set up both.
PROVIDER_SIDE = {"provider_models.py", "provider_database.py", "provider_queries.py", "provider_service.py", "provider_contracts.py", "provider_gateway.py", "provider_graph.py", "seed.py", "evaluation.py", "runtime_comparison.py", "fhir.py", "models.py"}
PROVIDER_TABLES = {"providers", "provider_schedules", "appointment_slots", "appointments", "booking_attempts"}


def test_the_two_metadatas_share_no_table():
    overlap = set(Base.metadata.tables) & set(ProviderBase.metadata.tables)
    assert not overlap, f"a table is declared in both domains: {overlap}"
    assert PROVIDER_TABLES == set(ProviderBase.metadata.tables)
    assert not PROVIDER_TABLES & set(Base.metadata.tables)


def test_no_referral_table_has_a_foreign_key_into_the_provider_domain():
    """A foreign key across databases cannot be enforced, so it must not exist."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            for key in column.foreign_keys:
                assert key.column.table.name not in PROVIDER_TABLES, (
                    f"{table.name}.{column.name} points at provider-owned {key.column.table.name}"
                )


def test_referral_side_modules_do_not_import_provider_models():
    """The request path must ask the provider domain, not read its tables."""
    offenders = []
    for path in APP.glob("*.py"):
        if path.name in PROVIDER_SIDE:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "provider_models" in node.module:
                offenders.append(path.name)
    assert not offenders, f"referral-side modules importing provider tables: {sorted(set(offenders))}"


def test_the_referral_api_reaches_the_provider_domain_only_through_the_gateway():
    import app.main as main

    source = pathlib.Path(main.__file__).read_text()
    for table_class in ("Provider", "ProviderSchedule", "AppointmentSlot", "Appointment"):
        assert f"select({table_class})" not in source, f"main.py still queries {table_class} directly"
    assert "provider_gateway_dependency" in source


def test_cross_boundary_references_survive_as_plain_columns():
    """The references still exist; only the unenforceable constraint is gone."""
    from app.models import Referral
    from app.provider_models import Appointment

    selected = inspect(Referral).columns["selected_slot_id"]
    assert not selected.foreign_keys
    assert selected.nullable

    referral_ref = inspect(Appointment).columns["referral_id"]
    assert not referral_ref.foreign_keys
    assert referral_ref.index is True
