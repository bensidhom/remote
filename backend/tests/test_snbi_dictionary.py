from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path

import pytest

from app.services.conversion.canonical_schema import ARRAY_SPECS, ROUTE_SPEC, TOP_LEVEL_FIELDS
from app.services.query import (
    DEFAULT_SNBI_DICTIONARY,
    SNBI_SCHEMA_ARTIFACT_ID,
    SNBI_SCHEMA_SHA256,
    build_snbi_dictionary,
)


def _schema_tags() -> set[str]:
    tags = set(TOP_LEVEL_FIELDS)
    for spec in ARRAY_SPECS.values():
        tags.update(spec.fields)
    tags.update(ROUTE_SPEC.fields)
    return tags


def test_dictionary_covers_every_canonical_snbi_tag_exactly_once():
    dictionary = DEFAULT_SNBI_DICTIONARY
    assert len(dictionary.fields) == 154
    assert {field.tag for field in dictionary.fields} == _schema_tags()
    assert len(dictionary.fields_by_tag) == len(dictionary.fields)
    assert all(field.physical_table for field in dictionary.fields)
    assert all(field.physical_column for field in dictionary.fields)
    assert all(field.transition_note for field in dictionary.fields)


def test_dictionary_is_derived_from_the_exact_registered_schema_bytes():
    artifact = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "data"
        / "fhwa"
        / "artifacts"
        / "SNBI.Schema.v04.json"
    )
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == SNBI_SCHEMA_SHA256
    assert DEFAULT_SNBI_DICTIONARY.schema_artifact_id == SNBI_SCHEMA_ARTIFACT_ID
    provenance = {
        item.artifact_id: item for item in DEFAULT_SNBI_DICTIONARY.provenance
    }
    assert provenance[SNBI_SCHEMA_ARTIFACT_ID].artifact_state == "vendored_verified"
    assert provenance[SNBI_SCHEMA_ARTIFACT_ID].sha256 == SNBI_SCHEMA_SHA256
    assert provenance["nbi_snbi_crosswalk_dec_2023"].artifact_state == "vendored_verified"
    assert provenance["nbi_snbi_crosswalk_dec_2023"].sha256 == (
        "9b81e0b7c9d808cbee65e8a5436f759e5d68b56665fb416ec51e69c40717d4fc"
    )
    assert provenance["nbi_snbi_code_mapping"].artifact_state == "vendored_verified"
    assert provenance["nbi_snbi_code_mapping"].sha256 == (
        "63f120e71363138a33d8cf1344199d00e363633c22ccc959d9ab49f27b76a475"
    )
    assert all(
        "does not certify" in item.evidence_use
        for item in provenance.values()
        if item.artifact_id != SNBI_SCHEMA_ARTIFACT_ID
    )


def test_schema_definition_constraints_requiredness_and_containers_are_preserved():
    dictionary = DEFAULT_SNBI_DICTIONARY
    bridge_number = dictionary.get("BID01")
    state = dictionary.get("BL01")
    feature_type = dictionary.get("BF01")
    route_designation = dictionary.get("BRT01")
    works = dictionary.get("BW02")

    assert bridge_number.title == "Bridge Number"
    assert bridge_number.required_in_container
    assert bridge_number.container == "Bridge"
    assert bridge_number.json_pointer_pattern == "/BID01"
    assert {item.keyword: item.value for item in bridge_number.constraints} == {
        "maxLength": 15
    }
    assert {item.keyword: item.value for item in state.constraints} == {
        "maximum": 99,
        "minimum": 1,
    }
    assert feature_type.container == "Features"
    assert feature_type.logical_key_fields == ("BF01",)
    assert feature_type.json_pointer_pattern == "/Features/*/BF01"
    assert route_designation.container == "Routes"
    assert route_designation.logical_key_fields == ("BRT01",)
    assert route_designation.json_pointer_pattern == "/Features/*/Routes/*/BRT01"
    assert works.container == "Works"
    assert works.required_in_container


def test_semantics_distinguish_snapshot_time_from_event_and_observation_time():
    dictionary = DEFAULT_SNBI_DICTIONARY
    assert "snapshot" in dictionary.get("BC01").temporal_semantics
    assert "traffic-observation" in dictionary.get("BH11").temporal_semantics
    assert "inspection begin event" in dictionary.get("BIE02").temporal_semantics
    assert "work-event" in dictionary.get("BW02").temporal_semantics
    assert "posting-status change event" in dictionary.get("BPS02").temporal_semantics


def test_transition_units_codes_and_do_not_report_status_are_explicitly_scoped():
    dictionary = DEFAULT_SNBI_DICTIONARY
    clearance = dictionary.get("BH13")
    condition = dictionary.get("BC01")
    derived = dictionary.get("BC13")

    assert clearance.unit == "foot"
    assert clearance.legacy_source_fields == (
        "VERT_CLR_OVER_MT_053",
        "VERT_CLR_UND_REF_054A",
        "VERT_CLR_UND_054B",
    )
    assert "reviewed local transition" in clearance.unit_evidence
    assert condition.code_values >= ("0", "1", "2", "3", "4")
    assert "not_a_complete_pinned_fhwa_code_dictionary" in condition.code_domain_status
    assert derived.calculation_status == "fhwa_calculated_do_not_report"
    assert not derived.submission_reportable
    assert any("do not report" in caution for caution in derived.cautions)


def test_unknown_metadata_remains_unavailable_instead_of_being_invented():
    bridge_name = DEFAULT_SNBI_DICTIONARY.get("BID02")
    assert bridge_name.unit is None
    assert bridge_name.unit_evidence.startswith("unavailable")
    assert bridge_name.code_values == ()
    assert bridge_name.code_domain_status.startswith("unavailable")
    assert bridge_name.legacy_source_fields == ()
    assert "No legacy NBI source" in bridge_name.transition_note


def test_prompt_serialization_is_deterministic_scopable_and_honest_about_authority():
    first = build_snbi_dictionary()
    second = build_snbi_dictionary()
    assert first.dictionary_hash == second.dictionary_hash
    assert first.prompt_text() == second.prompt_text()

    scoped = json.loads(first.prompt_text(["BC13", "BID01"]))
    assert scoped["dictionary_hash"] == first.dictionary_hash
    assert scoped["field_count"] == 2
    assert [field["tag"] for field in scoped["fields"]] == ["BC13", "BID01"]
    assert "unsupported" in scoped["schema_execution_status"]
    assert "hash-verified" in scoped["authority_boundary"]
    assert "C:\\test" not in first.prompt_text()
    with pytest.raises(KeyError):
        first.prompt_text(["NOT_A_TAG"])


def test_dictionary_and_entries_are_immutable():
    dictionary = DEFAULT_SNBI_DICTIONARY
    with pytest.raises(FrozenInstanceError):
        dictionary.schema_sha256 = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        dictionary.get("BL01").title = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        dictionary.fields_by_tag["X"] = dictionary.get("BL01")  # type: ignore[index]
