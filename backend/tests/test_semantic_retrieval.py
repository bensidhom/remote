from __future__ import annotations

import json
from pathlib import Path

from app.services.query.semantic import build_semantic_catalog
from app.services.query.semantic_retrieval import (
    DEFAULT_SEMANTIC_ALIASES,
    RETRIEVAL_VERSION,
    retrieve_semantic_context,
)
from app.services.query.service import MAX_PLAN_CONTEXT_BYTES, build_plan_context
from app.services.query.sql_ast import QueryScope


COMPLEX_BANK = (
    Path(__file__).parents[1]
    / "app"
    / "data"
    / "query"
    / "bridgette_complex_question_bank_v1.json"
)


def _catalog():
    return build_semantic_catalog(
        database_identity="semantic-retrieval-test",
        workspace_id="semantic-retrieval-test",
        schema_generation=1,
        data_generation=1,
    )


def _value_mappings(question: str) -> list[dict[str, object]]:
    result = retrieve_semantic_context(question, catalog=_catalog())
    return result.prompt_payload()["candidate_value_mappings"]


def test_alias_catalog_is_versioned_and_nonempty() -> None:
    assert DEFAULT_SEMANTIC_ALIASES.knowledge_version.endswith("v2")
    assert len(DEFAULT_SEMANTIC_ALIASES.records) >= 30
    assert len(DEFAULT_SEMANTIC_ALIASES.source_sha256) == 64
    assert len(DEFAULT_SEMANTIC_ALIASES.records_by_id) == len(
        DEFAULT_SEMANTIC_ALIASES.records
    )


def test_state_owned_deck_area_resolves_owner_s01_without_limiting_catalog() -> None:
    result = retrieve_semantic_context(
        "What is the total deck area for the bridge state owned?",
        catalog=_catalog(),
    )

    assert result.selected_tags[:4] == ("BG16", "BG05", "BG02", "BCL01")
    assert "inventory.total_deck_area" in result.selected_metric_ids
    mappings = result.prompt_payload()["candidate_value_mappings"]
    assert mappings[0]["field_ids"] == ["owner"]
    assert mappings[0]["canonical_values"] == ["S01"]
    assert "not an execution allowlist" in result.prompt_payload()["role"]


def test_exact_compound_filters_are_emitted_as_mandatory_requirements() -> None:
    payload = retrieve_semantic_context(
        "What is total deck area for state-owned bridges that are operationally Poor?",
        catalog=_catalog(),
    ).prompt_payload()

    requirements = {
        item["record_id"]: item
        for item in payload["exact_semantic_requirements"]
    }
    assert requirements["alias.value.owner.state_transportation_department"][
        "canonical_values"
    ] == ["S01"]
    assert requirements["alias.filter.condition.poor"]["canonical_values"] == [
        "Poor"
    ]


def test_overloaded_snbi_codes_do_not_create_unrelated_exact_requirements() -> None:
    payload = retrieve_semantic_context(
        "List span configurations with steel deck codes S01, S02, or S03.",
        catalog=_catalog(),
    ).prompt_payload()

    requirement_ids = {
        item["record_id"] for item in payload["exact_semantic_requirements"]
    }
    assert "alias.value.owner.all_state_agencies" not in requirement_ids
    assert "alias.value.owner.state_park" not in requirement_ids
    assert "alias.value.owner.state_toll" not in requirement_ids
    assert "alias.value.maintenance.state_transportation_department" not in requirement_ids


def test_state_maintained_resolves_maintenance_instead_of_owner() -> None:
    mappings = _value_mappings(
        "How much deck area is maintained by NJDOT on state-maintained bridges?"
    )

    assert mappings[0]["field_ids"] == ["maintenance_responsibility"]
    assert mappings[0]["snbi_tags"] == ["BCL02"]
    assert mappings[0]["canonical_values"] == ["S01"]


def test_owner_code_aliases_retrieve_exact_reviewed_values() -> None:
    examples = {
        "total area for state toll authority owned bridges": "S03",
        "deck area of county-owned bridges": "L01",
        "how many state park owned bridges are there": "S02",
    }
    for question, expected in examples.items():
        mappings = _value_mappings(question)
        assert mappings[0]["canonical_values"] == [expected], question
        assert mappings[0]["field_ids"] == ["owner"], question


def test_fuzzy_retrieval_recovers_clearance_misspellings() -> None:
    result = retrieve_semantic_context(
        "Show the minimum vertcal clearnce for every carried road.",
        catalog=_catalog(),
    )

    assert "BH13" in result.selected_tags
    assert "highway_min_vertical_clearance" in result.selected_field_ids
    assert any(
        "fuzzy_spelling" in record.match_reasons for record in result.records
    )


def test_plan_context_keeps_complete_catalog_and_adds_ranked_context() -> None:
    context = build_plan_context(
        _catalog(),
        QueryScope("semantic-retrieval-test", 34, "latest"),
        question="Compare state-owned and state-maintained total deck area.",
    )
    payload = json.loads(context)

    assert len(context.encode("utf-8")) <= MAX_PLAN_CONTEXT_BYTES
    assert payload["semantic_retrieval"]["retrieval_version"] == RETRIEVAL_VERSION
    assert payload["semantic_retrieval"]["records"]
    assert payload["snbi_dictionary"]["complete_compact_index"]["field_count"] == 154
    assert len(payload["semantic_catalog"]["resources"]) == 14
    assert payload["execution_contract"]["output"].startswith("The server validates")


def test_retrieval_is_stable_for_identical_knowledge_and_question() -> None:
    first = retrieve_semantic_context(
        "Rank counties by poor bridge count and deck area.", catalog=_catalog()
    )
    second = retrieve_semantic_context(
        "Rank counties by poor bridge count and deck area.", catalog=_catalog()
    )

    assert first.question_sha256 == second.question_sha256
    assert first.knowledge_hash == second.knowledge_hash
    assert first.prompt_payload() == second.prompt_payload()


def test_stratified_complex_questions_have_bounded_retrieval() -> None:
    cases = json.loads(COMPLEX_BANK.read_text(encoding="utf-8"))["cases"]
    catalog = _catalog()
    knowledge_hashes = set()

    selected = cases[::10]
    for case in selected:
        result = retrieve_semantic_context(case["question"], catalog=catalog)
        assert 1 <= len(result.records) <= 28, case["id"]
        assert len(result.selected_tags) <= 18, case["id"]
        knowledge_hashes.add(result.knowledge_hash)

    assert len(cases) == 600
    assert len(selected) == 60
    assert len(knowledge_hashes) == 1
