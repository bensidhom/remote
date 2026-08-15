from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.conversion.pinned_schema import (
    JSONSCHEMA_AVAILABLE,
    SchemaExecutionLimits,
    SchemaExecutionStatus,
    ValidationTarget,
    execute_pinned_schema,
)


DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


def _registry_payload(artifact: dict, *, artifact_ids=None) -> dict:
    return {
        "format_version": 1,
        "registry_id": "pinned_schema_test_registry",
        "observed_on": "2026-08-13",
        "artifacts": [artifact],
        "bundles": [
            {
                "id": "test_bundle",
                "title": "Pinned schema test bundle",
                "profile_kind": "research_import",
                "legal_status": "internal_non_submission",
                "compliance_use": "research_only_not_for_submission",
                "implementation_status": "registry_only",
                "enabled": True,
                "effective_submission_years": [],
                "artifact_ids": artifact_ids if artifact_ids is not None else [artifact["id"]],
                "inherits": [],
                "disabled_reason": None,
                "notes": [],
            }
        ],
    }


def _artifact(**overrides) -> dict:
    artifact = {
        "id": "test_schema",
        "title": "Test JSON Schema",
        "role": "submittal_schema",
        "authority": "FHWA",
        "legal_status": "operational",
        "state": "vendored_verified",
        "source_url": "https://www.fhwa.dot.gov/example/schema.json",
        "published_on": None,
        "published_label": None,
        "updated_on": None,
        "media_type": "application/schema+json",
        "local_path": "schema.json",
        "sha256": None,
        "notes": [],
    }
    artifact.update(overrides)
    return artifact


def _write_registry(
    tmp_path: Path,
    schema,
    *,
    raw_schema_bytes: bytes | None = None,
    artifact_overrides: dict | None = None,
    digest_override: str | None = None,
    artifact_ids=None,
) -> Path:
    schema_bytes = raw_schema_bytes
    if schema_bytes is None:
        schema_bytes = json.dumps(schema, separators=(",", ":")).encode("utf-8")
    (tmp_path / "schema.json").write_bytes(schema_bytes)
    artifact = _artifact(**(artifact_overrides or {}))
    if artifact["state"] == "vendored_verified":
        artifact["sha256"] = digest_override or hashlib.sha256(schema_bytes).hexdigest()
    payload = _registry_payload(artifact, artifact_ids=artifact_ids)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    return registry_path


def _object_schema(**updates) -> dict:
    schema = {
        "$schema": DRAFT_2020_12,
        "type": "object",
        "properties": {"BL01": {"type": "string"}},
        "required": ["BL01"],
        "additionalProperties": False,
    }
    schema.update(updates)
    return schema


def test_unverified_artifact_fails_closed(test_workspace: Path):
    schema = _object_schema()
    registry_path = _write_registry(
        test_workspace,
        schema,
        artifact_overrides={
            "state": "remote_unverified",
            "local_path": None,
            "sha256": None,
        },
    )

    result = execute_pinned_schema(
        {"BL01": "34"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.UNAVAILABLE
    assert not result.valid
    assert not result.schema_executed
    assert "vendored_verified" in result.detail


def test_registry_hash_mismatch_fails_closed(test_workspace: Path):
    registry_path = _write_registry(
        test_workspace,
        _object_schema(),
        digest_override="0" * 64,
    )

    result = execute_pinned_schema(
        {"BL01": "34"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.UNAVAILABLE
    assert "SHA-256 mismatch" in result.detail


def test_schema_must_belong_to_selected_bundle(test_workspace: Path):
    # The registry itself rejects the dangling bundle reference, so use another
    # registered artifact name as the requested, non-member identifier.
    registry_path = _write_registry(test_workspace, _object_schema())

    result = execute_pinned_schema(
        {"BL01": "34"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="not_the_schema",
    )

    assert result.status is SchemaExecutionStatus.UNAVAILABLE
    assert "not part" in result.detail or "unknown FHWA artifact" in result.detail


def test_non_json_media_type_is_not_executed(test_workspace: Path):
    registry_path = _write_registry(
        test_workspace,
        _object_schema(),
        artifact_overrides={"media_type": "text/html"},
    )

    result = execute_pinned_schema(
        {"BL01": "34"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.UNAVAILABLE
    assert "media type" in result.detail


def test_fhwa_2024_07_dialect_is_explicitly_unsupported(test_workspace: Path):
    registry_path = _write_registry(
        test_workspace,
        _object_schema(**{"$schema": "https://json-schema.org/draft/2024-07/schema"}),
    )

    result = execute_pinned_schema(
        {"BL01": "34"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.UNSUPPORTED_DIALECT
    assert result.dialect == "https://json-schema.org/draft/2024-07/schema"
    assert result.issues[0].schema_pointer == "/$schema"
    assert not result.schema_executed


def test_missing_dialect_is_not_defaulted(test_workspace: Path):
    registry_path = _write_registry(test_workspace, {"type": "object"})

    result = execute_pinned_schema(
        {},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.UNSUPPORTED_DIALECT
    assert result.dialect is None


def test_duplicate_schema_keys_are_rejected(test_workspace: Path):
    raw = (
        b'{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        b'"type":"object","type":"array"}'
    )
    registry_path = _write_registry(test_workspace, None, raw_schema_bytes=raw)

    result = execute_pinned_schema(
        {},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.INVALID_SCHEMA
    assert result.issues[0].keyword == "schema_json"
    assert "duplicate" in result.issues[0].message


def test_external_schema_references_are_forbidden(test_workspace: Path):
    registry_path = _write_registry(
        test_workspace,
        {
            "$schema": DRAFT_2020_12,
            "$ref": "https://example.com/not-pinned.json",
        },
    )

    result = execute_pinned_schema(
        {},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.INVALID_SCHEMA
    assert result.issues[0].schema_pointer == "/$ref"
    assert result.issues[0].keyword == "$ref"


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema is not installed in the slim test venv")
def test_valid_object_executes_declared_dialect(test_workspace: Path):
    registry_path = _write_registry(test_workspace, _object_schema())

    result = execute_pinned_schema(
        {"BL01": "34"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
        target=ValidationTarget.BRIDGE,
    )

    assert result.status is SchemaExecutionStatus.VALID
    assert result.valid
    assert result.schema_executed
    assert result.issues == ()
    assert result.to_dict()["reported_issue_count"] == 0


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema is not installed in the slim test venv")
def test_required_type_and_additional_properties_are_structured_and_sorted(test_workspace: Path):
    schema = _object_schema(
        properties={
            "BL01": {"type": "string"},
            "BID01": {"type": "integer"},
            "A/B~": {"type": "string"},
        },
        required=["BL01", "BID01"],
    )
    registry_path = _write_registry(test_workspace, schema)

    result = execute_pinned_schema(
        {"BID01": "wrong", "A/B~": 7, "EXTRA": True},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.INVALID
    assert {issue.keyword for issue in result.issues} == {
        "required",
        "type",
        "additionalProperties",
    }
    assert any(issue.instance_pointer == "/A~1B~0" for issue in result.issues)
    assert any(issue.schema_pointer == "/properties/A~1B~0/type" for issue in result.issues)
    sort_keys = [
        (issue.instance_pointer, issue.schema_pointer, issue.keyword, issue.message)
        for issue in result.issues
    ]
    assert sort_keys == sorted(sort_keys)


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema is not installed in the slim test venv")
def test_object_schema_validates_each_batch_item_with_prefixed_pointer(test_workspace: Path):
    registry_path = _write_registry(test_workspace, _object_schema())

    result = execute_pinned_schema(
        [{"BL01": "34"}, {"BL01": 34}],
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
        target="batch",
    )

    assert result.status is SchemaExecutionStatus.INVALID
    assert result.validation_target is ValidationTarget.BATCH
    assert result.issues[0].instance_pointer == "/1/BL01"


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema is not installed in the slim test venv")
def test_array_schema_validates_one_bridge_without_synthetic_index_pointer(test_workspace: Path):
    schema = {
        "$schema": DRAFT_2020_12,
        "type": "array",
        "items": _object_schema(),
    }
    registry_path = _write_registry(test_workspace, schema)

    result = execute_pinned_schema(
        {"BL01": 34},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
        target="bridge",
    )

    assert result.status is SchemaExecutionStatus.INVALID
    assert result.issues[0].instance_pointer == "/BL01"


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema is not installed in the slim test venv")
def test_invalid_schema_fails_meta_schema_self_check(test_workspace: Path):
    registry_path = _write_registry(
        test_workspace,
        {"$schema": DRAFT_2020_12, "type": 4},
    )

    result = execute_pinned_schema(
        {},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
    )

    assert result.status is SchemaExecutionStatus.INVALID_SCHEMA
    assert result.issues
    assert "meta-schema" in result.detail


@pytest.mark.skipif(not JSONSCHEMA_AVAILABLE, reason="jsonschema is not installed in the slim test venv")
def test_issue_limit_is_enforced(test_workspace: Path):
    schema = _object_schema(
        properties={
            "BL01": {"type": "string"},
            "BID01": {"type": "string"},
            "BID02": {"type": "string"},
            "BID03": {"type": "string"},
        },
        required=[],
    )
    registry_path = _write_registry(test_workspace, schema)

    result = execute_pinned_schema(
        {"BL01": 1, "BID01": 2, "BID02": 3, "BID03": 4},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
        limits=SchemaExecutionLimits(max_issues=2),
    )

    assert result.status is SchemaExecutionStatus.INVALID
    assert len(result.issues) == 2
    assert result.issues_truncated


def test_instance_resource_limit_fails_before_execution(test_workspace: Path):
    registry_path = _write_registry(test_workspace, _object_schema())

    result = execute_pinned_schema(
        {"BL01": "34", "BID01": "1"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
        limits=SchemaExecutionLimits(max_instance_nodes=2),
    )

    assert result.status is SchemaExecutionStatus.RESOURCE_LIMIT_EXCEEDED
    assert result.issues[0].keyword == "max_instance_nodes_exceeded"


def test_schema_byte_limit_is_enforced(test_workspace: Path):
    registry_path = _write_registry(test_workspace, _object_schema())

    result = execute_pinned_schema(
        {"BL01": "34"},
        registry_path=registry_path,
        bundle_id="test_bundle",
        artifact_id="test_schema",
        limits=SchemaExecutionLimits(max_schema_bytes=16),
    )

    assert result.status is SchemaExecutionStatus.RESOURCE_LIMIT_EXCEEDED
    assert result.issues[0].keyword == "max_schema_bytes_exceeded"


def test_limits_and_target_are_validated_as_programmer_configuration(test_workspace: Path):
    registry_path = _write_registry(test_workspace, _object_schema())

    with pytest.raises(ValueError, match="max_issues"):
        execute_pinned_schema(
            {},
            registry_path=registry_path,
            bundle_id="test_bundle",
            artifact_id="test_schema",
            limits=SchemaExecutionLimits(max_issues=0),
        )
    with pytest.raises(ValueError, match="target"):
        execute_pinned_schema(
            {},
            registry_path=registry_path,
            bundle_id="test_bundle",
            artifact_id="test_schema",
            target="not-a-target",
        )
