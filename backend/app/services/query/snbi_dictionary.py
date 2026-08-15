"""Hash-verified SNBI field dictionary for semantic query planning.

This module combines only locally inspectable evidence:

* FHWA's vendored NBI Submittal Schema v04 (exact bytes SHA-256 checked);
* BridgeGuard's canonical container/cardinality contract;
* reviewed physical projection mappings;
* reviewed transition-source metadata and local validation guidance.

The schema supplies authoritative field names, short definitions, JSON types,
requiredness, and structural constraints.  It does *not* supply the complete
SNBI narrative specification, item units, or code meanings.  Those properties
are marked unavailable unless this repository has explicit reviewed evidence.
The crosswalk and detailed-code-mapping source bytes are locally vendored and
SHA-256 verified.  That verifies source identity, not the correctness or
completeness of BridgeGuard's transition implementation; transition notes are
therefore exposed as reviewed local implementation evidence, never as FHWA
certification or a fully executable federal rule set.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from app.routers.services.constants import mapping as TAG_TO_PHYSICAL_COLUMN
from app.routers.services.editor_metadata import (
    DEFAULT_NOTES,
    FEATURE_SOURCE_FIELDS,
    INSPECTION_SOURCE_FIELDS,
    MAIN_SOURCE_FIELDS,
    POSTING_STATUS_SOURCE_FIELDS,
    ROUTE_SOURCE_FIELDS,
    SPAN_SOURCE_FIELDS,
    SUBSTRUCTURE_SOURCE_FIELDS,
    TRANSITION_NOTES,
    WORK_SOURCE_FIELDS,
)
from app.routers.services.snbi_rules import CODE_LISTS, FIELD_GUIDANCE, TEMPORARY_CODES
from app.routers.services.snbi_validation import DATE_FIELDS, DO_NOT_REPORT_FIELDS
from app.services.conversion.canonical_schema import ARRAY_SPECS, ROUTE_SPEC


DICTIONARY_VERSION = "bridgeguard-snbi-dictionary-v1"
SNBI_SCHEMA_ARTIFACT_ID = "snbi_schema_v04_july_2024"
SNBI_SCHEMA_SHA256 = "2555813afaab4701c11e3d41b49cb1f894dfcae772afbd1638d9d25348e383a0"
SNBI_SCHEMA_UPDATED_ON = "2024-07-18"
SNBI_SCHEMA_SOURCE_URL = "https://www.fhwa.dot.gov/bridge/snbi/SNBI.Schema.v04.json"

_APP_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _APP_ROOT / "data" / "fhwa" / "artifacts" / "SNBI.Schema.v04.json"
_REGISTRY_PATH = _APP_ROOT / "data" / "fhwa" / "registry.json"


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class SchemaConstraint:
    keyword: str
    value_json: str

    @property
    def value(self) -> object:
        return json.loads(self.value_json)

    def to_dict(self) -> dict[str, object]:
        return {"keyword": self.keyword, "value": self.value}


@dataclass(frozen=True, slots=True)
class DictionaryProvenance:
    artifact_id: str
    title: str
    authority: str
    artifact_state: str
    source_url: str | None
    sha256: str | None
    updated_on: str | None
    evidence_use: str

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "title": self.title,
            "authority": self.authority,
            "artifact_state": self.artifact_state,
            "source_url": self.source_url,
            "sha256": self.sha256,
            "updated_on": self.updated_on,
            "evidence_use": self.evidence_use,
        }


@dataclass(frozen=True, slots=True)
class SnbiFieldDefinition:
    tag: str
    title: str
    section_id: str
    container: str
    container_path: str
    cardinality: str
    logical_key_fields: tuple[str, ...]
    required_in_container: bool
    json_type: str
    constraints: tuple[SchemaConstraint, ...]
    unit: str | None
    unit_evidence: str
    code_values: tuple[str, ...]
    code_domain_status: str
    null_semantics: str
    physical_table: str
    physical_column: str | None
    legacy_source_fields: tuple[str, ...]
    transition_note: str | None
    transition_evidence_status: str
    calculation_status: str
    submission_reportable: bool
    temporal_semantics: str
    cautions: tuple[str, ...]
    definition_provenance_ids: tuple[str, ...]

    @property
    def json_pointer_pattern(self) -> str:
        if self.container == "Bridge":
            return f"/{self.tag}"
        if self.container == "Routes":
            return f"/Features/*/Routes/*/{self.tag}"
        return f"/{self.container}/*/{self.tag}"

    def to_dict(self, *, include_projection: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "tag": self.tag,
            "title": self.title,
            "section_id": self.section_id,
            "container": self.container,
            "container_path": self.container_path,
            "json_pointer_pattern": self.json_pointer_pattern,
            "cardinality": self.cardinality,
            "logical_key_fields": list(self.logical_key_fields),
            "required_in_container": self.required_in_container,
            "json_type": self.json_type,
            "constraints": [constraint.to_dict() for constraint in self.constraints],
            "unit": self.unit,
            "unit_evidence": self.unit_evidence,
            "code_values": list(self.code_values),
            "code_domain_status": self.code_domain_status,
            "null_semantics": self.null_semantics,
            "legacy_source_fields": list(self.legacy_source_fields),
            "transition_note": self.transition_note,
            "transition_evidence_status": self.transition_evidence_status,
            "calculation_status": self.calculation_status,
            "submission_reportable": self.submission_reportable,
            "temporal_semantics": self.temporal_semantics,
            "cautions": list(self.cautions),
            "definition_provenance_ids": list(self.definition_provenance_ids),
        }
        if include_projection:
            payload["physical_table"] = self.physical_table
            payload["physical_column"] = self.physical_column
        return payload


@dataclass(frozen=True, slots=True)
class SnbiDictionary:
    definition_version: str
    schema_artifact_id: str
    schema_sha256: str
    schema_dialect: str
    schema_execution_status: str
    fields: tuple[SnbiFieldDefinition, ...]
    provenance: tuple[DictionaryProvenance, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "provenance", tuple(self.provenance))
        tags = tuple(field.tag for field in self.fields)
        if not tags or len(tags) != len(set(tags)):
            raise ValueError("SNBI dictionary fields must have unique tags")

    @property
    def fields_by_tag(self) -> Mapping[str, SnbiFieldDefinition]:
        return MappingProxyType({field.tag: field for field in self.fields})

    @property
    def dictionary_hash(self) -> str:
        return hashlib.sha256(
            _stable_json(self._payload(include_projection=True)).encode("utf-8")
        ).hexdigest()

    def get(self, tag: str) -> SnbiFieldDefinition:
        normalized = str(tag).strip().upper()
        field = self.fields_by_tag.get(normalized)
        if field is None:
            raise KeyError(tag)
        return field

    def _select(self, tags: Iterable[str] | None) -> tuple[SnbiFieldDefinition, ...]:
        if tags is None:
            return self.fields
        normalized = tuple(sorted({str(tag).strip().upper() for tag in tags}))
        unknown = tuple(tag for tag in normalized if tag not in self.fields_by_tag)
        if unknown:
            raise KeyError(", ".join(unknown))
        return tuple(self.fields_by_tag[tag] for tag in normalized)

    def _payload(
        self,
        *,
        fields: tuple[SnbiFieldDefinition, ...] | None = None,
        include_projection: bool,
    ) -> dict[str, object]:
        selected = self.fields if fields is None else fields
        return {
            "dictionary_version": self.definition_version,
            "schema_artifact_id": self.schema_artifact_id,
            "schema_sha256": self.schema_sha256,
            "schema_dialect": self.schema_dialect,
            "schema_execution_status": self.schema_execution_status,
            "authority_boundary": (
                "The vendored v04 schema is hash-verified for names, descriptions, JSON shape, "
                "and constraints. Its declared 2024-07 JSON-Schema dialect is not executable by "
                "BridgeGuard. Transition/code metadata is reviewed local implementation evidence "
                "unless its provenance says vendored_verified."
            ),
            "field_count": len(selected),
            "fields": [
                field.to_dict(include_projection=include_projection)
                for field in selected
            ],
            "provenance": [item.to_dict() for item in self.provenance],
        }

    def prompt_payload(
        self,
        tags: Iterable[str] | None = None,
        *,
        include_projection: bool = True,
    ) -> dict[str, object]:
        """Return a deterministic reviewed prompt payload, optionally field-scoped."""

        selected = self._select(tags)
        payload = self._payload(
            fields=selected,
            include_projection=include_projection,
        )
        payload["dictionary_hash"] = self.dictionary_hash
        return payload

    def prompt_text(
        self,
        tags: Iterable[str] | None = None,
        *,
        include_projection: bool = True,
    ) -> str:
        return _stable_json(
            self.prompt_payload(tags, include_projection=include_projection)
        )


_SECTION_PREFIXES = (
    ("BID", "B.ID"),
    ("BCL", "B.CL"),
    ("BRH", "B.RH"),
    ("BLR", "B.LR"),
    ("BIR", "B.IR"),
    ("BAP", "B.AP"),
    ("BCS", "B.CS"),
    ("BRR", "B.RR"),
    ("BRT", "B.RT"),
    ("BIE", "B.IE"),
    ("BEP", "B.EP"),
    ("BPS", "B.PS"),
    ("BSP", "B.SP"),
    ("BSB", "B.SB"),
    ("BL", "B.L"),
    ("BG", "B.G"),
    ("BC", "B.C"),
    ("BW", "B.W"),
    ("BE", "B.E"),
    ("BF", "B.F"),
    ("BH", "B.H"),
    ("BN", "B.N"),
)


_TABLE_BY_CONTAINER = {
    "Bridge": "bridges",
    "Elements": "elements",
    "Features": "features",
    "Routes": "routes",
    "Inspections": "inspections",
    "PostingEvaluations": "posting_evaluations",
    "PostingStatuses": "posting_statuses",
    "SpanSets": "span_sets",
    "SubstructureSets": "substructure_sets",
    "Works": "works",
}


_UNIT_BY_TAG: Mapping[str, tuple[str, str]] = {
    "BL05": ("decimal_degree", "schema range and reviewed field guidance"),
    "BL06": ("decimal_degree", "schema range and reviewed field guidance"),
    "BL09": ("percent", "reviewed NBI transition source metadata (Item 98B)"),
    "BG01": ("foot", "reviewed local transition rule"),
    "BG02": ("foot", "reviewed local transition rule"),
    "BG03": ("foot", "reviewed local transition rule"),
    "BG04": ("foot", "reviewed local transition rule"),
    "BG05": ("foot", "reviewed local transition rule"),
    "BG06": ("foot", "reviewed local transition rule"),
    "BG07": ("foot", "reviewed local transition rule"),
    "BG08": ("foot", "reviewed local transition rule"),
    "BG09": ("foot", "reviewed local transition rule"),
    "BG11": ("degree", "reviewed NBI transition source metadata (Item 34)"),
    "BG13": ("foot", "reviewed local editor guidance"),
    "BG15": ("square_foot", "reviewed physical field identity and SNBI title"),
    "BG16": ("square_foot", "reviewed local transition calculation"),
    "BLR05": ("dimensionless_rating_factor", "SNBI field title and reviewed local guidance"),
    "BLR06": ("dimensionless_rating_factor", "SNBI field title and reviewed local guidance"),
    "BLR07": ("dimensionless_rating_factor", "reviewed local editor guidance"),
    "BH07": ("mile", "reviewed local transition rule"),
    "BH08": ("lane", "SNBI field title and reviewed transition source metadata"),
    "BH09": ("vehicle_per_day", "SNBI field title and reviewed transition source metadata"),
    "BH10": ("truck_per_day", "SNBI field title and reviewed local transition rule"),
    "BH12": ("foot", "reviewed local transition rule"),
    "BH13": ("foot", "reviewed local transition rule"),
    "BH14": ("foot", "reviewed local transition rule"),
    "BH15": ("foot", "reviewed local transition rule"),
    "BH16": ("foot", "reviewed local transition rule"),
    "BH17": ("mile", "reviewed local transition rule"),
    "BRR02": ("foot", "reviewed local transition rule"),
    "BRR03": ("foot", "reviewed local transition rule"),
    "BN02": ("foot", "reviewed local transition rule"),
    "BN03": ("foot", "reviewed local transition rule"),
    "BN04": ("foot", "reviewed local transition rule"),
    "BN05": ("foot", "reviewed local transition rule"),
    "BIE05": ("month", "SNBI field title and reviewed NBI source metadata (Item 91)"),
    "BEP02": ("dimensionless_rating_factor", "SNBI field title"),
}


_TEMPORAL_BY_TAG = {
    "BW01": "construction calendar year; distinct from inventory snapshot data_year",
    "BW02": "work-event calendar year; distinct from inventory snapshot data_year",
    "BH11": "traffic-observation calendar year; distinct from inventory snapshot data_year",
    "BLR03": "load-rating event date encoded YYYYMMDD",
    "BIE02": "inspection begin event date encoded YYYYMMDD",
    "BIE03": "inspection completion event date encoded YYYYMMDD",
    "BIE06": "calculated inspection due date encoded YYYYMMDD; do not report",
    "BIE08": "inspection quality-control event date encoded YYYYMMDD",
    "BIE09": "inspection quality-assurance event date encoded YYYYMMDD",
    "BIE10": "inspection data-update event date encoded YYYYMMDD",
    "BPS02": "posting-status change event date encoded YYYYMMDD",
}


_CALCULATION_BY_TAG = {
    "BG16": "fhwa_calculated_do_not_report",
    "BC12": "fhwa_calculated_do_not_report",
    "BC13": "fhwa_calculated_do_not_report",
    "BIE06": "fhwa_calculated_do_not_report",
}


def _section_id(tag: str) -> str:
    for prefix, section in _SECTION_PREFIXES:
        if tag.startswith(prefix):
            return section
    raise ValueError(f"unrecognized SNBI tag prefix: {tag}")


def _source_fields() -> dict[str, tuple[str, ...]]:
    sources: dict[str, tuple[str, ...]] = {}
    for table in (
        MAIN_SOURCE_FIELDS,
        FEATURE_SOURCE_FIELDS,
        ROUTE_SOURCE_FIELDS,
        SPAN_SOURCE_FIELDS,
        SUBSTRUCTURE_SOURCE_FIELDS,
        INSPECTION_SOURCE_FIELDS,
        POSTING_STATUS_SOURCE_FIELDS,
        WORK_SOURCE_FIELDS,
    ):
        for tag, fields in table.items():
            sources[tag] = tuple(str(item) for item in fields)
    return sources


def _schema_items(schema: Mapping[str, object]) -> Mapping[str, object]:
    items = schema.get("items")
    if isinstance(items, list):
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("unsupported tuple-style schema items")
        return items[0]
    if not isinstance(items, dict):
        raise ValueError("schema array items must be an object schema")
    return items


def _read_verified_schema() -> tuple[dict[str, object], DictionaryProvenance]:
    registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    artifacts = {
        str(item["id"]): item
        for item in registry.get("artifacts", [])
        if isinstance(item, dict) and "id" in item
    }
    metadata = artifacts.get(SNBI_SCHEMA_ARTIFACT_ID)
    if not isinstance(metadata, dict):
        raise RuntimeError(f"missing registry artifact {SNBI_SCHEMA_ARTIFACT_ID}")
    if metadata.get("state") != "vendored_verified":
        raise RuntimeError("SNBI schema artifact is not vendored_verified")
    if metadata.get("sha256") != SNBI_SCHEMA_SHA256:
        raise RuntimeError("registered SNBI schema digest differs from reviewed dictionary digest")
    raw = _SCHEMA_PATH.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != SNBI_SCHEMA_SHA256:
        raise RuntimeError("vendored SNBI schema digest mismatch")
    schema = json.loads(raw)
    if not isinstance(schema, dict):
        raise RuntimeError("vendored SNBI schema root is not an object")
    provenance = DictionaryProvenance(
        artifact_id=SNBI_SCHEMA_ARTIFACT_ID,
        title=str(metadata.get("title") or "NBI Submittal Schema v04"),
        authority=str(metadata.get("authority") or "FHWA"),
        artifact_state=str(metadata["state"]),
        source_url=str(metadata.get("source_url") or SNBI_SCHEMA_SOURCE_URL),
        sha256=str(metadata["sha256"]),
        updated_on=str(metadata.get("updated_on") or SNBI_SCHEMA_UPDATED_ON),
        evidence_use="field title, JSON type, requiredness, container, and schema constraints",
    )
    return schema, provenance


def _transition_provenance() -> tuple[DictionaryProvenance, ...]:
    registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    wanted = {"nbi_snbi_crosswalk_dec_2023", "nbi_snbi_code_mapping"}
    output: list[DictionaryProvenance] = []
    for item in registry.get("artifacts", []):
        if not isinstance(item, dict) or item.get("id") not in wanted:
            continue
        output.append(
            DictionaryProvenance(
                artifact_id=str(item["id"]),
                title=str(item.get("title") or item["id"]),
                authority=str(item.get("authority") or "FHWA"),
                artifact_state=str(item.get("state") or "unavailable"),
                source_url=str(item["source_url"]) if item.get("source_url") else None,
                sha256=str(item["sha256"]) if item.get("sha256") else None,
                updated_on=str(item["updated_on"]) if item.get("updated_on") else None,
                evidence_use=(
                    "SHA-256 verified FHWA source bytes used as provenance for "
                    "reviewed local transition implementation notes; byte identity "
                    "does not certify mapping completeness or execution"
                    if item.get("state") == "vendored_verified"
                    else
                    "registered remote provenance for reviewed local transition "
                    "implementation notes; not executable authoritative evidence"
                ),
            )
        )
    return tuple(sorted(output, key=lambda item: item.artifact_id))


def _containers(
    root_items: Mapping[str, object],
) -> tuple[tuple[str, Mapping[str, object], tuple[str, ...], tuple[str, ...]], ...]:
    properties = root_items.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("schema bridge items must define properties")
    root_required = tuple(str(value) for value in root_items.get("required", []))
    top_fields = {
        tag: value
        for tag, value in properties.items()
        if isinstance(tag, str) and tag.startswith("B") and isinstance(value, dict)
    }
    output: list[
        tuple[str, Mapping[str, object], tuple[str, ...], tuple[str, ...]]
    ] = [("Bridge", top_fields, root_required, ("BL01", "BID01"))]

    for container_name in (
        "Elements",
        "Features",
        "Inspections",
        "PostingEvaluations",
        "PostingStatuses",
        "SpanSets",
        "SubstructureSets",
        "Works",
    ):
        container_schema = properties.get(container_name)
        if not isinstance(container_schema, dict):
            raise ValueError(f"missing schema container {container_name}")
        item_schema = _schema_items(container_schema)
        item_properties = item_schema.get("properties")
        if not isinstance(item_properties, dict):
            raise ValueError(f"missing properties for {container_name}")
        fields = {
            tag: value
            for tag, value in item_properties.items()
            if isinstance(tag, str) and tag.startswith("B") and isinstance(value, dict)
        }
        required = tuple(str(value) for value in item_schema.get("required", []))
        canonical = ARRAY_SPECS[container_name]
        output.append((container_name, fields, required, canonical.logical_key))
        if container_name == "Features":
            routes_schema = item_properties.get("Routes")
            if not isinstance(routes_schema, dict):
                raise ValueError("missing nested Routes schema")
            route_items = _schema_items(routes_schema)
            route_properties = route_items.get("properties")
            if not isinstance(route_properties, dict):
                raise ValueError("missing route properties")
            output.append(
                (
                    "Routes",
                    {
                        tag: value
                        for tag, value in route_properties.items()
                        if isinstance(tag, str)
                        and tag.startswith("B")
                        and isinstance(value, dict)
                    },
                    tuple(str(value) for value in route_items.get("required", [])),
                    ROUTE_SPEC.logical_key,
                )
            )
    return tuple(output)


def _constraint_tuple(schema: Mapping[str, object]) -> tuple[SchemaConstraint, ...]:
    excluded = {"description", "type", "properties", "items", "required", "additionalProperties"}
    return tuple(
        SchemaConstraint(str(keyword), _stable_json(value))
        for keyword, value in sorted(schema.items())
        if keyword not in excluded
    )


def _field_cautions(tag: str, code_values: tuple[str, ...]) -> tuple[str, ...]:
    cautions: list[str] = []
    default_note = DEFAULT_NOTES.get(tag)
    if default_note:
        cautions.append(default_note)
    guidance = FIELD_GUIDANCE.get(tag)
    if guidance:
        expected = guidance.get("expected")
        hint = guidance.get("hint")
        if expected:
            cautions.append(f"Expected local input form: {expected}")
        if hint:
            cautions.append(hint)
    if any(value in TEMPORARY_CODES or value.endswith("-T") for value in code_values):
        cautions.append(
            "The reviewed local code subset includes temporary transition codes; refine them under the applicable profile before submission."
        )
    if tag in DO_NOT_REPORT_FIELDS:
        cautions.append("FHWA calculates or derives this field; retain only as provenance/internal data and do not report it.")
    return tuple(dict.fromkeys(cautions))


def build_snbi_dictionary() -> SnbiDictionary:
    """Build the complete immutable dictionary from the verified local schema."""

    schema, schema_provenance = _read_verified_schema()
    transition_provenance = _transition_provenance()
    transition_sources_verified = bool(transition_provenance) and all(
        source.artifact_state == "vendored_verified"
        for source in transition_provenance
    )
    root_items = _schema_items(schema)
    sources = _source_fields()
    fields: list[SnbiFieldDefinition] = []
    for container, properties, required, logical_key in _containers(root_items):
        for tag, field_schema in properties.items():
            json_type = field_schema.get("type")
            if not isinstance(json_type, str):
                raise ValueError(f"{container}.{tag} has no single JSON type")
            title = field_schema.get("description")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"{container}.{tag} has no schema description")
            unit, unit_evidence = _UNIT_BY_TAG.get(
                tag,
                (None, "unavailable in the vendored v04 schema and reviewed local metadata"),
            )
            code_values = tuple(sorted(str(value) for value in CODE_LISTS.get(tag, set())))
            if code_values:
                code_status = (
                    "reviewed_local_validation_subset_not_a_complete_pinned_fhwa_code_dictionary"
                )
            else:
                code_status = "unavailable_or_not_code_constrained_in_reviewed_local_metadata"
            transition_note = TRANSITION_NOTES.get(tag)
            legacy_sources = sources.get(tag, ())
            if transition_note or legacy_sources:
                transition_status = (
                    "reviewed_local_implementation; exact governing FHWA crosswalk/code "
                    "source bytes are vendored_verified, but mapping execution is not "
                    "FHWA-certified"
                    if transition_sources_verified
                    else
                    "reviewed_local_implementation; governing FHWA crosswalk/code "
                    "source bytes are not both vendored_verified"
                )
                provenance_ids = (
                    SNBI_SCHEMA_ARTIFACT_ID,
                    "nbi_snbi_crosswalk_dec_2023",
                    "nbi_snbi_code_mapping",
                )
            else:
                transition_status = "no reviewed legacy transition evidence available"
                provenance_ids = (SNBI_SCHEMA_ARTIFACT_ID,)
            container_path = (
                "/" if container == "Bridge" else "/Features/*/Routes" if container == "Routes" else f"/{container}"
            )
            required_here = tag in set(required)
            null_semantics = (
                "required in each present container row; the v04 schema type excludes JSON null"
                if required_here
                else "optional: represent unavailable/not-applicable by omission; the v04 schema type excludes JSON null"
            )
            fields.append(
                SnbiFieldDefinition(
                    tag=tag,
                    title=title.strip(),
                    section_id=_section_id(tag),
                    container=container,
                    container_path=container_path,
                    cardinality=(
                        "exactly_one_value_per_bridge"
                        if container == "Bridge" and required_here
                        else "zero_or_one_value_per_bridge"
                        if container == "Bridge"
                        else "zero_or_one_value_per_repeated_container_row"
                    ),
                    logical_key_fields=tuple(logical_key),
                    required_in_container=required_here,
                    json_type=json_type,
                    constraints=_constraint_tuple(field_schema),
                    unit=unit,
                    unit_evidence=unit_evidence,
                    code_values=code_values,
                    code_domain_status=code_status,
                    null_semantics=null_semantics,
                    physical_table=_TABLE_BY_CONTAINER[container],
                    physical_column=TAG_TO_PHYSICAL_COLUMN.get(tag),
                    legacy_source_fields=legacy_sources,
                    transition_note=transition_note,
                    transition_evidence_status=transition_status,
                    calculation_status=_CALCULATION_BY_TAG.get(
                        tag, "reported_or_transitioned_not_marked_fhwa_calculated"
                    ),
                    submission_reportable=tag not in DO_NOT_REPORT_FIELDS,
                    temporal_semantics=_TEMPORAL_BY_TAG.get(
                        tag,
                        "value describes the bridge inventory snapshot identified externally by data_year",
                    ),
                    cautions=_field_cautions(tag, code_values),
                    definition_provenance_ids=provenance_ids,
                )
            )

    fields.sort(key=lambda field: (field.container, field.tag))
    expected_schema_tags = {
        tag
        for _container, properties, _required, _logical_key in _containers(root_items)
        for tag in properties
    }
    actual_tags = {field.tag for field in fields}
    if actual_tags != expected_schema_tags:
        raise ValueError("SNBI dictionary does not cover every schema tag exactly once")
    return SnbiDictionary(
        definition_version=DICTIONARY_VERSION,
        schema_artifact_id=SNBI_SCHEMA_ARTIFACT_ID,
        schema_sha256=SNBI_SCHEMA_SHA256,
        schema_dialect=str(schema.get("$schema") or "unavailable"),
        schema_execution_status=(
            "unsupported_dialect_fail_closed; metadata extraction only"
        ),
        fields=tuple(fields),
        provenance=(schema_provenance, *transition_provenance),
    )


DEFAULT_SNBI_DICTIONARY = build_snbi_dictionary()


__all__ = [
    "DEFAULT_SNBI_DICTIONARY",
    "DICTIONARY_VERSION",
    "DictionaryProvenance",
    "SNBI_SCHEMA_ARTIFACT_ID",
    "SNBI_SCHEMA_SHA256",
    "SchemaConstraint",
    "SnbiDictionary",
    "SnbiFieldDefinition",
    "build_snbi_dictionary",
]
