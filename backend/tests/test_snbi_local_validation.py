from __future__ import annotations

from app.routers.services.snbi_validation import validate_snbi_payload


def test_integer_like_numeric_code_is_valid_but_decimal_code_is_not():
    validation = validate_snbi_payload(
        [{"BL01": 34, "BID01": "CODE-1", "BC01": 6.0, "BC02": 5.5}],
        legacy_flags=[False],
        submission_year=2025,
        issue_limit=1000,
    )
    rules = {issue["rule"] for issue in validation["issues"]}

    assert "BC01-code" not in rules
    assert "BC02-code" in rules
