"""Red-first PR5 contract for canonical revisions and staged publication.

Locked public API
=================

Production should implement ``app.services.conversion.revision_store`` with
the frozen request/result dataclasses and functions imported below.

Lifecycle semantics locked by this suite:

* staging stores immutable canonical/source/lineage/validation evidence but is
  not visible to the legacy projection and does not increment data_generation;
* identical canonical content is a no-op even when object key order differs;
* changed content receives the next monotonically increasing revision number
  and a stable JSON-Pointer-ordered diff;
* an exact idempotency-key replay returns the original batch outcome, while the
  same key with a changed payload is a typed HTTP-409 conflict;
* publish is the visibility mutation and increments data_generation exactly
  once; RFC-7396-like patch is optimistic but creates a staged revision only,
  so it leaves both the active read model and data_generation unchanged;
* batch staging is all-or-nothing.  A duplicate logical snapshot or invalid
  input is rejected with HTTP-422 semantics before the first write.

All SQLite files come from the isolated test workspace configured by
``conftest``; production databases are never opened.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from typing import Any, Dict

import pytest

from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.conversion.canonical_json import canonical_sha256
from app.services.conversion import revision_store as revision_store_module
from app.services.conversion.rule_bundle_manifest import (
    RuleBundleManifest,
    build_rule_bundle_manifest,
)


try:
    from app.services.conversion.revision_store import (
        CanonicalRevisionInput,
        IdempotencyConflictError,
        MutationAuditRecord,
        PatchSnapshotRevisionRequest,
        PatchSnapshotRevisionResult,
        PublishSnapshotRevisionRequest,
        PublishSnapshotRevisionResult,
        RevisionConflictError,
        RevisionInputError,
        RevisionNotFoundError,
        RevisionStoreError,
        SnapshotRevisionRecord,
        StageCanonicalBatchRequest,
        StageCanonicalBatchResult,
        StagedRevisionResult,
        WorkspaceBoundaryError,
        WorkspaceState,
        ensure_workspace,
        get_published_snapshot,
        get_snapshot_history,
        get_snapshot_revision,
        get_workspace_state,
        initialize_revision_store,
        list_mutation_audit,
        patch_snapshot_revision,
        publish_snapshot_revision,
        stage_canonical_batch,
    )

    _REVISION_IMPORT_ERROR: BaseException | None = None
except (ImportError, ModuleNotFoundError) as exc:  # Expected initial PR5 state.
    CanonicalRevisionInput = None  # type: ignore[assignment,misc]
    MutationAuditRecord = None  # type: ignore[assignment,misc]
    PatchSnapshotRevisionRequest = None  # type: ignore[assignment,misc]
    PatchSnapshotRevisionResult = None  # type: ignore[assignment,misc]
    PublishSnapshotRevisionRequest = None  # type: ignore[assignment,misc]
    PublishSnapshotRevisionResult = None  # type: ignore[assignment,misc]
    SnapshotRevisionRecord = None  # type: ignore[assignment,misc]
    StageCanonicalBatchRequest = None  # type: ignore[assignment,misc]
    StageCanonicalBatchResult = None  # type: ignore[assignment,misc]
    StagedRevisionResult = None  # type: ignore[assignment,misc]
    WorkspaceState = None  # type: ignore[assignment,misc]
    IdempotencyConflictError = RuntimeError  # type: ignore[assignment,misc]
    RevisionConflictError = RuntimeError  # type: ignore[assignment,misc]
    RevisionInputError = RuntimeError  # type: ignore[assignment,misc]
    RevisionNotFoundError = RuntimeError  # type: ignore[assignment,misc]
    RevisionStoreError = RuntimeError  # type: ignore[assignment,misc]
    WorkspaceBoundaryError = RuntimeError  # type: ignore[assignment,misc]
    ensure_workspace = None  # type: ignore[assignment]
    get_published_snapshot = None  # type: ignore[assignment]
    get_snapshot_history = None  # type: ignore[assignment]
    get_snapshot_revision = None  # type: ignore[assignment]
    get_workspace_state = None  # type: ignore[assignment]
    initialize_revision_store = None  # type: ignore[assignment]
    list_mutation_audit = None  # type: ignore[assignment]
    patch_snapshot_revision = None  # type: ignore[assignment]
    publish_snapshot_revision = None  # type: ignore[assignment]
    stage_canonical_batch = None  # type: ignore[assignment]
    _REVISION_IMPORT_ERROR = exc


WORKSPACE_A = "njdot-research"
WORKSPACE_B = "rowan-sandbox"
STATE = 34
YEAR = 2026
BRIDGE_A = "REV-0001"
BRIDGE_B = "REV-0002"


def _require_revision_api() -> None:
    if _REVISION_IMPORT_ERROR is not None:
        pytest.fail(
            "PR5 public API is not implemented: create "
            "app.services.conversion.revision_store with the frozen types and "
            f"functions documented in this module ({_REVISION_IMPORT_ERROR})",
            pytrace=False,
        )


def _document(
    bridge_number: str = BRIDGE_A,
    *,
    bridge_name: str = "Revision one",
    year_built: int = 1977,
    feature_type: str = "H01",
) -> Dict[str, Any]:
    return {
        "BL01": STATE,
        "BID01": bridge_number,
        "BID02": bridge_name,
        "BL03": "00123",
        "BW01": year_built,
        "Elements": [],
        "Features": [
            {
                "BF01": feature_type,
                "BF02": "1",
                "Routes": [
                    {"BRT01": "C01", "BRT02": "0001", "BRT03": "N"}
                ],
            }
        ],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


def _source_records(record_id: str = "record-001") -> tuple[Dict[str, Any], ...]:
    return (
        {
            "source_artifact_id": "artifact-001",
            "source_record_id": record_id,
            "record_kind": "native_snbi",
            "record_pointer": "/0",
            "payload": {"BID01": BRIDGE_A, "raw": "immutable evidence"},
        },
    )


def _lineage(rule_id: str = "DIRECT-BID02") -> tuple[Dict[str, Any], ...]:
    return (
        {
            "target_pointer": "/BID02",
            "source_artifact_id": "artifact-001",
            "source_record_id": "record-001",
            "source_pointer": "/BID02",
            "rule_id": rule_id,
            "status": "exact",
        },
    )


def _validation(rule_id: str = "BG-REVIEW-NAME") -> Dict[str, Any]:
    return {
        "profile_id": "research_historical_import",
        "status": "passed_with_warnings",
        "validation_owner": "server",
        "official_validation": False,
        "issues": (
            {
                "rule_id": rule_id,
                "severity": "warning",
                "json_pointer": "/BID02",
                "message": "Fixture warning retained as evidence",
            },
        ),
    }


def _input(
    bridge_number: str = BRIDGE_A,
    *,
    document: Dict[str, Any] | None = None,
    data_year: int | None = YEAR,
    lineage: tuple[Dict[str, Any], ...] | None = None,
    validation: Dict[str, Any] | None = None,
) -> Any:
    return CanonicalRevisionInput(
        state_code=STATE,
        bridge_number=bridge_number,
        data_year=data_year,
        canonical_document=document or _document(bridge_number),
        source_records=_source_records(f"record-{bridge_number}"),
        lineage=lineage if lineage is not None else _lineage(),
        validation=validation if validation is not None else _validation(),
    )


def _stage_request(
    *inputs: Any,
    workspace_id: str = WORKSPACE_A,
    idempotency_key: str = "stage-001",
    request_id: str = "request-001",
    expected_data_generation: int | None = 0,
    atomic_mode: str = "batch",
) -> Any:
    return StageCanonicalBatchRequest(
        workspace_id=workspace_id,
        profile_id="research_historical_import",
        actor_id="user-001",
        request_id=request_id,
        idempotency_key=idempotency_key,
        expected_data_generation=expected_data_generation,
        atomic_mode=atomic_mode,
        revisions=tuple(inputs or (_input(),)),
    )


def _publish_request(
    revision_id: int,
    *,
    expected_revision_id: int | None,
    expected_data_generation: int,
    request_id: str = "publish-request-001",
) -> Any:
    return PublishSnapshotRevisionRequest(
        workspace_id=WORKSPACE_A,
        revision_id=revision_id,
        actor_id="publisher-001",
        request_id=request_id,
        reason="Reviewed publication",
        expected_revision_id=expected_revision_id,
        expected_data_generation=expected_data_generation,
    )


def _canonical_text(document: Dict[str, Any]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_hash(document: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_text(document).encode("utf-8")).hexdigest()


@pytest.fixture
def revision_db(test_workspace: Path) -> str:
    _require_revision_api()
    path = str(test_workspace / "canonical-revisions.db")
    ensure_schema(path)
    initialize_revision_store(path, app_revision="pr5-contract")
    ensure_workspace(path, WORKSPACE_A, "NJDOT research")
    ensure_workspace(path, WORKSPACE_B, "Rowan sandbox")
    return path


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _scalar(path: str, sql: str, parameters=()):
    connection = _connect(path)
    try:
        row = connection.execute(sql, parameters).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


def _projection(path: str, bridge_number: str = BRIDGE_A):
    connection = _connect(path)
    try:
        bridge = connection.execute(
            "SELECT bridge_name, year_built FROM bridges "
            "WHERE state_code=? AND bridge_number=? AND data_year=?",
            (STATE, bridge_number, YEAR),
        ).fetchone()
        features = connection.execute(
            "SELECT feature_type, feature_location FROM features "
            "WHERE state_code=? AND bridge_number=? AND data_year=? ORDER BY id",
            (STATE, bridge_number, YEAR),
        ).fetchall()
        return bridge, features
    finally:
        connection.close()


def _stage_one(path: str, **request_overrides):
    request = _stage_request(_input(), **request_overrides)
    return stage_canonical_batch(path, request)


def _assert_error(exc: BaseException, *, status: int, code: str) -> None:
    assert isinstance(exc, RevisionStoreError)
    assert exc.status == status
    assert exc.code == code


def test_public_contract_types_are_frozen_and_errors_are_http_ready(revision_db):
    request = _stage_request(_input())
    result = stage_canonical_batch(revision_db, request)

    for value in (
        request,
        request.revisions[0],
        result,
        result.revisions[0],
        get_workspace_state(revision_db, WORKSPACE_A),
        get_snapshot_revision(
            revision_db, WORKSPACE_A, result.revisions[0].revision_id
        ),
    ):
        assert is_dataclass(value)
        with pytest.raises(FrozenInstanceError):
            value.workspace_id = "mutated"  # type: ignore[misc]

    for cls in (
        WorkspaceBoundaryError,
        RevisionNotFoundError,
        RevisionConflictError,
        IdempotencyConflictError,
        RevisionInputError,
    ):
        assert issubclass(cls, RevisionStoreError)


def test_schema_and_workspace_state_initialize_idempotently(revision_db):
    state = get_workspace_state(revision_db, WORKSPACE_A)
    assert isinstance(state, WorkspaceState)
    assert state.data_generation == 0
    assert state.schema_generation == LATEST_SCHEMA_VERSION

    initialize_revision_store(revision_db, app_revision="pr5-contract")
    ensure_workspace(revision_db, WORKSPACE_A, "Display name may change")
    assert get_workspace_state(revision_db, WORKSPACE_A) == state

    expected_tables = (
        "workspaces",
        "source_records",
        "snapshot_revisions",
        "published_snapshots",
        "field_lineage",
        "validation_runs",
        "validation_issues",
        "mutation_audit",
        "workspace_state",
    )
    for table in expected_tables:
        assert _scalar(
            revision_db,
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ) == 1


def test_rule_bundle_manifest_is_deterministic_and_profile_specific():
    research = build_rule_bundle_manifest("research_historical_import")
    replay = build_rule_bundle_manifest("research_historical_import")
    native = build_rule_bundle_manifest("native_snbi")

    assert research == replay
    assert research.bundle_id == "research_historical_import"
    assert research.sha256 != native.sha256
    assert research.manifest["rule_application_sha256"] == canonical_sha256(
        research.manifest["rule_application_versions"]
    )
    assert research.manifest["artifact_registry"]["sha256"]


def test_changed_rule_bundle_creates_new_revision_for_identical_content(
    revision_db, monkeypatch
):
    first = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(),
            idempotency_key="bundle-version-1",
            request_id="bundle-version-request-1",
        ),
    )
    original = build_rule_bundle_manifest("research_historical_import")
    changed_manifest = {
        **original.manifest,
        "test_rule_application_release": "v2",
    }
    changed = RuleBundleManifest(
        bundle_id=original.bundle_id,
        sha256=canonical_sha256(changed_manifest),
        manifest=changed_manifest,
    )
    monkeypatch.setattr(
        revision_store_module,
        "build_rule_bundle_manifest",
        lambda profile_id: changed,
    )

    second = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(),
            idempotency_key="bundle-version-2",
            request_id="bundle-version-request-2",
        ),
    )

    assert second.revisions[0].created is True
    assert second.revisions[0].revision_number == 2
    assert second.revisions[0].revision_id != first.revisions[0].revision_id
    record = get_snapshot_revision(
        revision_db, WORKSPACE_A, second.revisions[0].revision_id
    )
    assert record.content_hash == first.revisions[0].content_hash
    assert record.rule_bundle_sha256 == changed.sha256
    assert record.rule_bundle_manifest == changed.manifest


def test_duplicate_logical_keys_and_missing_year_reject_prewrite(revision_db):
    duplicate = _stage_request(
        _input(),
        _input(document=_document(bridge_name="Changed duplicate")),
        idempotency_key="duplicate-batch",
    )
    with pytest.raises(RevisionInputError) as duplicate_error:
        stage_canonical_batch(revision_db, duplicate)
    _assert_error(
        duplicate_error.value,
        status=422,
        code="duplicate_logical_snapshot",
    )
    assert get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    ) == ()
    assert list_mutation_audit(revision_db, WORKSPACE_A) == ()

    missing_year = _stage_request(
        _input(data_year=None),
        idempotency_key="missing-year",
    )
    with pytest.raises(RevisionInputError) as year_error:
        stage_canonical_batch(revision_db, missing_year)
    _assert_error(year_error.value, status=422, code="data_year_required")
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 0


def test_staging_is_immutable_monotonic_diffed_and_generation_neutral(revision_db):
    original = _document()
    first = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(document=original),
            idempotency_key="monotonic-1",
            request_id="monotonic-request-1",
        ),
    )
    one = first.revisions[0]
    assert isinstance(first, StageCanonicalBatchResult)
    assert isinstance(one, StagedRevisionResult)
    assert one.revision_number == 1
    assert one.created is True and one.idempotent is False
    assert one.content_hash == _canonical_hash(_document())
    assert first.data_generation == 0

    original["BID02"] = "Caller mutation cannot change evidence"
    record = get_snapshot_revision(revision_db, WORKSPACE_A, one.revision_id)
    assert isinstance(record, SnapshotRevisionRecord)
    assert record.canonical_document == _document()
    assert record.canonical_json == _canonical_text(_document())
    assert record.content_hash == one.content_hash
    expected_bundle = build_rule_bundle_manifest("research_historical_import")
    assert record.rule_bundle_id == "research_historical_import"
    assert record.rule_bundle_sha256 == expected_bundle.sha256
    assert record.rule_bundle_manifest == expected_bundle.manifest
    assert record.validation["rule_bundle_sha256"] == expected_bundle.sha256
    assert record.validation["rule_bundle_id"] == "research_historical_import"
    assert record.rule_bundle_manifest["authority_boundary"] == {
        "artifact_registration_only": True,
        "verified_source_bytes_are_not_verified_rule_execution": True,
        "external_validation": False,
        "official_fhwa_nbtis_validation": False,
        "submission_ready": False,
    }
    assert {
        artifact["artifact_id"]
        for artifact in record.rule_bundle_manifest["source_artifacts"]
    } >= {
        "nbi_snbi_crosswalk_dec_2023",
        "nbi_snbi_code_mapping",
        "legacy_element_xsd_v1_2013",
        "snbi_schema_v04_july_2024",
    }
    connection = sqlite3.connect(revision_db)
    connection.row_factory = sqlite3.Row
    try:
        batch = connection.execute(
            "SELECT rule_bundle_id,rule_bundle_sha256,options_json "
            "FROM conversion_batches WHERE id=?",
            (first.batch_id,),
        ).fetchone()
    finally:
        connection.close()
    assert batch["rule_bundle_id"] == expected_bundle.bundle_id
    assert batch["rule_bundle_sha256"] == expected_bundle.sha256
    assert json.loads(batch["options_json"])["rule_bundle_manifest"] == (
        expected_bundle.manifest
    )

    reordered = dict(reversed(tuple(_document().items())))
    same = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(document=reordered),
            idempotency_key="same-content-new-key",
            request_id="same-content-request",
        ),
    )
    assert same.revisions[0].revision_id == one.revision_id
    assert same.revisions[0].revision_number == 1
    assert same.revisions[0].created is False
    assert same.revisions[0].idempotent is True
    assert same.data_generation == 0

    changed = _document(bridge_name="Revision two", year_built=1978)
    second = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(document=changed),
            idempotency_key="monotonic-2",
            request_id="monotonic-request-2",
        ),
    ).revisions[0]
    assert second.revision_number == 2
    assert second.revision_id != one.revision_id
    assert second.diff == (
        {"op": "replace", "path": "/BID02", "before": "Revision one", "after": "Revision two"},
        {"op": "replace", "path": "/BW01", "before": 1977, "after": 1978},
    )
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 0

    connection = _connect(revision_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE snapshot_revisions SET canonical_json='{}' WHERE id=?",
                (one.revision_id,),
            )
    finally:
        connection.rollback()
        connection.close()


def test_idempotency_replay_and_changed_payload_conflict(revision_db):
    request = _stage_request(
        _input(),
        idempotency_key="replay-key",
        request_id="replay-request",
    )
    first = stage_canonical_batch(revision_db, request)
    replay = stage_canonical_batch(revision_db, request)
    assert replay == first
    assert len(get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    )) == 1

    conflicting = _stage_request(
        _input(document=_document(bridge_name="Conflicting payload")),
        idempotency_key="replay-key",
        request_id="replay-request",
    )
    with pytest.raises(IdempotencyConflictError) as caught:
        stage_canonical_batch(revision_db, conflicting)
    _assert_error(caught.value, status=409, code="idempotency_payload_conflict")
    assert len(get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    )) == 1
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 0


def test_workspace_boundary_is_non_enumerable(revision_db):
    a = stage_canonical_batch(
        revision_db,
        _stage_request(_input(), workspace_id=WORKSPACE_A, idempotency_key="ws-a"),
    ).revisions[0]
    b = stage_canonical_batch(
        revision_db,
        _stage_request(_input(), workspace_id=WORKSPACE_B, idempotency_key="ws-b"),
    ).revisions[0]
    assert a.revision_id != b.revision_id

    with pytest.raises(WorkspaceBoundaryError) as boundary:
        get_snapshot_revision(revision_db, WORKSPACE_A, b.revision_id)
    _assert_error(boundary.value, status=404, code="revision_not_found")
    with pytest.raises(RevisionNotFoundError) as absent:
        get_snapshot_revision(revision_db, WORKSPACE_A, 999_999)
    _assert_error(absent.value, status=404, code="revision_not_found")


def test_source_lineage_validation_and_stage_audit_are_revision_scoped(revision_db):
    first = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(),
            idempotency_key="evidence-1",
            request_id="evidence-request-1",
        ),
    ).revisions[0]
    second = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(
                document=_document(bridge_name="Evidence two"),
                lineage=_lineage("DIRECT-BID02-V2"),
                validation=_validation("BG-REVIEW-NAME-V2"),
            ),
            idempotency_key="evidence-2",
            request_id="evidence-request-2",
        ),
    ).revisions[0]

    record_one = get_snapshot_revision(revision_db, WORKSPACE_A, first.revision_id)
    record_two = get_snapshot_revision(revision_db, WORKSPACE_A, second.revision_id)
    assert record_one.source_records[0]["payload"]["raw"] == "immutable evidence"
    assert record_one.lineage[0]["rule_id"] == "DIRECT-BID02"
    assert record_two.lineage[0]["rule_id"] == "DIRECT-BID02-V2"
    assert record_one.validation["issues"][0]["rule_id"] == "BG-REVIEW-NAME"
    assert record_two.validation["issues"][0]["rule_id"] == "BG-REVIEW-NAME-V2"

    audit = list_mutation_audit(revision_db, WORKSPACE_A)
    assert all(isinstance(row, MutationAuditRecord) for row in audit)
    assert [row.action for row in audit] == ["stage_revision", "stage_revision"]
    assert audit[0].actor_id == "user-001"
    assert audit[0].request_id == "evidence-request-1"
    assert audit[0].idempotency_key == "evidence-1"
    assert audit[0].before_hash is None
    assert audit[0].after_hash == first.content_hash
    assert all(row.created_at for row in audit)


def test_batch_stage_failure_rolls_back_every_record_and_audit(revision_db):
    connection = _connect(revision_db)
    try:
        connection.execute(
            """
            CREATE TRIGGER induce_second_lineage_failure
            BEFORE INSERT ON field_lineage
            WHEN NEW.source_record_id='record-REV-0002'
            BEGIN
                SELECT RAISE(ABORT, 'induced batch lineage failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    request = _stage_request(
        _input(BRIDGE_A),
        _input(BRIDGE_B),
        idempotency_key="atomic-batch",
        atomic_mode="batch",
    )
    with pytest.raises((sqlite3.DatabaseError, RevisionStoreError), match="induced batch lineage failure"):
        stage_canonical_batch(revision_db, request)

    assert get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    ) == ()
    assert get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_B, YEAR
    ) == ()
    assert list_mutation_audit(revision_db, WORKSPACE_A) == ()
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 0


def test_publish_is_optimistic_unique_and_generation_atomic(revision_db):
    first = _stage_one(revision_db, idempotency_key="publish-1").revisions[0]
    assert _projection(revision_db) == (None, [])
    before = get_workspace_state(revision_db, WORKSPACE_A)

    published = publish_snapshot_revision(
        revision_db,
        _publish_request(
            first.revision_id,
            expected_revision_id=None,
            expected_data_generation=before.data_generation,
        ),
    )
    assert isinstance(published, PublishSnapshotRevisionResult)
    assert published.revision_id == first.revision_id
    assert published.previous_revision_id is None
    assert published.data_generation == before.data_generation + 1
    assert get_published_snapshot(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    ).revision_id == first.revision_id
    assert _projection(revision_db) == (("Revision one", 1977), [("H01", "1")])

    second = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(document=_document(bridge_name="Revision two")),
            idempotency_key="publish-2",
            expected_data_generation=published.data_generation,
        ),
    ).revisions[0]
    current = get_workspace_state(revision_db, WORKSPACE_A)
    assert current.data_generation == published.data_generation

    with pytest.raises(RevisionConflictError) as stale_generation:
        publish_snapshot_revision(
            revision_db,
            _publish_request(
                second.revision_id,
                expected_revision_id=first.revision_id,
                expected_data_generation=current.data_generation - 1,
                request_id="stale-generation",
            ),
        )
    _assert_error(stale_generation.value, status=409, code="stale_data_generation")
    with pytest.raises(RevisionConflictError) as stale_revision:
        publish_snapshot_revision(
            revision_db,
            _publish_request(
                second.revision_id,
                expected_revision_id=999_999,
                expected_data_generation=current.data_generation,
                request_id="stale-revision",
            ),
        )
    _assert_error(stale_revision.value, status=409, code="stale_revision")
    assert get_workspace_state(revision_db, WORKSPACE_A) == current
    assert _projection(revision_db) == (("Revision one", 1977), [("H01", "1")])

    replacement = publish_snapshot_revision(
        revision_db,
        _publish_request(
            second.revision_id,
            expected_revision_id=first.revision_id,
            expected_data_generation=current.data_generation,
            request_id="publish-replacement",
        ),
    )
    assert replacement.previous_revision_id == first.revision_id
    assert replacement.data_generation == current.data_generation + 1
    assert _scalar(
        revision_db,
        "SELECT COUNT(*) FROM published_snapshots "
        "WHERE workspace_id=? AND state_code=? AND bridge_number=? AND data_year=?",
        (WORKSPACE_A, STATE, BRIDGE_A, YEAR),
    ) == 1


def test_publish_requires_persisted_server_owned_validation(revision_db):
    staged = stage_canonical_batch(
        revision_db,
        _stage_request(
            _input(validation={"status": "passed", "validation_owner": "client"}),
            idempotency_key="untrusted-validation-stage",
            request_id="untrusted-validation-stage-request",
        ),
    ).revisions[0]

    with pytest.raises(RevisionInputError) as caught:
        publish_snapshot_revision(
            revision_db,
            _publish_request(
                staged.revision_id,
                expected_revision_id=None,
                expected_data_generation=0,
                request_id="untrusted-validation-publish",
            ),
        )
    _assert_error(caught.value, status=422, code="untrusted_validation_evidence")
    assert get_published_snapshot(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    ) is None
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 0


def test_publish_request_replay_is_exact_and_changed_payload_conflicts(revision_db):
    staged = _stage_one(
        revision_db,
        idempotency_key="publish-idempotency-stage",
        request_id="publish-idempotency-stage-request",
    ).revisions[0]
    request = _publish_request(
        staged.revision_id,
        expected_revision_id=None,
        expected_data_generation=0,
        request_id="publish-idempotency-request",
    )
    first = publish_snapshot_revision(revision_db, request)
    replay = publish_snapshot_revision(revision_db, request)
    assert replay == first
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 1
    assert [
        event.action
        for event in list_mutation_audit(revision_db, WORKSPACE_A)
        if event.action == "publish_revision"
    ] == ["publish_revision"]

    changed = PublishSnapshotRevisionRequest(
        **{**request.__dict__, "reason": "Changed reason with reused request ID"}
    )
    with pytest.raises(IdempotencyConflictError) as caught:
        publish_snapshot_revision(revision_db, changed)
    _assert_error(caught.value, status=409, code="request_id_payload_conflict")
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 1


def test_patch_uses_merge_patch_and_creates_generation_neutral_staged_revision(revision_db):
    staged = _stage_one(revision_db, idempotency_key="patch-base").revisions[0]
    published = publish_snapshot_revision(
        revision_db,
        _publish_request(
            staged.revision_id,
            expected_revision_id=None,
            expected_data_generation=0,
        ),
    )
    patch = PatchSnapshotRevisionRequest(
        workspace_id=WORKSPACE_A,
        state_code=STATE,
        bridge_number=BRIDGE_A,
        data_year=YEAR,
        patch={
            "BID02": "Patched name",
            "BW01": None,
            "Features": [
                {"BF01": "R01", "BF02": "2", "Routes": []}
            ],
        },
        actor_id="editor-001",
        request_id="patch-request-001",
        reason="Reviewed editor correction",
        expected_revision_id=staged.revision_id,
        expected_data_generation=published.data_generation,
        validation={
            **_validation("BG-PATCH-REVALIDATED"),
            "validation_owner": "server",
            "official_validation": False,
        },
    )
    result = patch_snapshot_revision(revision_db, patch)
    assert isinstance(result, PatchSnapshotRevisionResult)
    assert result.previous_revision_id == staged.revision_id
    assert result.revision_number == 2
    assert result.data_generation == published.data_generation
    assert tuple(item["path"] for item in result.diff) == (
        "/BID02",
        "/BW01",
        "/Features/0/BF01",
        "/Features/0/BF02",
        "/Features/0/Routes/0",
    )

    active = get_published_snapshot(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    )
    assert active.revision_id == staged.revision_id
    assert active.canonical_document["BID02"] == "Revision one"
    assert active.canonical_document["BW01"] == 1977
    patched = get_snapshot_revision(revision_db, WORKSPACE_A, result.revision_id)
    assert patched.canonical_document["BID02"] == "Patched name"
    assert "BW01" not in patched.canonical_document
    assert patched.canonical_document["Features"] == [
        {"BF01": "R01", "BF02": "2", "Routes": []}
    ]
    assert _projection(revision_db) == (("Revision one", 1977), [("H01", "1")])
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == published.data_generation

    audit = list_mutation_audit(revision_db, WORKSPACE_A)
    patch_event = next(row for row in audit if row.action == "patch_revision")
    assert patch_event.actor_id == "editor-001"
    assert patch_event.request_id == "patch-request-001"
    assert patch_event.reason == "Reviewed editor correction"


def test_patch_requires_fresh_server_validation_even_for_identical_content(revision_db):
    staged = _stage_one(
        revision_db,
        idempotency_key="patch-validation-base",
        request_id="patch-validation-base-request",
    ).revisions[0]
    published = publish_snapshot_revision(
        revision_db,
        _publish_request(
            staged.revision_id,
            expected_revision_id=None,
            expected_data_generation=0,
            request_id="patch-validation-publish",
        ),
    )
    request = PatchSnapshotRevisionRequest(
        workspace_id=WORKSPACE_A,
        state_code=STATE,
        bridge_number=BRIDGE_A,
        data_year=YEAR,
        patch={"BID02": "Revision one"},
        actor_id="editor-001",
        request_id="patch-validation-request",
        reason="No-op still evaluated",
        expected_revision_id=staged.revision_id,
        expected_data_generation=published.data_generation,
        validation=None,
    )

    with pytest.raises(RevisionInputError) as caught:
        patch_snapshot_revision(revision_db, request)
    _assert_error(caught.value, status=422, code="validation_evidence_required")
    assert len(get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    )) == 1
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 1


def test_patch_replay_is_exact_and_validation_evidence_is_immutable(revision_db):
    staged = _stage_one(
        revision_db,
        idempotency_key="patch-replay-base",
        request_id="patch-replay-base-request",
    ).revisions[0]
    published = publish_snapshot_revision(
        revision_db,
        _publish_request(
            staged.revision_id,
            expected_revision_id=None,
            expected_data_generation=0,
            request_id="patch-replay-publish",
        ),
    )
    request = PatchSnapshotRevisionRequest(
        workspace_id=WORKSPACE_A,
        state_code=STATE,
        bridge_number=BRIDGE_A,
        data_year=YEAR,
        patch={"BID02": "Replay-safe patch"},
        actor_id="editor-001",
        request_id="patch-replay-request",
        reason="Test exact patch replay",
        expected_revision_id=staged.revision_id,
        expected_data_generation=published.data_generation,
        validation=_validation("BG-PATCH-REPLAY"),
    )
    first = patch_snapshot_revision(revision_db, request)
    replay = patch_snapshot_revision(revision_db, request)
    assert replay == first
    assert len(get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    )) == 2
    assert get_workspace_state(revision_db, WORKSPACE_A).data_generation == 1

    changed = PatchSnapshotRevisionRequest(
        **{**request.__dict__, "reason": "Changed patch reason with reused request ID"}
    )
    with pytest.raises(IdempotencyConflictError) as caught:
        patch_snapshot_revision(revision_db, changed)
    _assert_error(caught.value, status=409, code="request_id_payload_conflict")

    connection = _connect(revision_db)
    try:
        validation_id = connection.execute(
            "SELECT id FROM validation_runs WHERE workspace_id=? "
            "AND snapshot_revision_id=?",
            (WORKSPACE_A, first.revision_id),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE validation_runs SET status='failed' WHERE id=?",
                (validation_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM validation_runs WHERE id=?",
                (validation_id,),
            )
    finally:
        connection.rollback()
        connection.close()


def test_publish_failure_rolls_back_pointer_projection_audit_and_generation(revision_db):
    staged = _stage_one(
        revision_db,
        idempotency_key="publish-rollback-stage",
    ).revisions[0]
    before = get_workspace_state(revision_db, WORKSPACE_A)
    audit_before = list_mutation_audit(revision_db, WORKSPACE_A)
    connection = _connect(revision_db)
    try:
        connection.execute(
            """
            CREATE TRIGGER induce_publish_audit_failure
            BEFORE INSERT ON mutation_audit
            WHEN NEW.action='publish_revision'
            BEGIN
                SELECT RAISE(ABORT, 'induced publish audit failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises((sqlite3.DatabaseError, RevisionStoreError), match="induced publish audit failure"):
        publish_snapshot_revision(
            revision_db,
            _publish_request(
                staged.revision_id,
                expected_revision_id=None,
                expected_data_generation=before.data_generation,
            ),
        )

    assert get_workspace_state(revision_db, WORKSPACE_A) == before
    assert get_published_snapshot(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    ) is None
    assert _projection(revision_db) == (None, [])
    assert list_mutation_audit(revision_db, WORKSPACE_A) == audit_before


def test_invalid_atomic_mode_is_422_before_write(revision_db):
    request = _stage_request(
        _input(),
        idempotency_key="invalid-atomic-mode",
        atomic_mode="best_effort",
    )
    with pytest.raises(RevisionInputError) as caught:
        stage_canonical_batch(revision_db, request)
    _assert_error(caught.value, status=422, code="unsupported_atomic_mode")
    assert get_snapshot_history(
        revision_db, WORKSPACE_A, STATE, BRIDGE_A, YEAR
    ) == ()
