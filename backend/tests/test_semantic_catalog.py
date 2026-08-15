from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from app.services.query import build_semantic_catalog


def _catalog(**overrides):
    values = {
        "database_identity": r"C:\private\bridgeguard.db#inode-123",
        "workspace_id": "shared",
        "schema_generation": 5,
        "data_generation": 17,
    }
    values.update(overrides)
    return build_semantic_catalog(**values)


def test_catalog_hash_is_stable_and_keyed_by_database_schema_and_data_identity():
    first = _catalog()
    second = _catalog()

    assert first.catalog_hash == second.catalog_hash
    assert first.cache_key == second.cache_key
    assert _catalog(database_identity="other-database").catalog_hash != first.catalog_hash
    assert _catalog(workspace_id="other-workspace").catalog_hash != first.catalog_hash
    assert _catalog(schema_generation=6).catalog_hash != first.catalog_hash
    assert _catalog(data_generation=18).catalog_hash != first.catalog_hash


def test_prompt_is_static_logical_allowlist_and_never_leaks_physical_discovery():
    catalog = _catalog()
    prompt = catalog.prompt_text()
    payload = json.loads(prompt)

    assert r"C:\private\bridgeguard.db" not in prompt
    assert "database_identity" not in prompt
    assert "physical_column" not in prompt
    assert "physical_relations" not in prompt
    assert "sqlite_master" not in prompt.lower()
    assert "pragma" not in prompt.lower()
    assert payload["catalog_hash"] == catalog.catalog_hash
    assert {resource["resource_id"] for resource in payload["resources"]} >= {
        "v_published_bridge_snapshots",
        "v_current_bridge_snapshots",
        "v_bridge_features",
        "v_carried_highway_features",
        "v_bridge_condition",
        "v_county_bridge_inventory",
    }


def test_catalog_exposes_only_reviewed_columns_functions_and_exact_joins():
    catalog = _catalog()

    assert catalog.is_allowed_column("v_published_bridge_snapshots", "bridge_number")
    assert not catalog.is_allowed_column("v_published_bridge_snapshots", "password_hash")
    assert not catalog.is_allowed_column("sqlite_master", "sql")
    assert catalog.physical_table_allowlist == frozenset(
        resource.resource_id for resource in catalog.resources
    )
    assert "bridges" not in catalog.physical_table_allowlist
    assert "bridges" in catalog.implementation_relation_inventory
    assert catalog.is_allowed_query_column(
        "v_published_bridge_snapshots", "bridge_number"
    )
    assert not catalog.is_allowed_query_column(
        "v_published_bridge_snapshots", "source_payload_json"
    )
    assert catalog.is_allowed_function("count", 1)
    assert catalog.is_allowed_function("COALESCE", 3)
    assert not catalog.is_allowed_function("load_extension", 1)
    assert not catalog.is_allowed_function("random", 0)

    key = ("workspace_id", "state_code", "bridge_number", "data_year")
    assert catalog.is_allowed_join(
        "v_published_bridge_snapshots",
        "v_bridge_features",
        key,
        key,
    )
    assert catalog.is_allowed_join(
        "v_bridge_features",
        "v_published_bridge_snapshots",
        key,
        key,
    )
    assert not catalog.is_allowed_join(
        "v_published_bridge_snapshots",
        "v_bridge_features",
        ("bridge_number",),
        ("bridge_number",),
    )


def test_catalog_contract_is_frozen_and_uses_tuple_members():
    catalog = _catalog()
    resource = catalog.resources_by_id["v_bridge_condition"]

    assert isinstance(catalog.resources, tuple)
    assert isinstance(resource.columns, tuple)
    assert isinstance(resource.scope_fields, tuple)
    with pytest.raises(FrozenInstanceError):
        resource.description = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        catalog.resources_by_id["unsafe"] = resource  # type: ignore[index]


def test_time_and_condition_semantics_are_not_global_latest_or_ambiguous():
    catalog = _catalog()
    current = catalog.resources_by_id["v_current_bridge_snapshots"]
    condition = catalog.resources_by_id["v_bridge_condition"]
    carried = catalog.resources_by_id["v_carried_highway_features"]

    assert "per bridge" in current.time_semantics
    assert "database-wide maximum" in current.description
    assert condition.implementation_id == "controlling_condition_v1"
    assert {
        "bc01",
        "bc02",
        "bc03",
        "bc04",
        "bc13",
        "controlling_rating",
        "condition_class",
        "condition_basis",
        "condition_conflict",
    }.issubset(condition.column_ids)
    assert "H01" in carried.description


def test_catalog_rejects_invalid_generation_identity():
    with pytest.raises(ValueError):
        _catalog(database_identity="")
    with pytest.raises(ValueError):
        _catalog(data_generation=-1)
