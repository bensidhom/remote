"""Authoritative SQLite schema manifest for the legacy SNBI projection.

This module is deliberately declarative and has no import-time database side
effects.  PR4 owns the shape of the ``features`` projection here while the
larger revision-store migration is developed separately.  Tables outside this
manifest are tolerated; columns outside a table that *is* owned by this
manifest are deterministic schema drift.

The physical column names mirror ``constants.mapping``.  In particular, the
nine railroad/navigation transition fields were historically accepted by the
canonical document model but discarded by the relational projection.  They
are now first-class nullable columns.  They stay nullable because absence is
material evidence -- a migration must never invent source values.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Iterable, Mapping, Sequence


LATEST_SCHEMA_VERSION = 18


class SchemaDriftError(RuntimeError):
    """Raised when the live schema differs from the authoritative manifest."""

    def __init__(self, differences: Sequence[str]):
        ordered = tuple(sorted(str(item) for item in differences))
        self.differences = ordered
        super().__init__("SQLite schema drift detected: " + "; ".join(ordered))


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    declared_type: str
    nullable: bool = True
    primary_key_position: int = 0
    default_sql: str | None = None

    @property
    def not_null(self) -> bool:
        return not self.nullable


@dataclass(frozen=True)
class ForeignKeySpec:
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_update: str = "NO ACTION"
    on_delete: str = "NO ACTION"


@dataclass(frozen=True)
class IndexSpec:
    name: str
    columns: tuple[str, ...] = ()
    unique: bool = False
    # ``sql_terms`` is reserved for deterministic expression indexes.  When
    # present it replaces ``columns`` in generated SQL and drift comparison.
    sql_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriggerSpec:
    name: str
    sql: str


@dataclass(frozen=True)
class ViewSpec:
    """One checksummed, read-only semantic view owned by a migration."""

    name: str
    sql: str


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    foreign_keys: tuple[ForeignKeySpec, ...] = ()
    indexes: tuple[IndexSpec, ...] = ()
    triggers: tuple[TriggerSpec, ...] = ()


@dataclass(frozen=True)
class SchemaManifest:
    version: int
    tables: tuple[TableSpec, ...]
    views: tuple[ViewSpec, ...] = ()


LEGACY_FEATURE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("id", "INTEGER", primary_key_position=1),
    ColumnSpec("state_code", "INTEGER", nullable=False),
    ColumnSpec("bridge_number", "TEXT", nullable=False),
    ColumnSpec("feature_type", "TEXT"),
    ColumnSpec("feature_location", "TEXT"),
    ColumnSpec("feature_name", "TEXT"),
    ColumnSpec("functional_classification", "TEXT"),
    ColumnSpec("urban_code", "TEXT"),
    ColumnSpec("nhs_designation", "TEXT"),
    ColumnSpec("national_highway_freight_network", "TEXT"),
    ColumnSpec("strahnet_designation", "TEXT"),
    ColumnSpec("lrs_route_id", "TEXT"),
    ColumnSpec("lrs_mile_point", "REAL"),
    ColumnSpec("lanes_on_highway", "REAL"),
    ColumnSpec("annual_average_daily_traffic", "REAL"),
    ColumnSpec("annual_average_daily_truck_traffic", "REAL"),
    ColumnSpec("year_of_annual_average_daily_traffic", "INTEGER"),
    ColumnSpec("highway_max_usable_vertical_clearance", "REAL"),
    ColumnSpec("highway_min_vertical_clearance", "REAL"),
    ColumnSpec("highway_min_horizontal_clearance_left", "REAL"),
    ColumnSpec("highway_min_horizontal_clearance_right", "REAL"),
    ColumnSpec("highway_max_usable_surface_width", "REAL"),
    ColumnSpec("bypass_detour_length", "REAL"),
    ColumnSpec("crossing_bridge_number", "TEXT"),
    ColumnSpec("data_year", "INTEGER"),
)


# The order is intentional.  ALTER TABLE appends columns, so a migrated v0
# database and a database built from an empty file converge on one shape.
FEATURE_TRANSITION_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("railroad_service_type", "TEXT"),       # BRR01
    ColumnSpec("railroad_min_vertical_clearance", "REAL"),  # BRR02
    ColumnSpec("railroad_min_horizontal_offset", "REAL"),   # BRR03
    ColumnSpec("navigable_waterway", "TEXT"),         # BN01
    ColumnSpec("navigable_min_vertical_clearance", "REAL"),  # BN02
    ColumnSpec("movable_bridge_max_navigation_vertical_clearance", "REAL"),  # BN03
    ColumnSpec("navigation_channel_width", "REAL"),   # BN04
    ColumnSpec("navigation_channel_min_horizontal_clearance", "REAL"),  # BN05
    ColumnSpec("substructure_navigation_protection", "TEXT"),  # BN06
)


FEATURE_TAG_TO_COLUMN: Mapping[str, str] = {
    "BRR01": "railroad_service_type",
    "BRR02": "railroad_min_vertical_clearance",
    "BRR03": "railroad_min_horizontal_offset",
    "BN01": "navigable_waterway",
    "BN02": "navigable_min_vertical_clearance",
    "BN03": "movable_bridge_max_navigation_vertical_clearance",
    "BN04": "navigation_channel_width",
    "BN05": "navigation_channel_min_horizontal_clearance",
    "BN06": "substructure_navigation_protection",
}


FEATURES_FOREIGN_KEY = ForeignKeySpec(
    columns=("state_code", "bridge_number", "data_year"),
    referenced_table="bridges",
    referenced_columns=("state_code", "bridge_number", "data_year"),
    on_update="CASCADE",
    on_delete="CASCADE",
)

LEGACY_FEATURES_FOREIGN_KEY = ForeignKeySpec(
    columns=("state_code", "bridge_number", "data_year"),
    referenced_table="bridges",
    referenced_columns=("state_code", "bridge_number", "data_year"),
)

FEATURES_INDEX = IndexSpec(
    name="idx_features_bridge_year",
    columns=("state_code", "bridge_number", "data_year"),
)

FEATURES_IDENTITY_INDEX = IndexSpec(
    name="uq_features_snapshot_type",
    columns=("state_code", "bridge_number", "data_year", "feature_type"),
    unique=True,
)

FEATURES_V2_TABLE = TableSpec(
    name="features",
    columns=LEGACY_FEATURE_COLUMNS + FEATURE_TRANSITION_COLUMNS,
    foreign_keys=(FEATURES_FOREIGN_KEY,),
    indexes=(FEATURES_INDEX, FEATURES_IDENTITY_INDEX),
)


def _with_official_integer_affinity(
    columns: tuple[ColumnSpec, ...],
    integer_names: frozenset[str],
) -> tuple[ColumnSpec, ...]:
    return tuple(
        ColumnSpec(
            column.name,
            "INTEGER" if column.name in integer_names else column.declared_type,
            nullable=column.nullable,
            primary_key_position=column.primary_key_position,
            default_sql=column.default_sql,
        )
        for column in columns
    )


FEATURE_INTEGER_COLUMNS = frozenset(
    {
        "lanes_on_highway",                    # BH08
        "annual_average_daily_traffic",        # BH09
        "annual_average_daily_truck_traffic",  # BH10
        "bypass_detour_length",                # BH17
    }
)

FEATURES_TABLE = TableSpec(
    name="features",
    columns=_with_official_integer_affinity(
        FEATURES_V2_TABLE.columns,
        FEATURE_INTEGER_COLUMNS,
    ),
    foreign_keys=FEATURES_V2_TABLE.foreign_keys,
    indexes=FEATURES_V2_TABLE.indexes,
)


SNAPSHOT_FOREIGN_KEY = ForeignKeySpec(
    columns=("state_code", "bridge_number", "data_year"),
    referenced_table="bridges",
    referenced_columns=("state_code", "bridge_number", "data_year"),
    on_update="CASCADE",
    on_delete="CASCADE",
)


def _child_columns(*payload: ColumnSpec) -> tuple[ColumnSpec, ...]:
    return (
        ColumnSpec("id", "INTEGER", primary_key_position=1),
        ColumnSpec("state_code", "INTEGER", nullable=False),
        ColumnSpec("bridge_number", "TEXT", nullable=False),
        *payload,
        ColumnSpec("data_year", "INTEGER"),
    )


def _child_table(
    name: str,
    payload: tuple[ColumnSpec, ...],
    identity_columns: tuple[str, ...],
    *,
    identity_name: str,
    identity_sql_terms: tuple[str, ...] = (),
) -> TableSpec:
    return TableSpec(
        name=name,
        columns=_child_columns(*payload),
        foreign_keys=(SNAPSHOT_FOREIGN_KEY,),
        indexes=(
            IndexSpec(
                name=f"idx_{name}_bridge_year",
                columns=("state_code", "bridge_number", "data_year"),
            ),
            IndexSpec(
                name=identity_name,
                columns=identity_columns,
                unique=True,
                sql_terms=identity_sql_terms,
            ),
        ),
    )


ROUTES_TABLE = _child_table(
    "routes",
    (
        ColumnSpec("feature_type", "TEXT"),
        ColumnSpec("route_designation", "TEXT"),
        ColumnSpec("route_number", "TEXT"),
        ColumnSpec("route_direction", "TEXT"),
        ColumnSpec("route_type", "TEXT"),
        ColumnSpec("service_type", "TEXT"),
    ),
    ("state_code", "bridge_number", "data_year", "feature_type", "route_designation"),
    identity_name="uq_routes_snapshot_feature_designation",
)

SPAN_SETS_TABLE = _child_table(
    "span_sets",
    (
        ColumnSpec("span_configuration_designation", "TEXT"),
        ColumnSpec("number_of_spans", "INTEGER"),
        ColumnSpec("number_of_beam_lines", "INTEGER"),
        ColumnSpec("span_material", "TEXT"),
        ColumnSpec("span_continuity", "TEXT"),
        ColumnSpec("span_type", "TEXT"),
        ColumnSpec("span_protective_system", "TEXT"),
        ColumnSpec("deck_interaction", "TEXT"),
        ColumnSpec("deck_material_and_type", "TEXT"),
        ColumnSpec("wearing_surface", "TEXT"),
        ColumnSpec("deck_protective_system", "TEXT"),
        ColumnSpec("deck_reinforcing_protective_system", "TEXT"),
        ColumnSpec("deck_stay_in_place_forms", "TEXT"),
    ),
    ("state_code", "bridge_number", "data_year", "span_configuration_designation"),
    identity_name="uq_span_sets_snapshot_designation",
)

SUBSTRUCTURE_SETS_TABLE = _child_table(
    "substructure_sets",
    (
        ColumnSpec("substructure_configuration_designation", "TEXT"),
        ColumnSpec("number_of_substructure_units", "INTEGER"),
        ColumnSpec("substructure_material", "TEXT"),
        ColumnSpec("substructure_type", "TEXT"),
        ColumnSpec("substructure_protective_system", "TEXT"),
        ColumnSpec("foundation_type", "TEXT"),
        ColumnSpec("foundation_protective_system", "TEXT"),
    ),
    ("state_code", "bridge_number", "data_year", "substructure_configuration_designation"),
    identity_name="uq_substructure_sets_snapshot_designation",
)

ELEMENT_INTEGER_COLUMNS = frozenset(
    {
        "element_total_quantity",               # BE03
        "element_quantity_condition_state_one", # BCS01
        "element_quantity_condition_state_two", # BCS02
        "element_quantity_condition_state_three", # BCS03
        "element_quantity_condition_state_four", # BCS04
    }
)

ELEMENTS_TABLE = _child_table(
    "elements",
    (
        ColumnSpec("element_number", "TEXT"),
        ColumnSpec("element_parent_number", "TEXT"),
        ColumnSpec("element_total_quantity", "INTEGER"),
        ColumnSpec("element_quantity_condition_state_one", "INTEGER"),
        ColumnSpec("element_quantity_condition_state_two", "INTEGER"),
        ColumnSpec("element_quantity_condition_state_three", "INTEGER"),
        ColumnSpec("element_quantity_condition_state_four", "INTEGER"),
    ),
    ("state_code", "bridge_number", "data_year", "element_number", "element_parent_number"),
    identity_name="uq_elements_snapshot_element_parent",
)

INSPECTIONS_TABLE = _child_table(
    "inspections",
    (
        ColumnSpec("inspection_type", "TEXT"),
        ColumnSpec("inspection_begin_date", "TEXT"),
        ColumnSpec("inspection_completion_date", "TEXT"),
        ColumnSpec("nationally_certified_bridge_inspector", "TEXT"),
        ColumnSpec("inspection_interval", "INTEGER"),
        ColumnSpec("inspection_due_date", "TEXT"),
        ColumnSpec("risk_based_inspection_interval_method", "TEXT"),
        ColumnSpec("inspection_quality_control_date", "TEXT"),
        ColumnSpec("inspection_quality_assurance_date", "TEXT"),
        ColumnSpec("inspection_data_update_date", "TEXT"),
        ColumnSpec("inspection_note", "TEXT"),
        ColumnSpec("inspection_equipment", "TEXT"),
    ),
    ("state_code", "bridge_number", "data_year", "inspection_type", "inspection_begin_date"),
    identity_name="uq_inspections_snapshot_type_begin",
)

POSTING_EVALUATIONS_TABLE = _child_table(
    "posting_evaluations",
    (
        ColumnSpec("legal_load_configuration", "TEXT"),
        ColumnSpec("legal_load_rating_factor", "REAL"),
        ColumnSpec("posting_type", "TEXT"),
        ColumnSpec("posting_value", "TEXT"),
    ),
    ("state_code", "bridge_number", "data_year", "legal_load_configuration"),
    identity_name="uq_posting_evaluations_snapshot_configuration",
)

POSTING_STATUSES_TABLE = _child_table(
    "posting_statuses",
    (
        ColumnSpec("load_posting_status", "TEXT"),
        ColumnSpec("posting_status_change_date", "TEXT"),
    ),
    (),
    identity_name="uq_posting_statuses_snapshot_status_date",
    identity_sql_terms=(
        '"state_code"',
        '"bridge_number"',
        '"data_year"',
        '"load_posting_status"',
        'COALESCE("posting_status_change_date", \'\')',
    ),
)

WORKS_TABLE = _child_table(
    "works",
    (
        ColumnSpec("year_work_performed", "INTEGER"),
        ColumnSpec("work_performed", "TEXT"),
    ),
    ("state_code", "bridge_number", "data_year", "year_work_performed", "work_performed"),
    identity_name="uq_works_snapshot_year_work",
)

PROJECTION_CHILD_TABLES: tuple[TableSpec, ...] = (
    FEATURES_TABLE,
    ROUTES_TABLE,
    SPAN_SETS_TABLE,
    SUBSTRUCTURE_SETS_TABLE,
    ELEMENTS_TABLE,
    INSPECTIONS_TABLE,
    POSTING_EVALUATIONS_TABLE,
    POSTING_STATUSES_TABLE,
    WORKS_TABLE,
)


def _uuid_primary_key(name: str = "id") -> ColumnSpec:
    return ColumnSpec(name, "TEXT", nullable=False, primary_key_position=1)


def _workspace_foreign_key() -> ForeignKeySpec:
    return ForeignKeySpec(
        columns=("workspace_id",),
        referenced_table="workspaces",
        referenced_columns=("id",),
        on_update="RESTRICT",
        on_delete="RESTRICT",
    )


def _immutable_triggers(table_name: str) -> tuple[TriggerSpec, TriggerSpec]:
    label = table_name.replace("_", " ")
    return (
        TriggerSpec(
            name=f"trg_{table_name}_no_update",
            sql=(
                f'CREATE TRIGGER "trg_{table_name}_no_update" '
                f'BEFORE UPDATE ON "{table_name}" FOR EACH ROW BEGIN '
                f"SELECT RAISE(ABORT, '{label} rows are immutable'); END"
            ),
        ),
        TriggerSpec(
            name=f"trg_{table_name}_no_delete",
            sql=(
                f'CREATE TRIGGER "trg_{table_name}_no_delete" '
                f'BEFORE DELETE ON "{table_name}" FOR EACH ROW BEGIN '
                f"SELECT RAISE(ABORT, '{label} rows are immutable'); END"
            ),
        ),
    )


WORKSPACES_TABLE = TableSpec(
    name="workspaces",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("slug", "TEXT", nullable=False),
        ColumnSpec("display_name", "TEXT", nullable=False),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
        ColumnSpec("updated_at", "TEXT", nullable=False),
    ),
    indexes=(
        IndexSpec("uq_workspaces_slug", ("slug",), unique=True),
    ),
)

SOURCE_ARTIFACTS_TABLE = TableSpec(
    name="source_artifacts",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("sha256", "TEXT", nullable=False),
        ColumnSpec("byte_size", "INTEGER", nullable=False),
        ColumnSpec("original_filename", "TEXT"),
        ColumnSpec("media_type", "TEXT"),
        ColumnSpec("artifact_kind", "TEXT", nullable=False),
        ColumnSpec("storage_uri", "TEXT"),
        ColumnSpec("source_system", "TEXT"),
        ColumnSpec("created_by", "TEXT"),
        ColumnSpec("created_at", "TEXT", nullable=False),
        ColumnSpec("retention_until", "TEXT"),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_source_artifacts_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
        IndexSpec(
            "idx_source_artifacts_workspace_digest",
            ("workspace_id", "sha256", "byte_size"),
        ),
        IndexSpec("idx_source_artifacts_workspace", ("workspace_id",)),
    ),
    triggers=(
        TriggerSpec(
            name="trg_source_artifacts_digest_immutable",
            sql=(
                'CREATE TRIGGER "trg_source_artifacts_digest_immutable" '
                'BEFORE UPDATE OF "sha256", "byte_size" ON "source_artifacts" '
                "FOR EACH ROW WHEN NEW.sha256 IS NOT OLD.sha256 "
                "OR NEW.byte_size IS NOT OLD.byte_size BEGIN "
                "SELECT RAISE(ABORT, 'source artifact digest is immutable'); END"
            ),
        ),
    ),
)

SOURCE_RECORDS_TABLE = TableSpec(
    name="source_records",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("source_artifact_id", "TEXT", nullable=False),
        ColumnSpec("record_ordinal", "INTEGER", nullable=False),
        ColumnSpec("source_role", "TEXT", nullable=False),
        ColumnSpec("record_identity", "TEXT"),
        ColumnSpec("raw_record_json", "TEXT", nullable=False),
        ColumnSpec("raw_record_sha256", "TEXT", nullable=False),
        ColumnSpec("parse_status", "TEXT", nullable=False),
        ColumnSpec("parse_issues_json", "TEXT"),
        ColumnSpec("state_code", "INTEGER"),
        ColumnSpec("bridge_number", "TEXT"),
        ColumnSpec("data_year", "INTEGER"),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "source_artifact_id"),
            "source_artifacts",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_source_records_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
        IndexSpec(
            "uq_source_records_artifact_ordinal",
            ("workspace_id", "source_artifact_id", "record_ordinal"),
            unique=True,
        ),
        IndexSpec(
            "idx_source_records_bridge",
            ("workspace_id", "state_code", "bridge_number", "data_year"),
        ),
    ),
    triggers=_immutable_triggers("source_records"),
)

CONVERSION_BATCHES_TABLE = TableSpec(
    name="conversion_batches",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("profile_id", "TEXT", nullable=False),
        ColumnSpec("rule_bundle_id", "TEXT", nullable=False),
        ColumnSpec("rule_bundle_sha256", "TEXT", nullable=False),
        ColumnSpec("options_json", "TEXT", nullable=False),
        ColumnSpec("request_sha256", "TEXT", nullable=False),
        ColumnSpec("idempotency_key", "TEXT"),
        ColumnSpec("actor_id", "TEXT", nullable=False),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("counts_json", "TEXT", nullable=False),
        ColumnSpec("requested_at", "TEXT", nullable=False),
        ColumnSpec("started_at", "TEXT"),
        ColumnSpec("completed_at", "TEXT"),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_conversion_batches_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
        IndexSpec(
            "uq_conversion_batches_idempotency",
            ("workspace_id", "idempotency_key"),
            unique=True,
        ),
        IndexSpec(
            "idx_conversion_batches_workspace_status",
            ("workspace_id", "status", "requested_at"),
        ),
    ),
    triggers=_immutable_triggers("conversion_batches"),
)

ELIGIBILITY_DECISIONS_TABLE = TableSpec(
    name="eligibility_decisions",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("conversion_batch_id", "TEXT", nullable=False),
        ColumnSpec("source_record_id", "TEXT", nullable=False),
        ColumnSpec("decision", "TEXT", nullable=False),
        ColumnSpec("reason_code", "TEXT", nullable=False),
        ColumnSpec("reason_detail", "TEXT"),
        ColumnSpec("evidence_json", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "conversion_batch_id"),
            "conversion_batches",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "source_record_id"),
            "source_records",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_eligibility_decisions_batch_record",
            ("workspace_id", "conversion_batch_id", "source_record_id"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("eligibility_decisions"),
)

SNAPSHOT_REVISIONS_TABLE = TableSpec(
    name="snapshot_revisions",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("conversion_batch_id", "TEXT"),
        ColumnSpec("source_record_id", "TEXT"),
        ColumnSpec("state_code", "INTEGER", nullable=False),
        ColumnSpec("bridge_number", "TEXT", nullable=False),
        ColumnSpec("data_year", "INTEGER", nullable=False),
        ColumnSpec("revision_number", "INTEGER", nullable=False),
        ColumnSpec("lifecycle_state", "TEXT", nullable=False),
        ColumnSpec("canonical_json", "TEXT", nullable=False),
        ColumnSpec("canonical_sha256", "TEXT", nullable=False),
        ColumnSpec("parent_revision_id", "TEXT"),
        ColumnSpec("profile_id", "TEXT", nullable=False),
        ColumnSpec("rule_bundle_sha256", "TEXT", nullable=False),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "conversion_batch_id"),
            "conversion_batches",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "source_record_id"),
            "source_records",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "parent_revision_id"),
            "snapshot_revisions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_snapshot_revisions_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
        IndexSpec(
            "uq_snapshot_revisions_logical_revision",
            ("workspace_id", "state_code", "bridge_number", "data_year", "revision_number"),
            unique=True,
        ),
        IndexSpec(
            "idx_snapshot_revisions_logical_hash",
            ("workspace_id", "state_code", "bridge_number", "data_year", "canonical_sha256"),
        ),
        IndexSpec(
            "uq_snapshot_revisions_logical_id",
            ("workspace_id", "state_code", "bridge_number", "data_year", "id"),
            unique=True,
        ),
        IndexSpec(
            "idx_snapshot_revisions_logical_state",
            ("workspace_id", "state_code", "bridge_number", "data_year", "lifecycle_state"),
        ),
    ),
    triggers=_immutable_triggers("snapshot_revisions"),
)

SNAPSHOT_REVISION_SOURCES_TABLE = TableSpec(
    name="snapshot_revision_sources",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("snapshot_revision_id", "TEXT", nullable=False),
        ColumnSpec("source_record_id", "TEXT", nullable=False),
        ColumnSpec("source_ordinal", "INTEGER", nullable=False),
        ColumnSpec("source_role", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "snapshot_revision_id"),
            "snapshot_revisions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "source_record_id"),
            "source_records",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_snapshot_revision_sources_ordinal",
            ("workspace_id", "snapshot_revision_id", "source_ordinal"),
            unique=True,
        ),
        IndexSpec(
            "uq_snapshot_revision_sources_record",
            ("workspace_id", "snapshot_revision_id", "source_record_id"),
            unique=True,
        ),
        IndexSpec(
            "idx_snapshot_revision_sources_record",
            ("workspace_id", "source_record_id"),
        ),
    ),
    triggers=_immutable_triggers("snapshot_revision_sources"),
)

PUBLISHED_SNAPSHOTS_TABLE = TableSpec(
    name="published_snapshots",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("state_code", "INTEGER", nullable=False),
        ColumnSpec("bridge_number", "TEXT", nullable=False),
        ColumnSpec("data_year", "INTEGER", nullable=False),
        ColumnSpec("snapshot_revision_id", "TEXT", nullable=False),
        ColumnSpec("published_by", "TEXT", nullable=False),
        ColumnSpec("published_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            (
                "workspace_id",
                "state_code",
                "bridge_number",
                "data_year",
                "snapshot_revision_id",
            ),
            "snapshot_revisions",
            ("workspace_id", "state_code", "bridge_number", "data_year", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_published_snapshots_logical",
            ("workspace_id", "state_code", "bridge_number", "data_year"),
            unique=True,
        ),
        IndexSpec(
            "idx_published_snapshots_revision",
            ("workspace_id", "snapshot_revision_id"),
        ),
    ),
)

FIELD_LINEAGE_TABLE = TableSpec(
    name="field_lineage",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("snapshot_revision_id", "TEXT", nullable=False),
        ColumnSpec("target_pointer", "TEXT", nullable=False),
        ColumnSpec("lineage_ordinal", "INTEGER", nullable=False),
        ColumnSpec("source_record_id", "TEXT"),
        ColumnSpec("source_pointer", "TEXT"),
        ColumnSpec("rule_id", "TEXT"),
        ColumnSpec("resolution_status", "TEXT", nullable=False),
        ColumnSpec("value_json", "TEXT"),
        ColumnSpec("evidence_json", "TEXT"),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "snapshot_revision_id"),
            "snapshot_revisions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "source_record_id"),
            "source_records",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_field_lineage_revision_pointer_ordinal",
            ("workspace_id", "snapshot_revision_id", "target_pointer", "lineage_ordinal"),
            unique=True,
        ),
        IndexSpec(
            "idx_field_lineage_source_record",
            ("workspace_id", "source_record_id"),
        ),
    ),
    triggers=_immutable_triggers("field_lineage"),
)

VALIDATION_RUNS_TABLE = TableSpec(
    name="validation_runs",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("snapshot_revision_id", "TEXT", nullable=False),
        ColumnSpec("profile_id", "TEXT", nullable=False),
        ColumnSpec("rule_bundle_id", "TEXT", nullable=False),
        ColumnSpec("rule_bundle_sha256", "TEXT", nullable=False),
        ColumnSpec("validator_version", "TEXT", nullable=False),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("canonical_sha256", "TEXT", nullable=False),
        ColumnSpec("summary_json", "TEXT", nullable=False),
        ColumnSpec("started_at", "TEXT", nullable=False),
        ColumnSpec("completed_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "snapshot_revision_id"),
            "snapshot_revisions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_validation_runs_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
        IndexSpec(
            "idx_validation_runs_revision_profile",
            ("workspace_id", "snapshot_revision_id", "profile_id", "completed_at"),
        ),
    ),
    triggers=_immutable_triggers("validation_runs"),
)

VALIDATION_ISSUES_TABLE = TableSpec(
    name="validation_issues",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("validation_run_id", "TEXT", nullable=False),
        ColumnSpec("issue_ordinal", "INTEGER", nullable=False),
        ColumnSpec("severity", "TEXT", nullable=False),
        ColumnSpec("code", "TEXT", nullable=False),
        ColumnSpec("json_pointer", "TEXT", nullable=False),
        ColumnSpec("message", "TEXT", nullable=False),
        ColumnSpec("evidence_json", "TEXT"),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "validation_run_id"),
            "validation_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_validation_issues_run_ordinal",
            ("workspace_id", "validation_run_id", "issue_ordinal"),
            unique=True,
        ),
        IndexSpec(
            "idx_validation_issues_run_severity",
            ("workspace_id", "validation_run_id", "severity"),
        ),
    ),
    triggers=_immutable_triggers("validation_issues"),
)

MUTATION_AUDIT_TABLE = TableSpec(
    name="mutation_audit",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("actor_id", "TEXT", nullable=False),
        ColumnSpec("action", "TEXT", nullable=False),
        ColumnSpec("entity_type", "TEXT", nullable=False),
        ColumnSpec("entity_id", "TEXT", nullable=False),
        ColumnSpec("request_id", "TEXT", nullable=False),
        ColumnSpec("reason", "TEXT"),
        ColumnSpec("before_sha256", "TEXT"),
        ColumnSpec("after_sha256", "TEXT"),
        ColumnSpec("metadata_json", "TEXT", nullable=False),
        ColumnSpec("occurred_at", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "idx_mutation_audit_workspace_time",
            ("workspace_id", "occurred_at", "id"),
        ),
        IndexSpec(
            "idx_mutation_audit_request",
            ("workspace_id", "request_id"),
        ),
        IndexSpec(
            "idx_mutation_audit_entity",
            ("workspace_id", "entity_type", "entity_id", "occurred_at"),
        ),
    ),
    triggers=_immutable_triggers("mutation_audit"),
)

WORKSPACE_STATE_TABLE = TableSpec(
    name="workspace_state",
    columns=(
        _uuid_primary_key("workspace_id"),
        ColumnSpec("data_generation", "INTEGER", nullable=False, default_sql="0"),
        ColumnSpec("schema_generation", "INTEGER", nullable=False, default_sql="5"),
        ColumnSpec("updated_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        ForeignKeySpec(
            ("workspace_id",),
            "workspaces",
            ("id",),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
)

REVISION_STORE_TABLES: tuple[TableSpec, ...] = (
    WORKSPACES_TABLE,
    SOURCE_ARTIFACTS_TABLE,
    SOURCE_RECORDS_TABLE,
    CONVERSION_BATCHES_TABLE,
    ELIGIBILITY_DECISIONS_TABLE,
    SNAPSHOT_REVISIONS_TABLE,
    SNAPSHOT_REVISION_SOURCES_TABLE,
    PUBLISHED_SNAPSHOTS_TABLE,
    FIELD_LINEAGE_TABLE,
    VALIDATION_RUNS_TABLE,
    VALIDATION_ISSUES_TABLE,
    MUTATION_AUDIT_TABLE,
    WORKSPACE_STATE_TABLE,
)


REF_COUNTIES_TABLE = TableSpec(
    name="ref_counties",
    columns=(
        ColumnSpec("state_code", "INTEGER", nullable=False),
        ColumnSpec("county_code", "INTEGER", nullable=False),
        ColumnSpec("county_name", "TEXT", nullable=False),
    ),
    indexes=(
        IndexSpec(
            "uq_ref_counties_state_county",
            ("state_code", "county_code"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("ref_counties"),
)


QUERY_AUDIT_TABLE = TableSpec(
    name="query_audit",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("request_id", "TEXT", nullable=False),
        ColumnSpec("attempt", "INTEGER", nullable=False),
        ColumnSpec("event_type", "TEXT", nullable=False),
        ColumnSpec("actor_id", "TEXT", nullable=False),
        ColumnSpec("role", "TEXT", nullable=False),
        ColumnSpec("session_id", "TEXT"),
        ColumnSpec("surface", "TEXT", nullable=False),
        ColumnSpec("question_sha256", "TEXT", nullable=False),
        ColumnSpec("scope_json", "TEXT", nullable=False),
        ColumnSpec("ast_json", "TEXT"),
        ColumnSpec("compiled_sql", "TEXT"),
        ColumnSpec("parameters_json", "TEXT"),
        ColumnSpec("schema_generation", "INTEGER", nullable=False),
        ColumnSpec("data_generation", "INTEGER", nullable=False),
        ColumnSpec("catalog_sha256", "TEXT", nullable=False),
        ColumnSpec("model_id", "TEXT"),
        ColumnSpec("prompt_version", "TEXT"),
        ColumnSpec("duration_ms", "INTEGER"),
        ColumnSpec("progress_steps", "INTEGER"),
        ColumnSpec("termination_reason", "TEXT"),
        ColumnSpec("row_count", "INTEGER"),
        ColumnSpec("byte_count", "INTEGER"),
        ColumnSpec("outcome", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_query_audit_request_event",
            ("workspace_id", "request_id", "attempt", "event_type"),
            unique=True,
        ),
        IndexSpec(
            "idx_query_audit_workspace_time",
            ("workspace_id", "created_at", "id"),
        ),
        IndexSpec(
            "idx_query_audit_request",
            ("workspace_id", "request_id", "attempt"),
        ),
    ),
    triggers=_immutable_triggers("query_audit"),
)


QUERY_SUPPORT_TABLES: tuple[TableSpec, ...] = (
    REF_COUNTIES_TABLE,
    QUERY_AUDIT_TABLE,
)


QUERY_RESPONSE_AUDIT_TABLE = TableSpec(
    name="query_response_audit",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("request_id", "TEXT", nullable=False),
        ColumnSpec("actor_id", "TEXT", nullable=False),
        ColumnSpec("surface", "TEXT", nullable=False),
        ColumnSpec("plan_version", "TEXT", nullable=False),
        # The stored plan is canonical but privacy-minimized: filter values are
        # replaced by type/shape markers before this JSON or its digest exists.
        ColumnSpec("plan_json", "TEXT", nullable=False),
        ColumnSpec("plan_sha256", "TEXT", nullable=False),
        ColumnSpec("compiled_query_sha256", "TEXT", nullable=False),
        ColumnSpec("evidence_sha256", "TEXT", nullable=False),
        ColumnSpec("evidence_row_count", "INTEGER", nullable=False),
        ColumnSpec("evidence_column_count", "INTEGER", nullable=False),
        ColumnSpec("evidence_byte_count", "INTEGER", nullable=False),
        ColumnSpec("evidence_truncated", "INTEGER", nullable=False),
        ColumnSpec("answer_verification_version", "TEXT", nullable=False),
        ColumnSpec("answer_mode", "TEXT", nullable=False),
        ColumnSpec("verification_status", "TEXT", nullable=False),
        ColumnSpec("provider_call_count", "INTEGER", nullable=False),
        ColumnSpec("claim_count", "INTEGER", nullable=False),
        ColumnSpec("verified_claim_count", "INTEGER", nullable=False),
        ColumnSpec("rejected_claim_count", "INTEGER", nullable=False),
        ColumnSpec("fallback_count", "INTEGER", nullable=False),
        ColumnSpec("schema_generation", "INTEGER", nullable=False),
        ColumnSpec("data_generation", "INTEGER", nullable=False),
        ColumnSpec("catalog_version", "TEXT", nullable=False),
        ColumnSpec("catalog_sha256", "TEXT", nullable=False),
        ColumnSpec("dictionary_version", "TEXT", nullable=False),
        ColumnSpec("dictionary_sha256", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
        ColumnSpec("record_sha256", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_query_response_audit_request",
            ("workspace_id", "request_id"),
            unique=True,
        ),
        IndexSpec(
            "idx_query_response_audit_workspace_time",
            ("workspace_id", "created_at", "id"),
        ),
    ),
    triggers=_immutable_triggers("query_response_audit"),
)


QUERY_RESPONSE_AUDIT_TABLES: tuple[TableSpec, ...] = (
    QUERY_RESPONSE_AUDIT_TABLE,
)


CHAT_SESSIONS_TABLE = TableSpec(
    name="chat_sessions",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("owner_id", "TEXT", nullable=False),
        ColumnSpec("title", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
        ColumnSpec("updated_at", "TEXT", nullable=False),
        ColumnSpec("retention_expires_at", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_chat_sessions_private_identity",
            ("workspace_id", "owner_id", "id"),
            unique=True,
        ),
        IndexSpec(
            "idx_chat_sessions_owner_updated",
            ("workspace_id", "owner_id", "updated_at", "id"),
        ),
        IndexSpec(
            "idx_chat_sessions_retention",
            ("retention_expires_at", "id"),
        ),
    ),
)


CHAT_TURNS_TABLE = TableSpec(
    name="chat_turns",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("owner_id", "TEXT", nullable=False),
        ColumnSpec("session_id", "TEXT", nullable=False),
        ColumnSpec("role", "TEXT", nullable=False),
        ColumnSpec("content", "TEXT", nullable=False),
        ColumnSpec("request_id", "TEXT"),
        ColumnSpec("cited_revision", "TEXT"),
        ColumnSpec("provenance_status", "TEXT", nullable=False),
        ColumnSpec("evidence_receipt_sha256", "TEXT"),
        ColumnSpec("verification_receipt_sha256", "TEXT"),
        ColumnSpec("token_count", "INTEGER", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        ForeignKeySpec(
            ("workspace_id", "owner_id", "session_id"),
            "chat_sessions",
            ("workspace_id", "owner_id", "id"),
            on_update="RESTRICT",
            on_delete="CASCADE",
        ),
    ),
    indexes=(
        IndexSpec(
            "idx_chat_turns_session_time",
            ("workspace_id", "owner_id", "session_id", "created_at", "id"),
        ),
        IndexSpec(
            "idx_chat_turns_request",
            ("workspace_id", "owner_id", "request_id"),
        ),
    ),
)


CHAT_SESSION_TABLES: tuple[TableSpec, ...] = (
    CHAT_SESSIONS_TABLE,
    CHAT_TURNS_TABLE,
)


FORECAST_DATASETS_TABLE = TableSpec(
    name="forecast_datasets",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("source_receipt_sha256", "TEXT", nullable=False),
        ColumnSpec("through_year", "INTEGER", nullable=False),
        ColumnSpec("projection_specification_sha256", "TEXT", nullable=False),
        ColumnSpec("configuration_sha256", "TEXT", nullable=False),
        ColumnSpec("statistics_json", "TEXT", nullable=False),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec("uq_forecast_datasets_workspace_id", ("workspace_id", "id"), unique=True),
        IndexSpec(
            "uq_forecast_datasets_identity",
            (
                "workspace_id",
                "source_receipt_sha256",
                "through_year",
                "projection_specification_sha256",
                "configuration_sha256",
            ),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("forecast_datasets"),
)


FORECAST_MODEL_VERSIONS_TABLE = TableSpec(
    name="forecast_model_versions",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("forecast_dataset_id", "TEXT", nullable=False),
        ColumnSpec("model_id", "TEXT", nullable=False),
        ColumnSpec("engine_version", "TEXT", nullable=False),
        ColumnSpec("model_json", "TEXT", nullable=False),
        ColumnSpec("qualification", "TEXT", nullable=False),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "forecast_dataset_id"),
            "forecast_datasets",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec("uq_forecast_models_workspace_id", ("workspace_id", "id"), unique=True),
        IndexSpec("uq_forecast_models_model_id", ("workspace_id", "model_id"), unique=True),
        IndexSpec("idx_forecast_models_dataset", ("workspace_id", "forecast_dataset_id")),
    ),
    triggers=_immutable_triggers("forecast_model_versions"),
)


FORECAST_TREATMENT_MODELS_TABLE = TableSpec(
    name="forecast_treatment_models",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("forecast_model_version_id", "TEXT", nullable=False),
        ColumnSpec("treatment_id", "TEXT", nullable=False),
        ColumnSpec("component", "TEXT", nullable=False),
        ColumnSpec("applicability_json", "TEXT", nullable=False),
        ColumnSpec("effect_json", "TEXT", nullable=False),
        ColumnSpec("exposure", "INTEGER", nullable=False),
        ColumnSpec("overlap_evidence_json", "TEXT", nullable=False),
        ColumnSpec("fallback_evidence_json", "TEXT", nullable=False),
        ColumnSpec("qualification", "TEXT", nullable=False),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "forecast_model_version_id"),
            "forecast_model_versions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_forecast_treatment_models_identity",
            ("workspace_id", "forecast_model_version_id", "treatment_id", "component"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("forecast_treatment_models"),
)


FORECAST_EVALUATION_RUNS_TABLE = TableSpec(
    name="forecast_evaluation_runs",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("forecast_model_version_id", "TEXT", nullable=False),
        ColumnSpec("evaluation_receipt_id", "TEXT", nullable=False),
        ColumnSpec("release_policy_id", "TEXT", nullable=False),
        ColumnSpec("release_qualified", "INTEGER", nullable=False),
        ColumnSpec("summary_json", "TEXT", nullable=False),
        ColumnSpec("rows_json", "TEXT", nullable=False),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "forecast_model_version_id"),
            "forecast_model_versions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec("uq_forecast_evaluations_workspace_id", ("workspace_id", "id"), unique=True),
        IndexSpec(
            "uq_forecast_evaluations_receipt",
            ("workspace_id", "evaluation_receipt_id"),
            unique=True,
        ),
        IndexSpec(
            "idx_forecast_evaluations_model",
            ("workspace_id", "forecast_model_version_id"),
        ),
    ),
    triggers=_immutable_triggers("forecast_evaluation_runs"),
)


FORECAST_HINDCAST_PREDICTIONS_TABLE = TableSpec(
    name="forecast_hindcast_predictions",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("forecast_evaluation_run_id", "TEXT", nullable=False),
        ColumnSpec("row_ordinal", "INTEGER", nullable=False),
        ColumnSpec("bridge_number", "TEXT", nullable=False),
        ColumnSpec("component", "TEXT", nullable=False),
        ColumnSpec("origin_year", "INTEGER", nullable=False),
        ColumnSpec("origin_revision_id", "TEXT", nullable=False),
        ColumnSpec("target_year", "INTEGER", nullable=False),
        ColumnSpec("target_revision_id", "TEXT", nullable=False),
        ColumnSpec("lead_years", "INTEGER", nullable=False),
        ColumnSpec("model_id", "TEXT", nullable=False),
        ColumnSpec("predicted_distribution_json", "TEXT", nullable=False),
        ColumnSpec("predicted_rating", "REAL", nullable=False),
        ColumnSpec("observed_rating", "REAL", nullable=False),
        ColumnSpec("absolute_error", "REAL", nullable=False),
        ColumnSpec("signed_error", "REAL", nullable=False),
        ColumnSpec("persistence_rating", "REAL", nullable=False),
        ColumnSpec("persistence_absolute_error", "REAL", nullable=False),
        ColumnSpec("evidence_stratum", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "forecast_evaluation_run_id"),
            "forecast_evaluation_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_forecast_hindcast_rows_ordinal",
            ("workspace_id", "forecast_evaluation_run_id", "row_ordinal"),
            unique=True,
        ),
        IndexSpec(
            "idx_forecast_hindcast_target",
            (
                "workspace_id",
                "forecast_evaluation_run_id",
                "component",
                "lead_years",
                "target_year",
            ),
        ),
    ),
    triggers=_immutable_triggers("forecast_hindcast_predictions"),
)


FORECAST_RUNS_TABLE = TableSpec(
    name="forecast_runs",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("forecast_model_version_id", "TEXT", nullable=False),
        ColumnSpec("forecast_evaluation_run_id", "TEXT", nullable=False),
        ColumnSpec("snapshot_revision_id", "TEXT", nullable=False),
        ColumnSpec("state_code", "INTEGER", nullable=False),
        ColumnSpec("bridge_number", "TEXT", nullable=False),
        ColumnSpec("start_year", "INTEGER", nullable=False),
        ColumnSpec("horizon_years", "INTEGER", nullable=False),
        ColumnSpec("forecast_mode", "TEXT", nullable=False),
        ColumnSpec("configuration_json", "TEXT", nullable=False),
        ColumnSpec("idempotency_key", "TEXT", nullable=False),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "forecast_model_version_id"),
            "forecast_model_versions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "forecast_evaluation_run_id"),
            "forecast_evaluation_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "snapshot_revision_id"),
            "snapshot_revisions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec("uq_forecast_runs_workspace_id", ("workspace_id", "id"), unique=True),
        IndexSpec(
            "uq_forecast_runs_idempotency",
            ("workspace_id", "idempotency_key"),
            unique=True,
        ),
        IndexSpec(
            "idx_forecast_runs_bridge",
            ("workspace_id", "state_code", "bridge_number", "start_year", "created_at"),
        ),
    ),
    triggers=_immutable_triggers("forecast_runs"),
)


FORECAST_PREDICTIONS_TABLE = TableSpec(
    name="forecast_predictions",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("forecast_run_id", "TEXT", nullable=False),
        ColumnSpec("prediction_ordinal", "INTEGER", nullable=False),
        ColumnSpec("forecast_year", "INTEGER", nullable=False),
        ColumnSpec("component", "TEXT", nullable=False),
        ColumnSpec("action_id", "TEXT"),
        ColumnSpec("pre_action_distribution_json", "TEXT", nullable=False),
        ColumnSpec("post_action_distribution_json", "TEXT", nullable=False),
        ColumnSpec("expected_rating", "REAL", nullable=False),
        ColumnSpec("dispersion_json", "TEXT", nullable=False),
        ColumnSpec("quantiles_json", "TEXT", nullable=False),
        ColumnSpec("fallback_level", "TEXT", nullable=False),
        ColumnSpec("fallback_evidence_json", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "forecast_run_id"),
            "forecast_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_forecast_predictions_ordinal",
            ("workspace_id", "forecast_run_id", "prediction_ordinal"),
            unique=True,
        ),
        IndexSpec(
            "idx_forecast_predictions_year_component",
            ("workspace_id", "forecast_run_id", "forecast_year", "component"),
        ),
    ),
    triggers=_immutable_triggers("forecast_predictions"),
)


FORECAST_RUN_ISSUES_TABLE = TableSpec(
    name="forecast_run_issues",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("forecast_run_id", "TEXT", nullable=False),
        ColumnSpec("issue_ordinal", "INTEGER", nullable=False),
        ColumnSpec("severity", "TEXT", nullable=False),
        ColumnSpec("code", "TEXT", nullable=False),
        ColumnSpec("message", "TEXT", nullable=False),
        ColumnSpec("evidence_json", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "forecast_run_id"),
            "forecast_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_forecast_run_issues_ordinal",
            ("workspace_id", "forecast_run_id", "issue_ordinal"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("forecast_run_issues"),
)


FORECAST_ANALYTICS_TABLES: tuple[TableSpec, ...] = (
    FORECAST_DATASETS_TABLE,
    FORECAST_MODEL_VERSIONS_TABLE,
    FORECAST_TREATMENT_MODELS_TABLE,
    FORECAST_EVALUATION_RUNS_TABLE,
    FORECAST_HINDCAST_PREDICTIONS_TABLE,
    FORECAST_RUNS_TABLE,
    FORECAST_PREDICTIONS_TABLE,
    FORECAST_RUN_ISSUES_TABLE,
)


KPI_MEASURE_DEFINITIONS_TABLE = TableSpec(
    name="kpi_measure_definitions",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("measure_id", "TEXT", nullable=False),
        ColumnSpec("definition_version", "TEXT", nullable=False),
        ColumnSpec("definition_json", "TEXT", nullable=False),
        ColumnSpec("definition_sha256", "TEXT", nullable=False),
        ColumnSpec("approval_status", "TEXT", nullable=False),
        ColumnSpec("approved_by", "TEXT"),
        ColumnSpec("approved_at", "TEXT"),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_kpi_measure_definitions_identity",
            ("workspace_id", "measure_id", "definition_version"),
            unique=True,
        ),
        IndexSpec(
            "uq_kpi_measure_definitions_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("kpi_measure_definitions"),
)


KPI_REPORTING_PERIODS_TABLE = TableSpec(
    name="kpi_reporting_periods",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("period_type", "TEXT", nullable=False),
        ColumnSpec("period_year", "INTEGER", nullable=False),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("created_by", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_kpi_reporting_periods_identity",
            ("workspace_id", "period_type", "period_year"),
            unique=True,
        ),
        IndexSpec(
            "uq_kpi_reporting_periods_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("kpi_reporting_periods"),
)


KPI_PROGRAM_SOURCES_TABLE = TableSpec(
    name="kpi_program_sources",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("source_artifact_id", "TEXT"),
        ColumnSpec("source_type", "TEXT", nullable=False),
        ColumnSpec("source_locator", "TEXT", nullable=False),
        ColumnSpec("import_sha256", "TEXT", nullable=False),
        ColumnSpec("approval_status", "TEXT", nullable=False),
        ColumnSpec("actor_id", "TEXT", nullable=False),
        ColumnSpec("reviewer_id", "TEXT"),
        ColumnSpec("reviewed_at", "TEXT"),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "source_artifact_id"),
            "source_artifacts",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_kpi_program_sources_receipt",
            ("workspace_id", "import_sha256"),
            unique=True,
        ),
        IndexSpec(
            "uq_kpi_program_sources_workspace_id",
            ("workspace_id", "id"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("kpi_program_sources"),
)


KPI_PROGRAM_APPROVALS_TABLE = TableSpec(
    name="kpi_program_approvals",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("source_id", "TEXT", nullable=False),
        ColumnSpec("decision", "TEXT", nullable=False),
        ColumnSpec("reviewer_id", "TEXT", nullable=False),
        ColumnSpec("reviewed_at", "TEXT", nullable=False),
        ColumnSpec("review_note", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "source_id"),
            "kpi_program_sources",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_kpi_program_approvals_source",
            ("workspace_id", "source_id"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("kpi_program_approvals"),
)


KPI_PROGRAM_VALUES_TABLE = TableSpec(
    name="kpi_program_values",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("source_id", "TEXT", nullable=False),
        ColumnSpec("measure_definition_id", "TEXT", nullable=False),
        ColumnSpec("reporting_period_id", "TEXT", nullable=False),
        ColumnSpec("value_kind", "TEXT", nullable=False),
        ColumnSpec("value_decimal", "TEXT", nullable=False),
        ColumnSpec("unit", "TEXT", nullable=False),
        ColumnSpec("organization", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "source_id"),
            "kpi_program_sources",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "measure_definition_id"),
            "kpi_measure_definitions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "reporting_period_id"),
            "kpi_reporting_periods",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_kpi_program_values_identity",
            (
                "workspace_id",
                "source_id",
                "measure_definition_id",
                "reporting_period_id",
                "value_kind",
                "organization",
            ),
            unique=True,
        ),
        IndexSpec(
            "idx_kpi_program_values_period",
            ("workspace_id", "reporting_period_id", "measure_definition_id"),
        ),
    ),
    triggers=_immutable_triggers("kpi_program_values"),
)


KPI_PROGRAM_ISSUES_TABLE = TableSpec(
    name="kpi_program_issues",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("source_id", "TEXT", nullable=False),
        ColumnSpec("issue_ordinal", "INTEGER", nullable=False),
        ColumnSpec("severity", "TEXT", nullable=False),
        ColumnSpec("code", "TEXT", nullable=False),
        ColumnSpec("message", "TEXT", nullable=False),
        ColumnSpec("source_locator", "TEXT"),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "source_id"),
            "kpi_program_sources",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_kpi_program_issues_ordinal",
            ("workspace_id", "source_id", "issue_ordinal"),
            unique=True,
        ),
    ),
    triggers=_immutable_triggers("kpi_program_issues"),
)


KPI_PROGRAM_TABLES: tuple[TableSpec, ...] = (
    KPI_MEASURE_DEFINITIONS_TABLE,
    KPI_REPORTING_PERIODS_TABLE,
    KPI_PROGRAM_SOURCES_TABLE,
    KPI_PROGRAM_APPROVALS_TABLE,
    KPI_PROGRAM_VALUES_TABLE,
    KPI_PROGRAM_ISSUES_TABLE,
)


DEPLOYMENT_BINDINGS_TABLE = TableSpec(
    name="deployment_bindings",
    columns=(
        _uuid_primary_key("deployment_id"),
        ColumnSpec("customer_slug", "TEXT", nullable=False),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("marker_sha256", "TEXT", nullable=False),
        ColumnSpec("bound_at", "TEXT", nullable=False),
    ),
    foreign_keys=(_workspace_foreign_key(),),
    indexes=(
        IndexSpec(
            "uq_deployment_bindings_workspace",
            ("workspace_id",),
            unique=True,
        ),
    ),
    triggers=(
        *_immutable_triggers("deployment_bindings"),
        TriggerSpec(
            name="trg_deployment_bindings_single_row",
            sql=(
                'CREATE TRIGGER "trg_deployment_bindings_single_row" '
                'BEFORE INSERT ON "deployment_bindings" FOR EACH ROW '
                'WHEN EXISTS (SELECT 1 FROM "deployment_bindings") BEGIN '
                "SELECT RAISE(ABORT, 'deployment binding already exists'); END"
            ),
        ),
    ),
)


DEPLOYMENT_IDENTITY_TABLES: tuple[TableSpec, ...] = (
    DEPLOYMENT_BINDINGS_TABLE,
)


POINT_CLOUD_RUNS_TABLE = TableSpec(
    name="point_cloud_runs",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("job_id", "TEXT", nullable=False),
        ColumnSpec("idempotency_key", "TEXT", nullable=False),
        ColumnSpec("request_sha256", "TEXT", nullable=False),
        ColumnSpec("actor_id", "TEXT", nullable=False),
        ColumnSpec("state_code", "INTEGER", nullable=False),
        ColumnSpec("bridge_number", "TEXT", nullable=False),
        ColumnSpec("data_year", "INTEGER", nullable=False),
        ColumnSpec("snapshot_revision_id", "TEXT", nullable=False),
        ColumnSpec("canonical_sha256", "TEXT", nullable=False),
        ColumnSpec("source_sha256", "TEXT", nullable=False),
        ColumnSpec("source_byte_size", "INTEGER", nullable=False),
        ColumnSpec("source_receipt_sha256", "TEXT", nullable=False),
        ColumnSpec("analysis_profile_sha256", "TEXT", nullable=False),
        ColumnSpec("knowledge_pack_sha256", "TEXT", nullable=False),
        ColumnSpec("report_source_set_sha256", "TEXT"),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("phase", "TEXT", nullable=False),
        ColumnSpec("progress_json", "TEXT", nullable=False),
        ColumnSpec("result_sha256", "TEXT"),
        ColumnSpec("error_code", "TEXT"),
        ColumnSpec("created_at", "TEXT", nullable=False),
        ColumnSpec("started_at", "TEXT"),
        ColumnSpec("completed_at", "TEXT"),
        ColumnSpec("updated_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "snapshot_revision_id"),
            "snapshot_revisions",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec("uq_point_cloud_runs_workspace_id", ("workspace_id", "id"), unique=True),
        IndexSpec(
            "uq_point_cloud_runs_idempotency",
            ("workspace_id", "actor_id", "idempotency_key"),
            unique=True,
        ),
        IndexSpec(
            "idx_point_cloud_runs_bridge",
            ("workspace_id", "state_code", "bridge_number", "data_year", "created_at"),
        ),
        IndexSpec("idx_point_cloud_runs_status", ("workspace_id", "status", "updated_at")),
    ),
    triggers=(
        TriggerSpec(
            name="trg_point_cloud_runs_identity_immutable",
            sql=(
                'CREATE TRIGGER "trg_point_cloud_runs_identity_immutable" BEFORE UPDATE OF '
                '"workspace_id","job_id","idempotency_key","request_sha256","actor_id",'
                '"state_code","bridge_number","data_year","snapshot_revision_id",'
                '"canonical_sha256","source_sha256","source_byte_size",'
                '"source_receipt_sha256","analysis_profile_sha256",'
                '"knowledge_pack_sha256","report_source_set_sha256","created_at" '
                'ON "point_cloud_runs" FOR EACH ROW BEGIN '
                "SELECT RAISE(ABORT, 'point cloud run identity is immutable'); END"
            ),
        ),
        TriggerSpec(
            name="trg_point_cloud_runs_no_delete",
            sql=(
                'CREATE TRIGGER "trg_point_cloud_runs_no_delete" BEFORE DELETE ON '
                '"point_cloud_runs" FOR EACH ROW BEGIN '
                "SELECT RAISE(ABORT, 'point cloud run rows are immutable evidence'); END"
            ),
        ),
    ),
)


def _point_cloud_evidence_table(
    name: str,
    payload_columns: tuple[ColumnSpec, ...],
    ordinal_column: str,
) -> TableSpec:
    return TableSpec(
        name=name,
        columns=(
            _uuid_primary_key(),
            ColumnSpec("workspace_id", "TEXT", nullable=False),
            ColumnSpec("run_id", "TEXT", nullable=False),
            *payload_columns,
            ColumnSpec("created_at", "TEXT", nullable=False),
        ),
        foreign_keys=(
            _workspace_foreign_key(),
            ForeignKeySpec(
                ("workspace_id", "run_id"),
                "point_cloud_runs",
                ("workspace_id", "id"),
                on_update="RESTRICT",
                on_delete="RESTRICT",
            ),
        ),
        indexes=(
            IndexSpec(
                f"uq_{name}_ordinal",
                ("workspace_id", "run_id", ordinal_column),
                unique=True,
            ),
            IndexSpec(f"idx_{name}_run", ("workspace_id", "run_id")),
        ),
        triggers=_immutable_triggers(name),
    )


POINT_CLOUD_RUN_SOURCES_TABLE = _point_cloud_evidence_table(
    "point_cloud_run_sources",
    (
        ColumnSpec("source_ordinal", "INTEGER", nullable=False),
        ColumnSpec("source_kind", "TEXT", nullable=False),
        ColumnSpec("source_id", "TEXT", nullable=False),
        ColumnSpec("sha256", "TEXT", nullable=False),
        ColumnSpec("byte_size", "INTEGER", nullable=False),
        ColumnSpec("receipt_json", "TEXT", nullable=False),
        ColumnSpec("receipt_sha256", "TEXT", nullable=False),
    ),
    "source_ordinal",
)

POINT_CLOUD_VLM_CALLS_TABLE = _point_cloud_evidence_table(
    "point_cloud_vlm_calls",
    (
        ColumnSpec("call_ordinal", "INTEGER", nullable=False),
        ColumnSpec("task", "TEXT", nullable=False),
        ColumnSpec("provider", "TEXT", nullable=False),
        ColumnSpec("model", "TEXT", nullable=False),
        ColumnSpec("prompt_sha256", "TEXT", nullable=False),
        ColumnSpec("schema_sha256", "TEXT", nullable=False),
        ColumnSpec("receipt_json", "TEXT", nullable=False),
        ColumnSpec("receipt_sha256", "TEXT", nullable=False),
    ),
    "call_ordinal",
)

POINT_CLOUD_COMPONENTS_TABLE = _point_cloud_evidence_table(
    "point_cloud_components",
    (
        ColumnSpec("component_ordinal", "INTEGER", nullable=False),
        ColumnSpec("candidate_id", "TEXT", nullable=False),
        ColumnSpec("proposed_class", "TEXT", nullable=False),
        ColumnSpec("final_class", "TEXT", nullable=False),
        ColumnSpec("disposition", "TEXT", nullable=False),
        ColumnSpec("component_json", "TEXT", nullable=False),
        ColumnSpec("component_sha256", "TEXT", nullable=False),
    ),
    "component_ordinal",
)

POINT_CLOUD_MEASUREMENTS_TABLE = _point_cloud_evidence_table(
    "point_cloud_measurement_candidates",
    (
        ColumnSpec("candidate_ordinal", "INTEGER", nullable=False),
        ColumnSpec("candidate_id", "TEXT", nullable=False),
        ColumnSpec("feature_id", "TEXT"),
        ColumnSpec("product", "TEXT", nullable=False),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("candidate_json", "TEXT", nullable=False),
        ColumnSpec("candidate_sha256", "TEXT", nullable=False),
    ),
    "candidate_ordinal",
)

POINT_CLOUD_REVIEWS_TABLE = TableSpec(
    name="point_cloud_reviews",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("run_id", "TEXT", nullable=False),
        ColumnSpec("review_ordinal", "INTEGER", nullable=False),
        ColumnSpec("decision", "TEXT", nullable=False),
        ColumnSpec("reason", "TEXT", nullable=False),
        ColumnSpec("corrections_json", "TEXT", nullable=False),
        ColumnSpec("before_sha256", "TEXT", nullable=False),
        ColumnSpec("after_sha256", "TEXT", nullable=False),
        ColumnSpec("reviewer_id", "TEXT", nullable=False),
        ColumnSpec("created_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "run_id"),
            "point_cloud_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec("uq_point_cloud_reviews_ordinal", ("workspace_id", "run_id", "review_ordinal"), unique=True),
        IndexSpec("uq_point_cloud_reviews_run_id", ("workspace_id", "run_id", "id"), unique=True),
    ),
    triggers=_immutable_triggers("point_cloud_reviews"),
)

PUBLISHED_POINT_CLOUD_RESULTS_TABLE = TableSpec(
    name="published_point_cloud_results",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("state_code", "INTEGER", nullable=False),
        ColumnSpec("bridge_number", "TEXT", nullable=False),
        ColumnSpec("data_year", "INTEGER", nullable=False),
        ColumnSpec("source_sha256", "TEXT", nullable=False),
        ColumnSpec("analysis_profile_sha256", "TEXT", nullable=False),
        ColumnSpec("run_id", "TEXT", nullable=False),
        ColumnSpec("review_id", "TEXT", nullable=False),
        ColumnSpec("published_by", "TEXT", nullable=False),
        ColumnSpec("published_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "run_id"),
            "point_cloud_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
        ForeignKeySpec(
            ("workspace_id", "run_id", "review_id"),
            "point_cloud_reviews",
            ("workspace_id", "run_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec(
            "uq_published_point_cloud_result",
            ("workspace_id", "state_code", "bridge_number", "data_year", "source_sha256", "analysis_profile_sha256"),
            unique=True,
        ),
        IndexSpec("idx_published_point_cloud_run", ("workspace_id", "run_id")),
    ),
    triggers=(
        TriggerSpec(
            name="trg_published_point_cloud_identity_immutable",
            sql=(
                'CREATE TRIGGER "trg_published_point_cloud_identity_immutable" BEFORE UPDATE OF '
                '"workspace_id","state_code","bridge_number","data_year","source_sha256",'
                '"analysis_profile_sha256" ON "published_point_cloud_results" '
                'FOR EACH ROW BEGIN SELECT RAISE(ABORT, '
                "'published point cloud identity is immutable'); END"
            ),
        ),
    ),
)

POINT_CLOUD_RETENTION_OUTBOX_TABLE = TableSpec(
    name="point_cloud_retention_outbox",
    columns=(
        _uuid_primary_key(),
        ColumnSpec("workspace_id", "TEXT", nullable=False),
        ColumnSpec("run_id", "TEXT", nullable=False),
        ColumnSpec("source_sha256", "TEXT", nullable=False),
        ColumnSpec("status", "TEXT", nullable=False),
        ColumnSpec("attempts", "INTEGER", nullable=False, default_sql="0"),
        ColumnSpec("error_code", "TEXT"),
        ColumnSpec("created_at", "TEXT", nullable=False),
        ColumnSpec("updated_at", "TEXT", nullable=False),
    ),
    foreign_keys=(
        _workspace_foreign_key(),
        ForeignKeySpec(
            ("workspace_id", "run_id"),
            "point_cloud_runs",
            ("workspace_id", "id"),
            on_update="RESTRICT",
            on_delete="RESTRICT",
        ),
    ),
    indexes=(
        IndexSpec("uq_point_cloud_retention_run", ("workspace_id", "run_id"), unique=True),
        IndexSpec("idx_point_cloud_retention_status", ("workspace_id", "status", "updated_at")),
    ),
    triggers=(
        TriggerSpec(
            name="trg_point_cloud_retention_identity_immutable",
            sql=(
                'CREATE TRIGGER "trg_point_cloud_retention_identity_immutable" BEFORE UPDATE OF '
                '"workspace_id","run_id","source_sha256","created_at" ON '
                '"point_cloud_retention_outbox" FOR EACH ROW BEGIN '
                "SELECT RAISE(ABORT, 'point cloud retention identity is immutable'); END"
            ),
        ),
    ),
)

POINT_CLOUD_RUN_TABLES: tuple[TableSpec, ...] = (
    POINT_CLOUD_RUNS_TABLE,
    POINT_CLOUD_RUN_SOURCES_TABLE,
    POINT_CLOUD_VLM_CALLS_TABLE,
    POINT_CLOUD_COMPONENTS_TABLE,
    POINT_CLOUD_MEASUREMENTS_TABLE,
    POINT_CLOUD_REVIEWS_TABLE,
    PUBLISHED_POINT_CLOUD_RESULTS_TABLE,
    POINT_CLOUD_RETENTION_OUTBOX_TABLE,
)


_AVAILABLE_BRIDGE_COLUMNS = """
    available.workspace_id,
    available.snapshot_revision_id,
    b.state_code,
    b.bridge_number,
    b.bridge_name,
    b.data_year,
    available.published_at,
    b.county_code,
    b.bridge_location,
    b.latitude,
    b.longitude,
    b.owner,
    b.maintenance_responsibility,
    b.year_built,
    b.total_bridge_length,
    b.maximum_span_length,
    b.bridge_width_out_to_out,
    b.calculated_deck_area AS reported_calculated_deck_area,
    CASE
      WHEN b.calculated_deck_area>0 THEN b.calculated_deck_area
      WHEN b.total_bridge_length>0 AND b.bridge_width_out_to_out>0
        THEN ROUND(b.total_bridge_length*b.bridge_width_out_to_out, 1)
      WHEN b.total_bridge_length>0 AND b.approach_roadway_width>0
        THEN ROUND(b.total_bridge_length*b.approach_roadway_width, 1)
      ELSE NULL
    END AS calculated_deck_area,
    CASE
      WHEN b.calculated_deck_area>0 THEN 'reported_bg16'
      WHEN b.total_bridge_length>0 AND b.bridge_width_out_to_out>0
        THEN 'calculated_length_times_out_to_out_width'
      WHEN b.total_bridge_length>0 AND b.approach_roadway_width>0
        THEN 'calculated_length_times_approach_width'
      ELSE 'unavailable'
    END AS calculated_deck_area_method,
    b.nhs_designation,
    b.deck_condition_rating,
    b.superstructure_condition_rating,
    b.substructure_condition_rating,
    b.culvert_condition_rating,
    b.lowest_condition_rating_code
""".strip()

PUBLISHED_BRIDGES_VIEW = ViewSpec(
    "v_published_bridge_snapshots",
    "CREATE VIEW v_published_bridge_snapshots AS "
    "WITH available AS (SELECT ws.workspace_id, "
    "'loaded-inventory:' || CAST(b.state_code AS TEXT) || ':' || "
    "b.bridge_number || ':' || CAST(b.data_year AS TEXT) AS snapshot_revision_id, "
    "b.state_code, b.bridge_number, b.data_year, NULL AS published_at "
    "FROM workspace_state ws CROSS JOIN bridges b) "
    "SELECT " + _AVAILABLE_BRIDGE_COLUMNS + " FROM available JOIN bridges b "
    "ON b.state_code=available.state_code "
    "AND b.bridge_number=available.bridge_number "
    "AND b.data_year=available.data_year",
)

CURRENT_BRIDGES_VIEW = ViewSpec(
    "v_current_bridge_snapshots",
    "CREATE VIEW v_current_bridge_snapshots AS "
    "WITH loaded_latest AS ("
    "SELECT state_code, MAX(data_year) AS data_year FROM bridges GROUP BY state_code), "
    "available AS ("
    "SELECT ws.workspace_id, "
    "'loaded-inventory:' || CAST(b.state_code AS TEXT) || ':' || "
    "b.bridge_number || ':' || CAST(b.data_year AS TEXT) AS snapshot_revision_id, "
    "b.state_code, b.bridge_number, b.data_year, NULL AS published_at "
    "FROM workspace_state ws CROSS JOIN loaded_latest latest "
    "JOIN bridges b ON b.state_code=latest.state_code "
    "AND b.data_year=latest.data_year) "
    "SELECT " + _AVAILABLE_BRIDGE_COLUMNS + " FROM available JOIN bridges b "
    "ON b.state_code=available.state_code "
    "AND b.bridge_number=available.bridge_number "
    "AND b.data_year=available.data_year",
)

BRIDGE_FEATURES_VIEW = ViewSpec(
    "v_bridge_features",
    """CREATE VIEW v_bridge_features AS SELECT
    p.workspace_id, p.snapshot_revision_id,
    f.state_code, f.bridge_number, f.data_year,
    f.feature_type, f.feature_location, f.feature_name,
    f.functional_classification, f.urban_code, f.nhs_designation,
    f.national_highway_freight_network, f.strahnet_designation,
    f.lrs_route_id, f.lrs_mile_point, f.lanes_on_highway,
    f.annual_average_daily_traffic, f.annual_average_daily_truck_traffic,
    f.year_of_annual_average_daily_traffic,
    f.highway_max_usable_vertical_clearance,
    f.highway_min_vertical_clearance,
    f.highway_min_horizontal_clearance_left,
    f.highway_min_horizontal_clearance_right,
    f.highway_max_usable_surface_width, f.bypass_detour_length,
    f.crossing_bridge_number, f.railroad_service_type,
    f.railroad_min_vertical_clearance, f.railroad_min_horizontal_offset,
    f.navigable_waterway, f.navigable_min_vertical_clearance,
    f.movable_bridge_max_navigation_vertical_clearance,
    f.navigation_channel_width, f.navigation_channel_min_horizontal_clearance,
    f.substructure_navigation_protection
    FROM v_published_bridge_snapshots p JOIN features f
      ON f.state_code=p.state_code AND f.bridge_number=p.bridge_number
     AND f.data_year=p.data_year""",
)

CARRIED_HIGHWAY_FEATURES_VIEW = ViewSpec(
    "v_carried_highway_features",
    "CREATE VIEW v_carried_highway_features AS SELECT * FROM v_bridge_features "
    "WHERE feature_type='H01' AND feature_location='C'",
)

BRIDGE_ROUTES_VIEW = ViewSpec(
    "v_bridge_routes",
    """CREATE VIEW v_bridge_routes AS SELECT
    p.workspace_id, p.snapshot_revision_id,
    r.state_code, r.bridge_number, r.data_year, r.feature_type,
    r.route_designation, r.route_number, r.route_direction,
    r.route_type, r.service_type
    FROM v_published_bridge_snapshots p JOIN routes r
      ON r.state_code=p.state_code AND r.bridge_number=p.bridge_number
     AND r.data_year=p.data_year""",
)

BRIDGE_CONDITION_VIEW = ViewSpec(
    "v_bridge_condition",
    """CREATE VIEW v_bridge_condition AS
    WITH ratings AS (
      SELECT p.*,
        CASE WHEN p.deck_condition_rating BETWEEN 0 AND 9
                   OR p.superstructure_condition_rating BETWEEN 0 AND 9
                   OR p.substructure_condition_rating BETWEEN 0 AND 9
                   OR p.culvert_condition_rating BETWEEN 0 AND 9
             THEN min(
               CASE WHEN p.deck_condition_rating BETWEEN 0 AND 9
                    THEN p.deck_condition_rating ELSE 99 END,
               CASE WHEN p.superstructure_condition_rating BETWEEN 0 AND 9
                    THEN p.superstructure_condition_rating ELSE 99 END,
               CASE WHEN p.substructure_condition_rating BETWEEN 0 AND 9
                    THEN p.substructure_condition_rating ELSE 99 END,
               CASE WHEN p.culvert_condition_rating BETWEEN 0 AND 9
                    THEN p.culvert_condition_rating ELSE 99 END)
             ELSE NULL END AS derived_applicable_rating,
        CASE WHEN (p.deck_condition_rating IS NOT NULL
                         AND NOT (p.deck_condition_rating BETWEEN 0 AND 9)
                         AND upper(trim(CAST(p.deck_condition_rating AS TEXT)))<>'N')
                    OR (p.superstructure_condition_rating IS NOT NULL
                         AND NOT (p.superstructure_condition_rating BETWEEN 0 AND 9)
                         AND upper(trim(CAST(p.superstructure_condition_rating AS TEXT)))<>'N')
                    OR (p.substructure_condition_rating IS NOT NULL
                         AND NOT (p.substructure_condition_rating BETWEEN 0 AND 9)
                         AND upper(trim(CAST(p.substructure_condition_rating AS TEXT)))<>'N')
                    OR (p.culvert_condition_rating IS NOT NULL
                         AND NOT (p.culvert_condition_rating BETWEEN 0 AND 9)
                         AND upper(trim(CAST(p.culvert_condition_rating AS TEXT)))<>'N')
             THEN 1 ELSE 0 END AS invalid_component_rating,
        CASE WHEN p.deck_condition_rating BETWEEN 0 AND 9
                   OR p.superstructure_condition_rating BETWEEN 0 AND 9
                   OR p.substructure_condition_rating BETWEEN 0 AND 9
             THEN 1 ELSE 0 END AS has_primary_rating,
        CASE WHEN p.culvert_condition_rating BETWEEN 0 AND 9
             THEN 1 ELSE 0 END AS has_culvert_rating
      FROM v_published_bridge_snapshots p
    ), controlling AS (
      SELECT ratings.*,
        CASE WHEN derived_applicable_rating IS NOT NULL THEN derived_applicable_rating
             WHEN lowest_condition_rating_code BETWEEN 0 AND 9
             THEN lowest_condition_rating_code ELSE NULL END AS controlling_rating,
        CASE WHEN derived_applicable_rating IS NOT NULL
                  AND has_primary_rating=1 AND has_culvert_rating=1
             THEN 'derived_applicable_min'
             WHEN derived_applicable_rating IS NOT NULL AND has_primary_rating=1
             THEN 'derived_primary_min'
             WHEN derived_applicable_rating IS NOT NULL AND has_culvert_rating=1
             THEN 'culvert_only'
             WHEN lowest_condition_rating_code BETWEEN 0 AND 9 THEN 'reported_bc13'
             ELSE 'unknown' END AS condition_basis,
        CASE WHEN invalid_component_rating=1 THEN 1
             WHEN lowest_condition_rating_code BETWEEN 0 AND 9
                   AND derived_applicable_rating IS NOT NULL
                   AND abs(lowest_condition_rating_code-derived_applicable_rating)>0.000001
             THEN 1 ELSE 0 END AS condition_conflict
      FROM ratings
    )
    SELECT workspace_id, snapshot_revision_id, state_code, bridge_number,
      data_year, published_at,
      deck_condition_rating AS bc01, superstructure_condition_rating AS bc02,
      substructure_condition_rating AS bc03, culvert_condition_rating AS bc04,
      lowest_condition_rating_code AS bc13, controlling_rating,
      CASE WHEN controlling_rating>=7 THEN 'Good'
           WHEN controlling_rating>=5 THEN 'Fair'
           WHEN controlling_rating IS NOT NULL THEN 'Poor'
           ELSE 'Unknown' END AS condition_class,
      condition_basis, condition_conflict
    FROM controlling""",
)

COUNTY_INVENTORY_VIEW = ViewSpec(
    "v_county_bridge_inventory",
    """CREATE VIEW v_county_bridge_inventory AS SELECT
    p.workspace_id, p.state_code, p.county_code,
    COALESCE(c.county_name, 'Unknown') AS county_name, p.data_year,
    COUNT(*) AS bridge_count,
    SUM(CASE WHEN bc.condition_class='Good' THEN 1 ELSE 0 END) AS good_bridge_count,
    SUM(CASE WHEN bc.condition_class='Fair' THEN 1 ELSE 0 END) AS fair_bridge_count,
    SUM(CASE WHEN bc.condition_class='Poor' THEN 1 ELSE 0 END) AS poor_bridge_count,
    SUM(CASE WHEN bc.condition_class='Unknown' THEN 1 ELSE 0 END) AS unknown_condition_count,
    SUM(COALESCE(NULLIF(p.calculated_deck_area, 0),
        CASE WHEN p.total_bridge_length>0 AND p.bridge_width_out_to_out>0
             THEN p.total_bridge_length*p.bridge_width_out_to_out ELSE NULL END))
        AS total_deck_area
    FROM v_published_bridge_snapshots p
    JOIN v_bridge_condition bc
      ON bc.workspace_id=p.workspace_id AND bc.state_code=p.state_code
     AND bc.bridge_number=p.bridge_number AND bc.data_year=p.data_year
    LEFT JOIN ref_counties c
      ON c.state_code=p.state_code AND c.county_code=p.county_code
    GROUP BY p.workspace_id, p.state_code, p.county_code,
             COALESCE(c.county_name, 'Unknown'), p.data_year""",
)


CURRENT_COUNTY_INVENTORY_VIEW = ViewSpec(
    "v_current_county_bridge_inventory",
    """CREATE VIEW v_current_county_bridge_inventory AS
    WITH classified AS (
      SELECT p.*,
        CASE WHEN p.lowest_condition_rating_code BETWEEN 0 AND 9
             THEN p.lowest_condition_rating_code
             WHEN p.deck_condition_rating BETWEEN 0 AND 9
                   OR p.superstructure_condition_rating BETWEEN 0 AND 9
                   OR p.substructure_condition_rating BETWEEN 0 AND 9
             THEN min(
               CASE WHEN p.deck_condition_rating BETWEEN 0 AND 9
                    THEN p.deck_condition_rating ELSE 99 END,
               CASE WHEN p.superstructure_condition_rating BETWEEN 0 AND 9
                    THEN p.superstructure_condition_rating ELSE 99 END,
               CASE WHEN p.substructure_condition_rating BETWEEN 0 AND 9
                    THEN p.substructure_condition_rating ELSE 99 END)
             WHEN p.deck_condition_rating IS NULL
                   AND p.superstructure_condition_rating IS NULL
                   AND p.substructure_condition_rating IS NULL
                   AND p.culvert_condition_rating BETWEEN 0 AND 9
             THEN p.culvert_condition_rating ELSE NULL END AS controlling_rating
      FROM v_current_bridge_snapshots p
    )
    SELECT p.workspace_id, p.state_code, p.county_code,
      COALESCE(c.county_name, 'Unknown') AS county_name, p.data_year,
      COUNT(*) AS bridge_count,
      SUM(CASE WHEN p.controlling_rating>=7 THEN 1 ELSE 0 END) AS good_bridge_count,
      SUM(CASE WHEN p.controlling_rating>=5 AND p.controlling_rating<7
               THEN 1 ELSE 0 END) AS fair_bridge_count,
      SUM(CASE WHEN p.controlling_rating<5 THEN 1 ELSE 0 END) AS poor_bridge_count,
      SUM(CASE WHEN p.controlling_rating IS NULL THEN 1 ELSE 0 END)
        AS unknown_condition_count,
      SUM(COALESCE(NULLIF(p.calculated_deck_area, 0),
          CASE WHEN p.total_bridge_length>0 AND p.bridge_width_out_to_out>0
               THEN p.total_bridge_length*p.bridge_width_out_to_out ELSE NULL END))
        AS total_deck_area
    FROM classified p LEFT JOIN ref_counties c
      ON c.state_code=p.state_code AND c.county_code=p.county_code
    GROUP BY p.workspace_id, p.state_code, p.county_code,
             COALESCE(c.county_name, 'Unknown'), p.data_year""",
)


def _published_child_view(name: str, table: str, columns: str) -> ViewSpec:
    return ViewSpec(
        name,
        f"CREATE VIEW {name} AS SELECT p.workspace_id, "
        f"p.snapshot_revision_id, {columns} FROM v_published_bridge_snapshots p "
        f"JOIN {table} child ON child.state_code=p.state_code "
        "AND child.bridge_number=p.bridge_number AND child.data_year=p.data_year",
    )


BRIDGE_ELEMENTS_VIEW = _published_child_view(
    "v_bridge_elements",
    "elements",
    "child.state_code, child.bridge_number, child.data_year, "
    "child.element_number, child.element_parent_number, child.element_total_quantity, "
    "child.element_quantity_condition_state_one, child.element_quantity_condition_state_two, "
    "child.element_quantity_condition_state_three, child.element_quantity_condition_state_four",
)

BRIDGE_WORKS_VIEW = _published_child_view(
    "v_bridge_works",
    "works",
    "child.state_code, child.bridge_number, child.data_year, "
    "child.year_work_performed, child.work_performed",
)

BRIDGE_POSTING_STATUSES_VIEW = _published_child_view(
    "v_bridge_posting_statuses",
    "posting_statuses",
    "child.state_code, child.bridge_number, child.data_year, "
    "child.load_posting_status, child.posting_status_change_date",
)

BRIDGE_SPAN_SETS_VIEW = _published_child_view(
    "v_bridge_span_sets",
    "span_sets",
    "child.state_code, child.bridge_number, child.data_year, "
    "child.span_configuration_designation, child.number_of_spans, "
    "child.number_of_beam_lines, child.span_material, child.span_continuity, "
    "child.span_type, child.span_protective_system, child.deck_interaction, "
    "child.deck_material_and_type, child.wearing_surface, "
    "child.deck_protective_system, child.deck_reinforcing_protective_system, "
    "child.deck_stay_in_place_forms",
)

BRIDGE_DECK_MATERIAL_SUMMARY_VIEW = ViewSpec(
    "v_bridge_deck_material_summary",
    """CREATE VIEW v_bridge_deck_material_summary AS SELECT
    p.workspace_id, p.snapshot_revision_id,
    p.state_code, p.bridge_number, p.data_year,
    COUNT(s.id) AS span_set_count,
    SUM(CASE WHEN s.deck_material_and_type IS NOT NULL
              AND s.deck_material_and_type<>''
              AND s.deck_material_and_type<>'0' THEN 1 ELSE 0 END)
      AS known_deck_material_span_count,
    SUM(CASE WHEN s.deck_material_and_type IN
              ('C01','C02','C03','C04','C05','CX','CP-T','CR-T')
             THEN 1 ELSE 0 END) AS concrete_deck_span_count,
    CASE WHEN MAX(CASE WHEN s.deck_material_and_type IN
              ('C01','C02','C03','C04','C05','CX','CP-T','CR-T')
             THEN 1 ELSE 0 END)=1 THEN 1 ELSE 0 END AS has_concrete_deck,
    CASE WHEN MAX(CASE WHEN s.deck_material_and_type IS NOT NULL
              AND s.deck_material_and_type<>''
              AND s.deck_material_and_type<>'0' THEN 1 ELSE 0 END)=1
         THEN 1 ELSE 0 END AS has_known_deck_material
    FROM v_published_bridge_snapshots p LEFT JOIN span_sets s
      ON s.state_code=p.state_code AND s.bridge_number=p.bridge_number
     AND s.data_year=p.data_year
    GROUP BY p.workspace_id, p.snapshot_revision_id,
             p.state_code, p.bridge_number, p.data_year""",
)

CURRENT_BRIDGE_DECK_MATERIAL_SUMMARY_VIEW = ViewSpec(
    "v_current_bridge_deck_material_summary",
    """CREATE VIEW v_current_bridge_deck_material_summary AS SELECT
    current.workspace_id, current.snapshot_revision_id,
    current.state_code, current.bridge_number, current.data_year,
    COUNT(s.id) AS span_set_count,
    SUM(CASE WHEN s.deck_material_and_type IS NOT NULL
              AND s.deck_material_and_type<>''
              AND s.deck_material_and_type<>'0' THEN 1 ELSE 0 END)
      AS known_deck_material_span_count,
    SUM(CASE WHEN s.deck_material_and_type IN
              ('C01','C02','C03','C04','C05','CX','CP-T','CR-T')
             THEN 1 ELSE 0 END) AS concrete_deck_span_count,
    CASE WHEN MAX(CASE WHEN s.deck_material_and_type IN
              ('C01','C02','C03','C04','C05','CX','CP-T','CR-T')
             THEN 1 ELSE 0 END)=1 THEN 1 ELSE 0 END AS has_concrete_deck,
    CASE WHEN MAX(CASE WHEN s.deck_material_and_type IS NOT NULL
              AND s.deck_material_and_type<>''
              AND s.deck_material_and_type<>'0' THEN 1 ELSE 0 END)=1
         THEN 1 ELSE 0 END AS has_known_deck_material
    FROM v_current_bridge_snapshots current LEFT JOIN span_sets s
      ON s.state_code=current.state_code
     AND s.bridge_number=current.bridge_number
     AND s.data_year=current.data_year
    GROUP BY current.workspace_id, current.snapshot_revision_id,
             current.state_code, current.bridge_number, current.data_year""",
)

QUERY_SEMANTIC_VIEWS: tuple[ViewSpec, ...] = (
    PUBLISHED_BRIDGES_VIEW,
    CURRENT_BRIDGES_VIEW,
    BRIDGE_FEATURES_VIEW,
    CARRIED_HIGHWAY_FEATURES_VIEW,
    BRIDGE_ROUTES_VIEW,
    BRIDGE_CONDITION_VIEW,
    COUNTY_INVENTORY_VIEW,
    CURRENT_COUNTY_INVENTORY_VIEW,
    BRIDGE_ELEMENTS_VIEW,
    BRIDGE_WORKS_VIEW,
    BRIDGE_POSTING_STATUSES_VIEW,
    BRIDGE_SPAN_SETS_VIEW,
    BRIDGE_DECK_MATERIAL_SUMMARY_VIEW,
    CURRENT_BRIDGE_DECK_MATERIAL_SUMMARY_VIEW,
)

SCHEMA_MIGRATIONS_TABLE = TableSpec(
    name="schema_migrations",
    columns=(
        ColumnSpec("version", "INTEGER", primary_key_position=1),
        ColumnSpec("checksum", "TEXT", nullable=False),
        ColumnSpec("applied_at", "TEXT", nullable=False),
        ColumnSpec("app_revision", "TEXT", nullable=False),
    ),
)

AUTHORITATIVE_SCHEMA = SchemaManifest(
    version=LATEST_SCHEMA_VERSION,
    tables=(
        SCHEMA_MIGRATIONS_TABLE,
        *PROJECTION_CHILD_TABLES,
        *REVISION_STORE_TABLES,
        *QUERY_SUPPORT_TABLES,
        *QUERY_RESPONSE_AUDIT_TABLES,
        *CHAT_SESSION_TABLES,
        *DEPLOYMENT_IDENTITY_TABLES,
        *FORECAST_ANALYTICS_TABLES,
        *KPI_PROGRAM_TABLES,
        *POINT_CLOUD_RUN_TABLES,
    ),
    views=QUERY_SEMANTIC_VIEWS,
)


LEGACY_FEATURES_CREATE_SQL = """
CREATE TABLE features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state_code INTEGER NOT NULL,
    bridge_number TEXT NOT NULL,
    feature_type TEXT,
    feature_location TEXT,
    feature_name TEXT,
    functional_classification TEXT,
    urban_code TEXT,
    nhs_designation TEXT,
    national_highway_freight_network TEXT,
    strahnet_designation TEXT,
    lrs_route_id TEXT,
    lrs_mile_point REAL,
    lanes_on_highway REAL,
    annual_average_daily_traffic REAL,
    annual_average_daily_truck_traffic REAL,
    year_of_annual_average_daily_traffic INTEGER,
    highway_max_usable_vertical_clearance REAL,
    highway_min_vertical_clearance REAL,
    highway_min_horizontal_clearance_left REAL,
    highway_min_horizontal_clearance_right REAL,
    highway_max_usable_surface_width REAL,
    bypass_detour_length REAL,
    crossing_bridge_number TEXT,
    data_year INTEGER,
    FOREIGN KEY (state_code, bridge_number, data_year)
        REFERENCES bridges (state_code, bridge_number, data_year)
)
""".strip()


SCHEMA_MIGRATIONS_CREATE_SQL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    app_revision TEXT NOT NULL
)
""".strip()


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(value: str) -> str:
    """Quote a manifest-controlled SQLite identifier."""

    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Unsafe SQLite identifier: {value!r}")
    return f'"{value}"'


def create_index_sql(
    table_name: str,
    index: IndexSpec,
    *,
    if_not_exists: bool = False,
) -> str:
    """Render one manifest-controlled index, including expression indexes."""

    terms = index.sql_terms or tuple(
        quote_identifier(column) for column in index.columns
    )
    if not terms:
        raise ValueError(f"Index {index.name!r} has no columns or SQL terms")
    unique = "UNIQUE " if index.unique else ""
    optional = "IF NOT EXISTS " if if_not_exists else ""
    return (
        f"CREATE {unique}INDEX {optional}{quote_identifier(index.name)} "
        f"ON {quote_identifier(table_name)} ({', '.join(terms)})"
    )


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def view_exists(connection: sqlite3.Connection, view_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = ?",
        (view_name,),
    ).fetchone()
    return row is not None


def _normalise_type(value: object) -> str:
    return " ".join(str(value or "").upper().split())


def _normalise_default(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).strip().split())


def _normalise_schema_sql(value: object) -> str:
    return re.sub(r'[\s"`\[\];]+', "", str(value or "").lower())


def _column_differences(
    connection: sqlite3.Connection,
    table: TableSpec,
    *,
    allow_missing: Iterable[str] = (),
) -> list[str]:
    allowed_missing = frozenset(allow_missing)
    rows = connection.execute(
        f"PRAGMA table_info({quote_identifier(table.name)})"
    ).fetchall()
    live = {str(row[1]): row for row in rows}
    expected_names = tuple(column.name for column in table.columns)
    differences: list[str] = []

    for position, column in enumerate(table.columns):
        row = live.get(column.name)
        if row is None:
            if column.name not in allowed_missing:
                differences.append(f"{table.name}.{column.name}: missing column")
            continue
        if _normalise_type(row[2]) != _normalise_type(column.declared_type):
            differences.append(
                f"{table.name}.{column.name}: type {_normalise_type(row[2])!r} "
                f"!= {_normalise_type(column.declared_type)!r}"
            )
        if bool(row[3]) != column.not_null:
            differences.append(
                f"{table.name}.{column.name}: not-null {bool(row[3])!r} "
                f"!= {column.not_null!r}"
            )
        if int(row[5]) != column.primary_key_position:
            differences.append(
                f"{table.name}.{column.name}: primary-key position {int(row[5])} "
                f"!= {column.primary_key_position}"
            )
        if _normalise_default(row[4]) != _normalise_default(column.default_sql):
            differences.append(
                f"{table.name}.{column.name}: default {_normalise_default(row[4])!r} "
                f"!= {_normalise_default(column.default_sql)!r}"
            )
        if int(row[0]) != position:
            differences.append(
                f"{table.name}.{column.name}: ordinal {int(row[0])} != {position}"
            )

    unexpected = sorted(set(live).difference(expected_names))
    differences.extend(
        f"{table.name}.{column_name}: unexpected column" for column_name in unexpected
    )
    return differences


def _foreign_key_differences(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> list[str]:
    rows = connection.execute(
        f"PRAGMA foreign_key_list({quote_identifier(table.name)})"
    ).fetchall()
    grouped: dict[int, list[tuple[object, ...]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(tuple(row))

    live: set[tuple[tuple[str, ...], str, tuple[str, ...], str, str]] = set()
    for fk_rows in grouped.values():
        ordered = sorted(fk_rows, key=lambda row: int(row[1]))
        live.add(
            (
                tuple(str(row[3]) for row in ordered),
                str(ordered[0][2]),
                tuple(str(row[4]) for row in ordered),
                str(ordered[0][5]).upper(),
                str(ordered[0][6]).upper(),
            )
        )

    expected = {
        (
            key.columns,
            key.referenced_table,
            key.referenced_columns,
            key.on_update.upper(),
            key.on_delete.upper(),
        )
        for key in table.foreign_keys
    }
    differences: list[str] = []
    for missing in sorted(expected.difference(live), key=repr):
        differences.append(f"{table.name}: missing foreign key {missing!r}")
    for extra in sorted(live.difference(expected), key=repr):
        differences.append(f"{table.name}: unexpected foreign key {extra!r}")
    return differences


def _index_differences(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> list[str]:
    differences: list[str] = []
    list_rows = connection.execute(
        f"PRAGMA index_list({quote_identifier(table.name)})"
    ).fetchall()
    unique_by_name = {str(item[1]): bool(item[2]) for item in list_rows}
    expected_names = {index.name for index in table.indexes}
    # SQLite owns indexes whose origin is ``pk`` or ``u`` (PRIMARY KEY and
    # inline UNIQUE constraints).  Only independently created indexes are part
    # of this manifest's explicit index set.
    explicit_live_names = {
        str(item[1])
        for item in list_rows
        if len(item) < 4 or str(item[3]).lower() == "c"
    }
    for expected in table.indexes:
        row = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (expected.name,),
        ).fetchone()
        if row is None:
            differences.append(f"{table.name}: missing index {expected.name}")
            continue
        if expected.sql_terms:
            live_sql = _normalise_schema_sql(row[1])
            expected_sql = _normalise_schema_sql(
                create_index_sql(table.name, expected)
            )
            if live_sql != expected_sql:
                differences.append(
                    f"{table.name}.{expected.name}: expression SQL "
                    f"{row[1]!r} != {create_index_sql(table.name, expected)!r}"
                )
        else:
            index_rows = connection.execute(
                f"PRAGMA index_info({quote_identifier(expected.name)})"
            ).fetchall()
            columns = tuple(
                str(item[2]) for item in sorted(index_rows, key=lambda item: item[0])
            )
            if columns != expected.columns:
                differences.append(
                    f"{table.name}.{expected.name}: columns "
                    f"{columns!r} != {expected.columns!r}"
                )
        if unique_by_name.get(expected.name) != expected.unique:
            differences.append(
                f"{table.name}.{expected.name}: unique "
                f"{unique_by_name.get(expected.name)!r} != {expected.unique!r}"
            )
    for unexpected in sorted(explicit_live_names.difference(expected_names)):
        differences.append(f"{table.name}: unexpected index {unexpected}")
    return differences


def _trigger_differences(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> list[str]:
    # Projection tables historically permit service/test-owned operational
    # triggers.  Once a table declares migration-owned triggers, however, the
    # full trigger set and definitions become authoritative.
    if not table.triggers:
        return []
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='trigger' AND tbl_name=? ORDER BY name",
        (table.name,),
    ).fetchall()
    live = {str(row[0]): str(row[1] or "") for row in rows}
    expected = {trigger.name: trigger for trigger in table.triggers}
    differences = [
        f"{table.name}: unexpected trigger {name}"
        for name in sorted(set(live).difference(expected))
    ]
    differences.extend(
        f"{table.name}: missing trigger {name}"
        for name in sorted(set(expected).difference(live))
    )
    for name in sorted(set(live).intersection(expected)):
        if _normalise_schema_sql(live[name]) != _normalise_schema_sql(
            expected[name].sql
        ):
            differences.append(
                f"{table.name}.{name}: trigger definition differs from manifest"
            )
    return differences


def collect_schema_drift(
    connection: sqlite3.Connection,
    manifest: SchemaManifest = AUTHORITATIVE_SCHEMA,
) -> tuple[str, ...]:
    """Return stable, sorted drift diagnostics for every owned table."""

    differences: list[str] = []
    for table in manifest.tables:
        if not table_exists(connection, table.name):
            differences.append(f"{table.name}: missing table")
            continue
        differences.extend(_column_differences(connection, table))
        differences.extend(_foreign_key_differences(connection, table))
        differences.extend(_index_differences(connection, table))
        differences.extend(_trigger_differences(connection, table))
    live_views = {
        str(row[0]): str(row[1] or "")
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='view'"
        ).fetchall()
    }
    for view in manifest.views:
        live_sql = live_views.get(view.name)
        if live_sql is None:
            differences.append(f"{view.name}: missing view")
        elif _normalise_schema_sql(live_sql) != _normalise_schema_sql(view.sql):
            differences.append(f"{view.name}: view definition differs from manifest")
    return tuple(sorted(differences))


def assert_schema_matches(
    connection: sqlite3.Connection,
    manifest: SchemaManifest = AUTHORITATIVE_SCHEMA,
) -> None:
    """Raise :class:`SchemaDriftError` unless the owned schema is exact."""

    differences = collect_schema_drift(connection, manifest)
    if differences:
        raise SchemaDriftError(differences)


def assert_table_matches(connection: sqlite3.Connection, table: TableSpec) -> None:
    """Validate one owned table without weakening the full manifest check."""

    if not table_exists(connection, table.name):
        raise SchemaDriftError((f"{table.name}: missing table",))
    differences = _column_differences(connection, table)
    differences.extend(_foreign_key_differences(connection, table))
    differences.extend(_index_differences(connection, table))
    differences.extend(_trigger_differences(connection, table))
    if differences:
        raise SchemaDriftError(differences)


def assert_legacy_features_compatible(connection: sqlite3.Connection) -> None:
    """Validate the v0 feature projection while allowing only the nine gaps."""

    if not table_exists(connection, FEATURES_TABLE.name):
        raise SchemaDriftError(("features: missing table",))
    missing = tuple(column.name for column in FEATURE_TRANSITION_COLUMNS)
    differences = _column_differences(
        connection,
        FEATURES_V2_TABLE,
        allow_missing=missing,
    )
    target_fk_differences = _foreign_key_differences(connection, FEATURES_TABLE)
    legacy_table = TableSpec(
        name=FEATURES_V2_TABLE.name,
        columns=FEATURES_V2_TABLE.columns,
        foreign_keys=(LEGACY_FEATURES_FOREIGN_KEY,),
    )
    legacy_fk_differences = _foreign_key_differences(connection, legacy_table)
    # A v0 table has NO ACTION/NO ACTION.  A partially or fully migrated table
    # may already have the target CASCADE/CASCADE relationship.  No third
    # behavior is adopted silently.
    if target_fk_differences and legacy_fk_differences:
        differences.extend(legacy_fk_differences)
    if differences:
        raise SchemaDriftError(differences)
