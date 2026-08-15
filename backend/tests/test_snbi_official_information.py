from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.query.snbi_official_information import (
    DEFAULT_SNBI_INFORMATION,
    INFORMATION_AUTHORITY,
    INFORMATION_VERSION,
    MAX_PROMPT_FIELDS,
    REMOTE_OBSERVATION_STATE,
    VALIDATION_INFORMATION_STATUS,
    VENDORED_VERIFIED_SOURCE_STATE,
    load_snbi_informational_catalog,
)
from app.services.query.semantic import build_semantic_catalog
from app.services.query.service import MAX_QUERY_CONTEXT_BYTES, build_query_context
from app.services.query.sql_ast import QueryScope


ARTIFACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "data"
    / "fhwa"
    / "derived"
    / "snbi_field_information_v1.json"
)


def test_catalog_has_all_unique_fields_and_exact_source_boundary() -> None:
    catalog = DEFAULT_SNBI_INFORMATION

    assert catalog.dataset_version == INFORMATION_VERSION
    assert len(catalog.fields) == 154
    assert len(catalog.fields_by_tag) == 154
    assert tuple(catalog.fields_by_tag) == tuple(sorted(catalog.fields_by_tag))
    assert catalog.may_satisfy_official_validation is False
    assert catalog.runtime_network_required is False

    assert len(catalog.sources) == 5
    assert all(source.authority == "FHWA" for source in catalog.sources)
    source_states = {
        source.source_id: source.artifact_state for source in catalog.sources
    }
    assert source_states["snbi_qanda_feb_2023"] == REMOTE_OBSERVATION_STATE
    assert all(
        source_states[source_id] == VENDORED_VERIFIED_SOURCE_STATE
        for source_id in {
            "snbi_march_2022",
            "nbi_snbi_crosswalk_dec_2023",
            "nbi_snbi_code_mapping",
            "validation_logic_v1_1_dec_2025",
        }
    )
    assert all(
        source.source_url.startswith("https://www.fhwa.dot.gov/")
        for source in catalog.sources
    )
    assert "Source-byte identity does not execute" in catalog.authority_boundary


def test_every_field_resolves_sources_and_has_core_information() -> None:
    catalog = DEFAULT_SNBI_INFORMATION
    source_ids = set(catalog.sources_by_id)

    for field in catalog.fields:
        assert field.format
        assert field.frequency in {"I", "EI", "C"}
        assert field.frequency_meaning
        assert field.specification_locator.startswith("publication page ")
        assert field.transitions
        assert field.validation_notes
        assert set(field.source_ids) <= source_ids
        assert set(field.code_meaning_source_ids) <= source_ids
        assert all(item.source_id in source_ids for item in field.transitions)
        assert all(item.source_locator for item in field.transitions)
        assert all(item.source_id in source_ids for item in field.validation_notes)
        assert all(item.source_locator for item in field.validation_notes)
        assert all(
            item.authority_status == VALIDATION_INFORMATION_STATUS
            for item in field.validation_notes
        )


def test_representative_fields_keep_meanings_lineage_and_applicability() -> None:
    catalog = DEFAULT_SNBI_INFORMATION

    condition = catalog.get("BC01")
    assert condition.title == "Deck Condition Rating"
    assert condition.unit == "ordinal_condition_code"
    assert condition.code_meanings_by_value["5"] == "Fair"
    assert any(item.legacy_item_id == "58" for item in condition.transitions)
    assert any(item.rule_id == "BC01-1" for item in condition.validation_notes)

    nhs = catalog.get("BH03")
    assert nhs.title == "NHS Designation"
    assert nhs.code_meanings_by_value["Y"] == "NHS"
    assert any(item.legacy_item_id == "104" for item in nhs.transitions)

    length = catalog.get("BG01")
    assert length.unit == "foot"
    assert any(item.category == "partial" for item in length.transitions)
    assert any(item.rule_id == "BG01-1" for item in length.validation_notes)

    inspection = catalog.get("BIE02")
    assert inspection.frequency == "EI"
    assert inspection.unit == "calendar_date_yyyymmdd"
    assert any(item.legacy_item_id == "90/93" for item in inspection.transitions)

    vertical = catalog.get("BH13")
    assert vertical.unit == "foot"
    assert any("rounded down" in note for note in vertical.reporting_notes)
    assert any(item.legacy_item_id == "53/54A/54B" for item in vertical.transitions)
    assert any(item.rule_id == "BH13-1" for item in vertical.validation_notes)


def test_multicolumn_and_shared_code_tables_are_not_cross_contaminated() -> None:
    catalog = DEFAULT_SNBI_INFORMATION

    state_codes = catalog.get("BL01").code_meanings_by_value
    assert len(state_codes) == 60
    assert state_codes["34"] == "New Jersey"
    assert state_codes["47"] == "Tennessee"

    foundation_codes = catalog.get("BSB07").code_meanings_by_value
    assert foundation_codes["0"] == "None"
    assert foundation_codes["A01"] == "Admixture – internally sealed"
    assert all("roadside hardware" not in value.casefold() for value in foundation_codes.values())

    roadside_codes = catalog.get("BRH01").code_meanings_by_value
    assert "MYY1..MYY6" in roadside_codes
    assert "A01" not in roadside_codes

    work_codes = catalog.get("BW03").code_meanings_by_value
    assert work_codes["DK2"] == "Major deck rehabilitation."
    assert work_codes["CU7"] == "Culvert coating preserved."
    assert "SP1" not in work_codes["DK1"]


def test_prompt_payload_is_selected_bounded_and_explicitly_non_validating() -> None:
    catalog = DEFAULT_SNBI_INFORMATION
    payload = catalog.prompt_payload(["BH13", "BC01"], max_validation_rules_per_field=2)

    assert payload["selected_tags"] == ["BC01", "BH13"]
    assert payload["authority_status"] == INFORMATION_AUTHORITY
    assert payload["may_satisfy_official_validation"] is False
    assert payload["runtime_network_required"] is False
    assert {item["tag"] for item in payload["selected_fields"]} == {"BC01", "BH13"}
    assert all(len(item["validation_notes"]) <= 2 for item in payload["selected_fields"])
    assert any(item["validation_notes_truncated"] for item in payload["selected_fields"])
    encoded = json.dumps(payload)
    assert "BIE11" not in encoded
    assert "C:\\" not in encoded
    assert "/backend/" not in encoded
    assert VENDORED_VERIFIED_SOURCE_STATE in encoded
    assert '"may_satisfy_official_validation": false' in encoded

    too_many = [field.tag for field in catalog.fields[: MAX_PROMPT_FIELDS + 1]]
    with pytest.raises(ValueError, match="at most"):
        catalog.prompt_payload(too_many)
    with pytest.raises(ValueError, match="between 0 and"):
        catalog.prompt_payload(["BH13"], max_validation_rules_per_field=13)
    with pytest.raises(KeyError):
        catalog.prompt_payload(["BZZ99"])


def test_artifact_hash_tampering_fails_closed(tmp_path: Path) -> None:
    raw = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    raw["fields"][0]["title"] = "tampered"
    tampered = tmp_path / "snbi-info.json"
    tampered.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_snbi_informational_catalog(tampered)


def test_verified_source_claim_must_match_the_fhwa_registry(tmp_path: Path) -> None:
    raw = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    raw["sources"][0]["observed_sha256"] = "0" * 64
    raw.pop("dataset_sha256")
    canonical = json.dumps(
        raw,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    import hashlib

    raw["dataset_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    tampered = tmp_path / "snbi-info-source-mismatch.json"
    tampered.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="differs from verified registry artifact"):
        load_snbi_informational_catalog(tampered)


def test_bridgette_context_gets_only_relevant_bounded_information() -> None:
    catalog = build_semantic_catalog(
        database_identity="test-db",
        workspace_id="information-test-workspace",
        schema_generation=1,
        data_generation=1,
    )
    context = build_query_context(
        catalog,
        QueryScope(
            workspace_id="information-test-workspace",
            state_code=34,
            time_mode="latest",
        ),
        question="Explain BH13 highway minimum vertical clearance and legacy Item 53",
    )
    payload = json.loads(context)
    enrichment = payload["snbi_informational_enrichment"]

    assert len(context.encode("utf-8")) <= MAX_QUERY_CONTEXT_BYTES
    assert "BH13" in enrichment["selected_tags"]
    assert enrichment["may_satisfy_official_validation"] is False
    assert enrichment["authority_status"] == INFORMATION_AUTHORITY
    field = next(item for item in enrichment["selected_fields"] if item["tag"] == "BH13")
    assert field["unit"] == "foot"
    assert any(item["legacy_item_id"] == "53/54A/54B" for item in field["transitions"])
    assert len(field["validation_notes"]) <= 8
    assert "BIE11" not in enrichment["selected_tags"]


def test_coverage_is_exact_and_catalog_views_are_immutable() -> None:
    catalog = DEFAULT_SNBI_INFORMATION
    assert catalog.coverage == {
        "applicability_caution_fields": 139,
        "code_meaning_fields": 68,
        "format_and_frequency_fields": 154,
        "interpretive_note_fields": 141,
        "reporting_note_fields": 143,
        "temporary_code_caution_fields": 18,
        "transition_fields": 154,
        "unit_fields": 71,
        "validation_note_fields": 154,
        "validation_rule_links": 617,
    }
    with pytest.raises(TypeError):
        catalog.fields_by_tag["BC01"] = catalog.get("BC01")  # type: ignore[index]
    with pytest.raises(TypeError):
        catalog.get("BC01").code_meanings_by_value["5"] = "changed"  # type: ignore[index]
