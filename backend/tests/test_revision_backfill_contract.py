"""Red-first PR5 contract for safe legacy-projection backfill.

Production should implement ``app.services.conversion.revision_backfill`` with
the frozen request/result types imported below.  This suite intentionally uses
only per-test SQLite files beneath ``backend/.test-artifacts``.  It never opens
``backend/data`` and never treats a legacy relational projection as original
NBI/SNBI source evidence.

The locked workflow is two-phase:

1. a read-only dry run inventories the exact source database and produces a
   deterministic, content-bound manifest; then
2. an explicit apply supplies that manifest digest, an explicit workspace,
   actor, profile, destination, and publication choice.

Shadow-database migration is the default.  Writing back into the source file is
allowed only for a non-dry-run request with ``in_place=True``.  All snapshots
are reconstructed with their exact stored ``data_year`` and normalized into
canonical SNBI before one atomic write.  A legacy projection is research
provenance, not proof that original upload bytes or field lineage survived.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from typing import Any, Dict

import pytest

from app.routers.services.database import ensure_schema, load_json_to_db
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.routers.services.snbi_schema import ALL_NESTED_ARRAY_NAMES


try:
    from app.services.conversion.revision_backfill import (
        BackfillManifest,
        BackfillSnapshotPlan,
        LegacyBackfillError,
        LegacyBackfillRequest,
        LegacyBackfillResult,
        backfill_legacy_projection,
    )

    _BACKFILL_IMPORT_ERROR: BaseException | None = None
except (ImportError, ModuleNotFoundError) as exc:  # Expected initial PR5 state.
    BackfillManifest = None  # type: ignore[assignment,misc]
    BackfillSnapshotPlan = None  # type: ignore[assignment,misc]
    LegacyBackfillRequest = None  # type: ignore[assignment,misc]
    LegacyBackfillResult = None  # type: ignore[assignment,misc]
    LegacyBackfillError = RuntimeError  # type: ignore[assignment,misc]
    backfill_legacy_projection = None  # type: ignore[assignment]
    _BACKFILL_IMPORT_ERROR = exc


WORKSPACE = "njdot-legacy-research"
ACTOR = "migration.operator@rowan.edu"
PROFILE = "research_historical_import"
STATE = 34

PROJECTION_TABLES = (
    "bridges",
    "features",
    "routes",
    "span_sets",
    "substructure_sets",
    "elements",
    "inspections",
    "posting_evaluations",
    "posting_statuses",
    "works",
)


def _require_backfill_api() -> None:
    if _BACKFILL_IMPORT_ERROR is not None:
        pytest.fail(
            "PR5 backfill API is not implemented: create "
            "app.services.conversion.revision_backfill with the frozen types "
            f"and function documented in this module ({_BACKFILL_IMPORT_ERROR})",
            pytrace=False,
        )


def _bridge(
    bridge_number: str,
    *,
    name: str,
    year_built: int,
    feature_type: str = "H01",
) -> Dict[str, Any]:
    """Return a projection fixture exercising every nested SNBI container."""

    return {
        "BL01": STATE,
        "BID01": bridge_number,
        "BID02": name,
        "BL03": "00123",
        "BW01": year_built,
        "Elements": [
            {"BE01": "12", "BE02": "0", "BE03": 125.5, "BCS01": 100.0}
        ],
        "Features": [
            {
                "BF01": feature_type,
                "BF02": "1",
                "BRR01": "R01",
                "Routes": [
                    {"BRT01": "C01", "BRT02": "0001", "BRT03": "N"}
                ],
            }
        ],
        "Inspections": [
            {"BIE01": "REG", "BIE02": "2024-06-01", "BIE05": 24}
        ],
        "PostingEvaluations": [
            {"BEP01": "1", "BEP02": 1.25, "BEP03": "A"}
        ],
        "PostingStatuses": [{"BPS01": "A", "BPS02": "2024-06-01"}],
        "SpanSets": [{"BSP01": "1", "BSP02": "01", "BSP03": "01"}],
        "SubstructureSets": [
            {"BSB01": "1", "BSB02": 2, "BSB03": "01", "BSB04": "01"}
        ],
        "Works": [{"BW02": 2020, "BW03": "Deck rehabilitation"}],
    }


def _make_source(test_workspace: Path) -> Path:
    source = test_workspace / "legacy-source.sqlite"
    ensure_schema(str(source))
    fixtures = (
        (_bridge("BG-001", name="Old name", year_built=1939), 2021),
        (_bridge("BG-001", name="Current name", year_built=1939), 2024),
        (
            _bridge(
                "BG-002",
                name="Second bridge",
                year_built=1977,
                feature_type="R01",
            ),
            2023,
        ),
    )
    for document, data_year in fixtures:
        inserted, existed, skipped, reasons = load_json_to_db(
            str(source), [document], data_year_override=data_year
        )
        assert (inserted, existed, skipped, reasons) == (1, 0, 0, [])
    return source


def _request(
    source: Path,
    destination: Path,
    **overrides: Any,
) -> Any:
    values: Dict[str, Any] = {
        "source_db_path": source,
        "destination_db_path": destination,
        "workspace_id": WORKSPACE,
        "actor_id": ACTOR,
        "profile_id": PROFILE,
        "request_id": "legacy-backfill-test",
        "dry_run": True,
        "publish": False,
        "in_place": False,
        "expected_manifest_sha256": None,
    }
    values.update(overrides)
    return LegacyBackfillRequest(**values)


def _dry_run(source: Path, destination: Path) -> Any:
    _require_backfill_api()
    return backfill_legacy_projection(_request(source, destination))


def _apply(
    source: Path,
    destination: Path,
    manifest_sha256: str,
    **overrides: Any,
) -> Any:
    return backfill_legacy_projection(
        _request(
            source,
            destination,
            dry_run=False,
            expected_manifest_sha256=manifest_sha256,
            **overrides,
        )
    )


def _rows(path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _scalar(path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
    rows = _rows(path, sql, parameters)
    assert len(rows) == 1 and len(rows[0]) == 1
    return rows[0][0]


def _assert_error(error: BaseException, *, code: str) -> None:
    assert isinstance(error, LegacyBackfillError)
    assert error.code == code
    assert isinstance(str(error), str) and str(error).strip()


def test_dry_run_is_read_only_deterministic_and_inventory_is_content_bound(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination_a = test_workspace / "shadow-a.sqlite"
    destination_b = test_workspace / "shadow-b.sqlite"
    source_before = source.read_bytes()
    expected_database_sha256 = hashlib.sha256(source_before).hexdigest()

    real_connect = sqlite3.connect
    connect_calls: list[tuple[object, dict[str, Any]]] = []

    def recording_connect(database: object, *args: Any, **kwargs: Any):
        connect_calls.append((database, dict(kwargs)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    first = backfill_legacy_projection(_request(source, destination_a))
    second = backfill_legacy_projection(_request(source, destination_b))

    assert is_dataclass(first) and isinstance(first, LegacyBackfillResult)
    assert first == second
    assert first.dry_run is True
    assert first.applied is False
    assert first.no_op is False
    assert first.staged_revision_ids == ()
    assert first.published_revision_ids == ()
    assert not destination_a.exists()
    assert not destination_b.exists()
    assert source.read_bytes() == source_before

    # Opening an ordinary SQLite connection can create journals or accidentally
    # invoke startup migration.  The source inventory must use SQLite URI
    # read-only mode, not merely promise not to execute INSERT/UPDATE.
    source_resolved = source.resolve()
    source_opens = []
    for database, kwargs in connect_calls:
        database_text = str(database)
        if source_resolved.as_posix() in database_text.replace("\\", "/"):
            source_opens.append((database_text, kwargs))
    assert source_opens
    assert all("mode=ro" in database for database, _ in source_opens)
    assert all(kwargs.get("uri") is True for _, kwargs in source_opens)

    manifest = first.manifest
    assert is_dataclass(manifest) and isinstance(manifest, BackfillManifest)
    assert manifest.format_version == "bridgeguard-legacy-backfill-v1"
    assert manifest.source_database_sha256 == expected_database_sha256
    assert manifest.source_database_size == len(source_before)
    assert len(manifest.source_schema_sha256) == 64
    assert manifest.source_user_version == LATEST_SCHEMA_VERSION
    assert manifest.quick_check == "ok"
    assert manifest.foreign_key_violations == ()
    assert len(manifest.manifest_sha256) == 64

    counts = dict(manifest.table_counts)
    assert {name: counts[name] for name in PROJECTION_TABLES} == {
        "bridges": 3,
        "features": 3,
        "routes": 3,
        "span_sets": 3,
        "substructure_sets": 3,
        "elements": 3,
        "inspections": 3,
        "posting_evaluations": 3,
        "posting_statuses": 3,
        "works": 3,
    }
    assert counts["snapshot_revisions"] == 0
    assert tuple(
        (item.state_code, item.bridge_number, item.data_year)
        for item in manifest.snapshots
    ) == (
        (STATE, "BG-001", 2021),
        (STATE, "BG-001", 2024),
        (STATE, "BG-002", 2023),
    )
    assert all(
        isinstance(item, BackfillSnapshotPlan)
        and len(item.canonical_sha256) == 64
        for item in manifest.snapshots
    )

    with pytest.raises(FrozenInstanceError):
        first.applied = True
    with pytest.raises(FrozenInstanceError):
        manifest.quick_check = "changed"
    with pytest.raises(FrozenInstanceError):
        manifest.snapshots[0].data_year = 1900


@pytest.mark.parametrize("field", ("workspace_id", "actor_id", "profile_id"))
def test_scope_and_ownership_must_be_explicit_and_nonblank(
    test_workspace: Path,
    field: str,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / f"missing-{field}.sqlite"

    with pytest.raises(LegacyBackfillError) as raised:
        backfill_legacy_projection(_request(source, destination, **{field: "  "}))

    _assert_error(raised.value, code="explicit_scope_required")
    assert not destination.exists()


def test_paths_are_explicit_and_same_file_requires_apply_in_place_opt_in(
    test_workspace: Path,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    shadow = test_workspace / "shadow.sqlite"

    with pytest.raises(LegacyBackfillError) as dry_collision:
        backfill_legacy_projection(_request(source, source, in_place=True))
    _assert_error(dry_collision.value, code="path_collision")

    manifest = _dry_run(source, shadow).manifest
    with pytest.raises(LegacyBackfillError) as apply_collision:
        _apply(source, source, manifest.manifest_sha256)
    _assert_error(apply_collision.value, code="path_collision")

    result = _apply(
        source,
        source,
        manifest.manifest_sha256,
        in_place=True,
    )
    assert result.applied is True
    assert len(result.staged_revision_ids) == 3
    assert _scalar(source, "SELECT COUNT(*) FROM snapshot_revisions") == 3


def test_apply_requires_exact_dry_run_manifest_and_rejects_source_drift(
    test_workspace: Path,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / "shadow.sqlite"

    with pytest.raises(LegacyBackfillError) as no_precondition:
        backfill_legacy_projection(
            _request(source, destination, dry_run=False)
        )
    _assert_error(no_precondition.value, code="manifest_precondition_required")
    assert not destination.exists()

    manifest = _dry_run(source, destination).manifest
    connection = sqlite3.connect(source)
    try:
        connection.execute(
            "UPDATE bridges SET bridge_name=? "
            "WHERE state_code=? AND bridge_number=? AND data_year=?",
            ("Changed after preflight", STATE, "BG-001", 2024),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LegacyBackfillError) as changed:
        _apply(source, destination, manifest.manifest_sha256)
    _assert_error(changed.value, code="source_changed")
    if destination.exists():
        assert _scalar(destination, "SELECT COUNT(*) FROM snapshot_revisions") == 0
        assert _scalar(destination, "SELECT COUNT(*) FROM workspaces") == 0


def test_apply_stages_exact_years_and_canonical_children_without_publishing(
    test_workspace: Path,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / "shadow.sqlite"
    manifest = _dry_run(source, destination).manifest

    result = _apply(source, destination, manifest.manifest_sha256)

    assert result.applied is True
    assert result.no_op is False
    assert result.dry_run is False
    assert len(result.staged_revision_ids) == 3
    assert result.published_revision_ids == ()
    assert _rows(
        destination,
        "SELECT state_code, bridge_number, data_year, revision_number, "
        "lifecycle_state FROM snapshot_revisions "
        "ORDER BY state_code, bridge_number COLLATE BINARY, data_year",
    ) == [
        (STATE, "BG-001", 2021, 1, "staged"),
        (STATE, "BG-001", 2024, 1, "staged"),
        (STATE, "BG-002", 2023, 1, "staged"),
    ]
    assert _scalar(destination, "SELECT COUNT(*) FROM published_snapshots") == 0
    assert _scalar(destination, "SELECT COUNT(*) FROM bridges") == 0
    assert _scalar(
        destination,
        "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
        (WORKSPACE,),
    ) == 0

    canonical_text = _scalar(
        destination,
        "SELECT canonical_json FROM snapshot_revisions "
        "WHERE workspace_id=? AND state_code=? AND bridge_number=? AND data_year=?",
        (WORKSPACE, STATE, "BG-001", 2024),
    )
    canonical = json.loads(canonical_text)
    assert canonical["BL01"] == STATE
    assert canonical["BID01"] == "BG-001"
    assert canonical["BID02"] == "Current name"
    assert canonical["BW01"] == 1939
    assert tuple(name for name in ALL_NESTED_ARRAY_NAMES if name in canonical) == tuple(
        ALL_NESTED_ARRAY_NAMES
    )
    assert all(isinstance(canonical[name], list) for name in ALL_NESTED_ARRAY_NAMES)
    assert canonical["Features"][0]["BF01"] == "H01"
    assert canonical["Features"][0]["BRR01"] == "R01"
    assert canonical["Features"][0]["Routes"][0]["BRT01"] == "C01"
    assert canonical["Elements"][0]["BE03"] == 125.5
    assert canonical["Works"][0]["BW02"] == 2020

    # This is explicitly a reconstruction from the relational read model.  It
    # must not be labeled as an original native file or receive invented
    # target-to-source field lineage.
    artifact_rows = _rows(
        destination,
        "SELECT artifact_kind, source_system, storage_uri, sha256, byte_size "
        "FROM source_artifacts ORDER BY id",
    )
    assert artifact_rows
    assert all(row[0] == "legacy_projection_database" for row in artifact_rows)
    assert all(row[1] == "bridgeguard_legacy_projection" for row in artifact_rows)
    assert all(row[2] is None for row in artifact_rows)
    assert all(len(row[3]) == 64 and row[4] > 0 for row in artifact_rows)
    assert _rows(
        destination,
        "SELECT DISTINCT source_role, parse_status FROM source_records",
    ) == [("legacy_projection_reconstruction", "reconstructed_research")]
    assert _scalar(destination, "SELECT COUNT(*) FROM field_lineage") == 0
    assert _rows(
        destination,
        "SELECT DISTINCT status, profile_id FROM validation_runs",
    ) == [("passed_with_warnings", PROFILE)]
    assert _scalar(
        destination,
        "SELECT COUNT(*) FROM validation_issues "
        "WHERE code='LEGACY_PROJECTION_RECONSTRUCTION' "
        "AND severity='warning'",
    ) == 3


def test_explicit_publish_is_atomic_projects_all_snapshots_and_increments_once(
    test_workspace: Path,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / "published-shadow.sqlite"
    manifest = _dry_run(source, destination).manifest

    result = _apply(
        source,
        destination,
        manifest.manifest_sha256,
        publish=True,
    )

    assert result.applied is True
    assert len(result.staged_revision_ids) == 3
    assert len(result.published_revision_ids) == 3
    assert _scalar(destination, "SELECT COUNT(*) FROM published_snapshots") == 3
    assert _rows(
        destination,
        "SELECT state_code, bridge_number, data_year FROM bridges "
        "ORDER BY state_code, bridge_number COLLATE BINARY, data_year",
    ) == [
        (STATE, "BG-001", 2021),
        (STATE, "BG-001", 2024),
        (STATE, "BG-002", 2023),
    ]
    assert _scalar(
        destination,
        "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
        (WORKSPACE,),
    ) == 1
    assert _scalar(
        destination,
        "SELECT COUNT(*) FROM inspection_report_history "
        "WHERE report_file='JSON_LOAD'",
    ) == 0


@pytest.mark.parametrize("publish", (False, True))
def test_apply_is_deterministic_and_idempotent_across_retries(
    test_workspace: Path,
    publish: bool,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / f"retry-{publish}.sqlite"
    manifest = _dry_run(source, destination).manifest

    first = _apply(
        source,
        destination,
        manifest.manifest_sha256,
        publish=publish,
    )
    before = {
        table: _scalar(destination, f'SELECT COUNT(*) FROM "{table}"')
        for table in (
            "source_artifacts",
            "source_records",
            "conversion_batches",
            "snapshot_revisions",
            "published_snapshots",
            "validation_runs",
            "validation_issues",
            "mutation_audit",
        )
    }
    generation_before = _scalar(
        destination,
        "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
        (WORKSPACE,),
    )

    repeated = _apply(
        source,
        destination,
        manifest.manifest_sha256,
        publish=publish,
    )

    assert first.manifest == repeated.manifest
    assert repeated.no_op is True
    assert repeated.staged_revision_ids == first.staged_revision_ids
    assert repeated.published_revision_ids == first.published_revision_ids
    assert {
        table: _scalar(destination, f'SELECT COUNT(*) FROM "{table}"')
        for table in before
    } == before
    assert _scalar(
        destination,
        "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
        (WORKSPACE,),
    ) == generation_before


def test_missing_year_is_not_inferred_from_inspection_or_current_time(
    test_workspace: Path,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / "missing-year-shadow.sqlite"
    connection = sqlite3.connect(source)
    try:
        connection.execute(
            "INSERT INTO bridges (state_code, bridge_number, bridge_name, data_year) "
            "VALUES (?, ?, ?, NULL)",
            (STATE, "NO-YEAR", "Must fail closed"),
        )
        connection.execute(
            "INSERT INTO inspections "
            "(state_code, bridge_number, inspection_type, inspection_begin_date, "
            "data_year) VALUES (?, ?, ?, ?, NULL)",
            (STATE, "NO-YEAR", "REG", "2022-07-01"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LegacyBackfillError) as raised:
        _dry_run(source, destination)

    _assert_error(raised.value, code="data_year_required")
    assert not destination.exists()


def test_source_integrity_or_unsupported_projection_fails_closed(
    test_workspace: Path,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / "corrupt-shadow.sqlite"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO routes "
            "(state_code, bridge_number, data_year, feature_type, "
            "route_designation) VALUES (?, ?, ?, ?, ?)",
            (STATE, "BG-001", 2024, "NO-SUCH-FEATURE", "ORPHAN"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LegacyBackfillError) as raised:
        _dry_run(source, destination)

    _assert_error(raised.value, code="source_integrity_error")
    assert not destination.exists()


def test_induced_destination_failure_rolls_back_the_whole_backfill(
    test_workspace: Path,
):
    _require_backfill_api()
    source = _make_source(test_workspace)
    destination = test_workspace / "rollback-shadow.sqlite"
    manifest = _dry_run(source, destination).manifest
    ensure_schema(str(destination))
    connection = sqlite3.connect(destination)
    try:
        connection.execute(
            "CREATE TRIGGER induce_second_bridge_failure "
            "BEFORE INSERT ON snapshot_revisions "
            "FOR EACH ROW WHEN NEW.bridge_number='BG-002' BEGIN "
            "SELECT RAISE(ABORT, 'induced backfill failure'); END"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LegacyBackfillError) as raised:
        _apply(source, destination, manifest.manifest_sha256, publish=True)

    assert raised.value.code in {"apply_failed", "destination_schema_error"}
    for table in (
        "workspaces",
        "source_artifacts",
        "source_records",
        "conversion_batches",
        "snapshot_revisions",
        "published_snapshots",
        "validation_runs",
        "validation_issues",
        "mutation_audit",
        "bridges",
    ):
        assert _scalar(destination, f'SELECT COUNT(*) FROM "{table}"') == 0
