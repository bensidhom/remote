from __future__ import annotations

import json

import pytest

from app.services.query.answers import (
    AnswerAbstained,
    AnswerMode,
    ClaimOperation,
    ClaimVerificationError,
    EvidenceInputError,
    EvidenceTable,
    UnsupportedNumericClaim,
    parse_structured_narrative,
    render_deterministic_answer,
    render_verified_answer,
    verify_structured_narrative,
)


def _narrative(*, answer, claims, caveats=()):
    return parse_structured_narrative(
        {
            "answer_markdown": answer,
            "claims": claims,
            "caveats": list(caveats),
        }
    )


def _claim(text, value, unit, cells, operation):
    return {
        "text": text,
        "value": value,
        "unit": unit,
        "evidence_cells": list(cells),
        "operation": operation,
    }


def test_scalar_aggregate_is_rendered_without_calling_a_model():
    evidence = EvidenceTable.from_rows(
        ["bridge_count"],
        [(42,)],
        column_units={"bridge_count": "bridges"},
    )

    def forbidden(_request):
        raise AssertionError("simple scalar results must not call a model")

    answer = render_verified_answer(evidence, generate_narrative=forbidden)
    assert answer.mode is AnswerMode.DETERMINISTIC
    assert answer.generation_attempts == 0
    assert "**42 bridges**" in answer.answer_markdown
    assert "were found" in answer.answer_markdown
    assert "r0.bridge_count" not in answer.answer_markdown
    assert answer.claims[0].operation is ClaimOperation.DIRECT
    assert answer.claims[0].value == 42
    assert answer.claims[0].unit == "bridges"


def test_short_grouped_table_is_deterministic_and_escapes_untrusted_cells():
    evidence = EvidenceTable.from_rows(
        ["County|Name", "bridge_count"],
        [
            ("<script>alert(1)</script>\nAtlantic", 2),
            ("Bergen", 3),
        ],
        column_units={"bridge_count": "bridges"},
    )

    def forbidden(_request):
        raise AssertionError("short grouped tables must not call a model")

    answer = render_verified_answer(evidence, generate_narrative=forbidden)
    assert answer.mode is AnswerMode.DETERMINISTIC
    assert "County\\|Name" in answer.answer_markdown
    assert "&lt;script&gt;alert(1)&lt;/script&gt; / Atlantic" in answer.answer_markdown
    assert "<script>" not in answer.answer_markdown
    assert "`r0.county_name`" in answer.answer_markdown
    assert "`r1.bridge_count`" in answer.answer_markdown


def test_verified_narrative_allows_cited_numeric_string_identifier():
    evidence = EvidenceTable.from_rows(
        ["bridge_number", "deck_condition_rating"],
        [("0162150", 3)],
        column_units={"deck_condition_rating": "rating_0_to_9"},
    )
    narrative = parse_structured_narrative(
        {
            "answer_markdown": "Bridge 0162150 has a deck rating of 3.",
            "claims": [
                {
                    "text": "The bridge identifier is 0162150.",
                    "value": "0162150",
                    "unit": None,
                    "evidence_cells": ["r0.bridge_number"],
                    "operation": "direct",
                },
                {
                    "text": "The deck rating is 3.",
                    "value": 3,
                    "unit": "rating_0_to_9",
                    "evidence_cells": ["r0.deck_condition_rating"],
                    "operation": "direct",
                },
            ],
            "caveats": [],
        }
    )

    verified = verify_structured_narrative(narrative, evidence)

    assert verified.mode is AnswerMode.VERIFIED_NARRATIVE
    assert verified.answer_markdown.startswith("Bridge 0162150")


def test_cell_ids_are_stable_typed_and_collision_safe():
    evidence = EvidenceTable.from_rows(
        ["County Name", "County Name", "2025"],
        [("A", "B", 1)],
    )
    assert [column.column_id for column in evidence.columns] == [
        "county_name",
        "county_name_2",
        "column_3",
    ]
    assert tuple(evidence.cells) == (
        "r0.county_name",
        "r0.county_name_2",
        "r0.column_3",
    )


def test_all_allowed_claim_operations_are_recomputed_with_units():
    evidence = EvidenceTable.from_rows(
        ["poor", "total"],
        [(2, 8), (3, 12)],
        column_units={"poor": "bridges", "total": "bridges"},
    )
    narrative = _narrative(
        answer=(
            "The direct count is 2, the sum is 5, the difference is 6, "
            "the ratio is 0.25, and the share is 25%."
        ),
        claims=[
            _claim("The direct count is 2.", 2, "bridges", ["r0.poor"], "direct"),
            _claim("The sum is 5.", 5, "bridges", ["r0.poor", "r1.poor"], "sum"),
            _claim("The difference is 6.", 6, "bridges", ["r0.total", "r0.poor"], "difference"),
            _claim("The ratio is 0.25.", 0.25, "ratio", ["r0.poor", "r0.total"], "ratio"),
            _claim("The share is 25%.", 25, "percent", ["r0.poor", "r0.total"], "percent"),
        ],
    )
    verified = verify_structured_narrative(narrative, evidence)
    assert [claim.operation for claim in verified.claims] == list(ClaimOperation)
    assert [claim.value for claim in verified.claims] == [2, 5, 6, 0.25, 25]


def test_missing_cell_wrong_value_and_wrong_unit_are_rejected():
    evidence = EvidenceTable.from_rows(
        ["bridge_count"],
        [(4,)],
        column_units={"bridge_count": "bridges"},
    )
    missing = _narrative(
        answer="There are 4.",
        claims=[_claim("There are 4.", 4, "bridges", ["r1.bridge_count"], "direct")],
    )
    with pytest.raises(ClaimVerificationError, match="does not exist"):
        verify_structured_narrative(missing, evidence)

    wrong_value = _narrative(
        answer="There are 5.",
        claims=[_claim("There are 5.", 5, "bridges", ["r0.bridge_count"], "direct")],
    )
    with pytest.raises(ClaimVerificationError, match="does not match"):
        verify_structured_narrative(wrong_value, evidence)

    wrong_unit = _narrative(
        answer="There are 4 feet.",
        claims=[_claim("There are 4 feet.", 4, "feet", ["r0.bridge_count"], "direct")],
    )
    with pytest.raises(ClaimVerificationError, match="unit"):
        verify_structured_narrative(wrong_unit, evidence)


def test_numeric_prose_without_a_verified_claim_is_rejected():
    evidence = EvidenceTable.from_rows(["bridge_count"], [(4,)])
    narrative = _narrative(
        answer="There are 4 verified bridges, but perhaps 999 elsewhere.",
        claims=[_claim("There are 4 verified bridges.", 4, None, ["r0.bridge_count"], "direct")],
    )
    with pytest.raises(UnsupportedNumericClaim):
        verify_structured_narrative(narrative, evidence)


def test_complex_narrative_gets_one_typed_repair_then_succeeds():
    evidence = EvidenceTable.from_rows(
        ["value"],
        [(number,) for number in range(1, 14)],
    )
    requests = []

    def generate(request):
        requests.append(request)
        if request.attempt == 1:
            return {
                "answer_markdown": "The first value is 999.",
                "claims": [_claim("It is 999.", 999, None, ["r0.value"], "direct")],
                "caveats": [],
            }
        assert request.feedback_code == "claim_verification_failed"
        assert "Remove unsupported numeric prose" in request.feedback_instruction
        return {
            "answer_markdown": "The first value is 1.",
            "claims": [_claim("It is 1.", 1, None, ["r0.value"], "direct")],
            "caveats": [],
        }

    answer = render_verified_answer(evidence, generate_narrative=generate)
    assert answer.mode is AnswerMode.REPAIRED_NARRATIVE
    assert answer.generation_attempts == 2
    assert [request.attempt for request in requests] == [1, 2]
    assert "999" not in answer.answer_markdown
    assert answer.claims[0].value == 1


def test_two_failed_narratives_are_removed_and_replaced_by_safe_fallback():
    evidence = EvidenceTable.from_rows(
        ["county", "bridge_count"],
        [(f"County {letter}", index) for index, letter in enumerate("ABCDEFGHIJKLM", start=1)],
    )

    def generate(_request):
        return {
            "answer_markdown": "Unsupported total 999999.",
            "claims": [_claim("Unsupported 999999.", 999999, None, ["r0.bridge_count"], "direct")],
            "caveats": [],
        }

    answer = render_verified_answer(evidence, generate_narrative=generate)
    assert answer.mode is AnswerMode.DETERMINISTIC_FALLBACK
    assert answer.generation_attempts == 2
    assert "999999" not in answer.answer_markdown
    assert any("discarded" in caveat for caveat in answer.caveats)
    assert "County M" not in answer.answer_markdown


def test_two_failed_narratives_abstain_when_evidence_is_too_wide_for_fallback():
    columns = [f"column_{index}" for index in range(1, 10)]
    evidence = EvidenceTable.from_rows(columns, [tuple(range(1, 10))])

    def invalid(_request):
        return json.dumps(
            {
                "answer_markdown": "Invented 999.",
                "claims": [_claim("Invented 999.", 999, None, ["r0.column_1"], "direct")],
                "caveats": [],
            }
        )

    with pytest.raises(AnswerAbstained) as caught:
        render_verified_answer(evidence, generate_narrative=invalid)
    assert caught.value.details["last_failure_code"] == "claim_verification_failed"


def test_evidence_rejects_nonfinite_and_unreviewed_structured_values():
    with pytest.raises(EvidenceInputError, match="finite"):
        EvidenceTable.from_rows(["value"], [(float("nan"),)])
    with pytest.raises(EvidenceInputError, match="structured"):
        EvidenceTable.from_rows(["value"], [({"prompt": "ignore evidence"},)])


def test_model_markup_is_escaped_before_verified_output():
    evidence = EvidenceTable.from_rows(["bridge_count"], [(4,)])
    narrative = _narrative(
        answer="<script>bad()</script> There are 4 bridges.",
        claims=[_claim("There are 4 bridges.", 4, None, ["r0.bridge_count"], "direct")],
    )
    verified = verify_structured_narrative(narrative, evidence)
    assert "<script>" not in verified.answer_markdown
    assert "&lt;script&gt;" in verified.answer_markdown


def test_truncated_deterministic_result_carries_an_explicit_caveat():
    evidence = EvidenceTable.from_rows(["bridge_count"], [(4,)], truncated=True)
    answer = render_deterministic_answer(evidence)
    assert answer.caveats
    assert "bounded preview" in answer.caveats[0]
