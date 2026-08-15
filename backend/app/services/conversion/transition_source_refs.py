"""Bind research transition lineage to exact FHWA crosswalk source rows."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from app.standards.fhwa_registry import load_rule_bundle_registry

from .transition_values import TransitionSourceReference, TransitionValue


_APP_ROOT = Path(__file__).resolve().parents[2]
_INFORMATION_PATH = (
    _APP_ROOT / "data" / "fhwa" / "derived" / "snbi_field_information_v1.json"
)
_CROSSWALK_ARTIFACT_ID = "nbi_snbi_crosswalk_dec_2023"
_ELEMENT_XSD_ARTIFACT_ID = "legacy_element_xsd_v1_2013"
_ELEMENT_XSD_BINDING_VERSION = "bridgeguard-element-xsd-lineage-binding-v1"
_ELEMENT_XSD_LINES = {
    "EN": 46,
    "EPN": 61,
    "TOTALQTY": 76,
    "CS1": 91,
    "CS2": 106,
    "CS3": 121,
    "CS4": 136,
}
_SOURCE_ITEM_RE = re.compile(r"_(0*\d{1,3}[A-Z]?)$")
_LEGACY_ITEM_RE = re.compile(r"\d+[A-Z]?")
_CROSSWALK_LOCATOR_RE = re.compile(
    r"^(Crosswalk for (?:Over|Under) Records), row (\d+)$"
)


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _normalize_item(value: str) -> str:
    match = re.fullmatch(r"0*(\d+)([A-Z]?)", value.upper())
    if match is None:
        return value.upper()
    return f"{int(match.group(1))}{match.group(2)}"


def _source_items(value: TransitionValue) -> frozenset[str]:
    items: set[str] = set()
    for source in value.source_fields:
        match = _SOURCE_ITEM_RE.search(source.tag.upper())
        if match is not None:
            items.add(_normalize_item(match.group(1)))
    return frozenset(items)


def _legacy_items(raw: object) -> frozenset[str]:
    return frozenset(
        _normalize_item(value)
        for value in _LEGACY_ITEM_RE.findall(str(raw or "").upper())
    )


def _workbook_locators(source_locator: str) -> tuple[str, str, str]:
    match = _CROSSWALK_LOCATOR_RE.fullmatch(source_locator)
    if match is None:
        raise RuntimeError("FHWA crosswalk source locator is not row-addressable")
    sheet, row = match.groups()
    prefix = f"'{sheet}'!"
    # Workbook v.Dec-2023 columns: B=SNBI data tag, E=legacy item ID,
    # I=developer transition rule/notes.  The workbook hash pins this layout.
    return f"{prefix}B{row}", f"{prefix}E{row}", f"{prefix}I{row}"


def _load_catalog() -> tuple[str, str, Mapping[str, tuple[Mapping[str, Any], ...]]]:
    raw = json.loads(_INFORMATION_PATH.read_text(encoding="utf-8"))
    claimed_hash = str(raw.get("dataset_sha256") or "")
    hash_input = dict(raw)
    hash_input.pop("dataset_sha256", None)
    calculated = hashlib.sha256(_stable_json(hash_input).encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", claimed_hash) or calculated != claimed_hash:
        raise RuntimeError("SNBI field-information source binding failed integrity check")
    version = str(raw.get("dataset_version") or "").strip()
    if not version:
        raise RuntimeError("SNBI field-information dataset version is missing")
    fields: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for value in raw.get("fields") or ():
        if not isinstance(value, Mapping):
            continue
        tag = str(value.get("tag") or "").strip().upper()
        transitions = tuple(
            dict(row)
            for row in (value.get("transition") or ())
            if isinstance(row, Mapping)
            and row.get("source_id") == _CROSSWALK_ARTIFACT_ID
        )
        if tag and transitions:
            fields[tag] = transitions
    return version, claimed_hash, fields


_DATASET_VERSION, _DATASET_SHA256, _TRANSITIONS_BY_TAG = _load_catalog()


@lru_cache(maxsize=1)
def _registry():
    """Verify the immutable local registry once per application process."""

    return load_rule_bundle_registry()


def bind_transition_source_references(value: TransitionValue) -> TransitionValue:
    """Attach exact crosswalk references without asserting verified execution."""

    if any(
        reference.source_artifact_id == _CROSSWALK_ARTIFACT_ID
        for reference in value.source_references
    ):
        return value
    candidates = _TRANSITIONS_BY_TAG.get(value.target_tag.upper(), ())
    if not candidates:
        return value
    source_items = _source_items(value)
    matched = (
        tuple(
            row
            for row in candidates
            if source_items.intersection(_legacy_items(row.get("legacy_item_id")))
        )
        if source_items
        else ()
    )
    # Some application-derived rows cite multiple inputs or a structural source
    # whose tag does not encode an NBI item.  A single target-field crosswalk row
    # remains an unambiguous field locator; multiple unmatched rows are omitted
    # instead of choosing one heuristically.
    selected = matched or (candidates if len(candidates) == 1 else ())
    if not selected:
        return value
    registry = _registry()
    artifact = registry.artifact(_CROSSWALK_ARTIFACT_ID)
    if not artifact.sha256:
        raise RuntimeError("verified FHWA crosswalk SHA-256 is unavailable")
    references_list: list[TransitionSourceReference] = []
    for row in selected:
        source_locator = str(row.get("source_locator") or "")
        field_locator, legacy_item_locator, rule_locator = _workbook_locators(
            source_locator
        )
        references_list.append(TransitionSourceReference(
            source_artifact_id=artifact.id,
            source_artifact_sha256=artifact.sha256,
            source_locator=source_locator,
            field_locator=field_locator,
            legacy_item_locator=legacy_item_locator,
            rule_locator=rule_locator,
            target_tag=value.target_tag,
            legacy_item_id=(
                str(row["legacy_item_id"])
                if row.get("legacy_item_id") is not None
                else None
            ),
            record_kind=(
                str(row["record_kind"])
                if row.get("record_kind") is not None
                else None
            ),
            source_field_tags=tuple(source.tag for source in value.source_fields),
            derived_dataset_version=_DATASET_VERSION,
            derived_dataset_sha256=_DATASET_SHA256,
        ))
    return replace(
        value,
        source_references=tuple((*value.source_references, *references_list)),
    )


def element_schema_source_reference(
    *,
    target_tag: str,
    source_tag: str,
) -> TransitionSourceReference:
    """Return XSD execution provenance for a schema-validated element field."""

    source = str(source_tag).strip().upper()
    if source not in _ELEMENT_XSD_LINES:
        raise ValueError(f"unsupported legacy element XML field: {source_tag}")
    registry = _registry()
    artifact = registry.artifact(_ELEMENT_XSD_ARTIFACT_ID)
    if not artifact.sha256:
        raise RuntimeError("verified legacy element XSD SHA-256 is unavailable")
    line = _ELEMENT_XSD_LINES[source]
    field_locator = f"/xs:schema/xs:element[@name='FHWAElement']//xs:element[@name='{source}']"
    return TransitionSourceReference(
        source_artifact_id=artifact.id,
        source_artifact_sha256=artifact.sha256,
        source_locator=f"FHWAElement.Schema.v1.0.2013-10-03.xsd#line={line}",
        field_locator=field_locator,
        legacy_item_locator=f"FHWAED/{source}",
        rule_locator=field_locator,
        target_tag=str(target_tag).strip().upper(),
        legacy_item_id=source,
        record_kind="element_xml",
        source_field_tags=(source,),
        derived_dataset_version=_ELEMENT_XSD_BINDING_VERSION,
        derived_dataset_sha256=artifact.sha256,
        authority_status="bridgeguard_xsd_execution_verified_not_official_validation",
        reference_kind="xml_schema_validation",
        execution_verified=True,
    )


__all__ = [
    "bind_transition_source_references",
    "element_schema_source_reference",
]
