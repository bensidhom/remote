"""Compare paired Bridgette question-bank runs for planning and raw SQL modes.

This report intentionally separates response coverage from semantic safety.  A mode
does not receive correctness credit merely because it returned an ``answer`` event.
The deterministic behavior score comes from the versioned question bank, while a
small, explicit manual-audit table records semantic defects visible in generated
SQL or in the selected reviewed metric.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


MANUAL_AUDIT: dict[str, dict[str, str]] = {
    "snbi.016": {
        "assessment": "Planning is semantically correct",
        "reason": (
            "Planning used the reviewed inventory.total_bridge_length metric over "
            "total_bridge_length (1,318,342.1 ft). Raw SQL summed the nearby "
            "nbis_bridge_length field (1,292,176.4 ft)."
        ),
    },
    "snbi.022": {
        "assessment": "Raw SQL is more correct on this case",
        "reason": (
            "Raw SQL retained the Bergen County filter and returned 496. The planning "
            "plan dropped the county filter and returned the statewide concrete-deck "
            "count of 5,456."
        ),
    },
    "snbi.041": {
        "assessment": "Planning is semantically correct",
        "reason": (
            "Planning used the reviewed controlling Good condition class. Raw SQL "
            "mistook approach_roadway_alignment = 'G' for bridge condition."
        ),
    },
    "snbi.042": {
        "assessment": "Planning is semantically safer",
        "reason": (
            "Planning used the reviewed controlling Fair condition class; raw SQL "
            "reduced Fair to a deck-only rating test."
        ),
    },
    "snbi.043": {
        "assessment": "Planning is semantically safer",
        "reason": (
            "Planning used the reviewed controlling Poor condition class; raw SQL "
            "reduced Poor to a deck-only rating test."
        ),
    },
    "snbi.044": {
        "assessment": "Planning is semantically correct",
        "reason": (
            "Planning used unknown controlling condition. Raw SQL substituted a null "
            "legal-load-rating factor, which is a different concept."
        ),
    },
    "snbi.045": {
        "assessment": "Planning is semantically correct",
        "reason": (
            "Planning used the reviewed Good bridge percentage. Raw SQL used the "
            "unpopulated lowest_condition_rating_code field and returned 0%."
        ),
    },
    "snbi.046": {
        "assessment": "Planning is semantically correct",
        "reason": (
            "Planning used the reviewed Fair bridge percentage. Raw SQL mistook "
            "approach_roadway_alignment = 'F' for Fair bridge condition."
        ),
    },
    "snbi.047": {
        "assessment": "Planning is semantically safer",
        "reason": (
            "Planning used the reviewed controlling Poor percentage. Raw SQL built an "
            "ad-hoc any-component-equals-4 definition."
        ),
    },
    "snbi.048": {
        "assessment": "Planning is semantically correct",
        "reason": (
            "Planning grouped the reviewed controlling Good class by year. Raw SQL "
            "counted deck rating 7 only."
        ),
    },
    "snbi.049": {
        "assessment": "Planning is semantically correct",
        "reason": (
            "Planning grouped the reviewed controlling Poor percentage by year. Raw "
            "SQL calculated a deck-rating-only percentage."
        ),
    },
    "snbi.055": {
        "assessment": "Planning is semantically safer",
        "reason": (
            "Planning used the reviewed condition-conflict evidence and returned rows. "
            "Raw SQL invented its own comparison and concluded that no conflicts exist."
        ),
    },
    "snbi.086": {
        "assessment": "Planning safely asks for clarification",
        "reason": (
            "Raw SQL tested condition-state-four quantity IS NOT NULL instead of > 0, "
            "then reported 6,794 bridges."
        ),
    },
    "snbi.088": {
        "assessment": "Neither output is reliable",
        "reason": (
            "The bank expects clarification because no reviewed substructure-material "
            "semantic resource exists. Planning substituted the concrete-deck count; "
            "raw SQL returned zero from an unreviewed code interpretation."
        ),
    },
    "snbi.090": {
        "assessment": "Planning safely explains the capability gap",
        "reason": (
            "Planning refused to substitute an incomplete federal TPM formula. Raw SQL "
            "used deck rating 4 and incomplete eligibility/weighting rules, returning null."
        ),
    },
    "snbi.092": {
        "assessment": "Planning safely asks for clarification",
        "reason": "The phrase 'NHS bridges' is ambiguous; raw SQL silently chose one definition.",
    },
    "snbi.093": {
        "assessment": "Planning safely asks for clarification",
        "reason": "The phrase 'bad condition' is ambiguous; raw SQL silently invented an ad-hoc definition.",
    },
    "snbi.094": {
        "assessment": "Planning safely asks for clarification",
        "reason": (
            "The comparison metric/grouping was unspecified. Raw SQL returned a truncated "
            "bridge list and incorrectly narrated Essex as zero."
        ),
    },
    "snbi.095": {
        "assessment": "Planning safely asks for bridge identity",
        "reason": "An unresolved bridge name should not be treated as a definitive zero-row answer.",
    },
    "snbi.096": {
        "assessment": "Planning provides the safer supported alternative",
        "reason": "Paint color is not represented by the reviewed SNBI query surface.",
    },
    "snbi.097": {
        "assessment": "Planning prevents a false factual claim",
        "reason": (
            "Raw SQL converted a non-toll inventory classification into a current toll "
            "price of $0, although the database has no toll-price field."
        ),
    },
    "snbi.098": {
        "assessment": "Planning prevents fabricated evidence",
        "reason": (
            "Raw SQL generated the literal expression 0 AS crash_count and reported zero "
            "crashes even though the database has no crash-history field."
        ),
    },
    "snbi.099": {
        "assessment": "Planning emits the correct safe behavior",
        "reason": (
            "Raw SQL's prose admits live traffic is unavailable, but it still emits an "
            "answer event rather than a supported alternative/abstention."
        ),
    },
    "snbi.100": {
        "assessment": "Planning emits the correct safe behavior",
        "reason": "Current weather is external to the bridge inventory and should produce an abstention with alternatives.",
    },
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _message(result: dict[str, Any]) -> str:
    if result.get("answer_markdown"):
        return str(result["answer_markdown"])
    event = result.get("decision_event") or {}
    if event.get("message"):
        return str(event["message"])
    if result.get("error"):
        return f"ERROR: {result['error']}"
    return "(no response text)"


def _compact(value: str, limit: int = 230) -> str:
    text = re.sub(r"\s+", " ", value).strip().replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _default_assessment(
    expected: str, plan: dict[str, Any], raw: dict[str, Any]
) -> tuple[str, str]:
    p_actual = plan.get("actual_behavior")
    r_actual = raw.get("actual_behavior")
    if expected == "answer":
        if p_actual == "answer" and r_actual == "answer":
            return (
                "Both answered",
                "Both modes returned a direct answer; no deterministic semantic adjudication was available for this case.",
            )
        if p_actual == "answer":
            return "Planning more complete", "Planning answered while raw SQL did not."
        if r_actual == "answer":
            return (
                "Raw SQL more complete; review required",
                "Raw SQL answered while planning did not; direct-answer status alone does not prove semantic correctness.",
            )
        return "Neither answered", "Both modes failed to return the expected direct answer."

    p_match = p_actual == expected
    r_match = r_actual == expected
    if p_match and r_match:
        return "Both handled safely", f"Both modes emitted the expected {expected} behavior."
    if p_match:
        return "Planning handled safely", f"Planning emitted the expected {expected}; raw SQL did not."
    if r_match:
        return "Raw SQL handled safely", f"Raw SQL emitted the expected {expected}; planning did not."
    return "Neither matched safe behavior", f"Neither mode emitted the expected {expected} behavior."


def _percent(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    bank = _load(args.bank)
    plan_run = _load(args.plan)
    raw_run = _load(args.raw)
    bank_cases = {case["id"]: case for case in bank["cases"]}
    plan_cases = {case["id"]: case for case in plan_run["results"]}
    raw_cases = {case["id"]: case for case in raw_run["results"]}
    ids = [case["id"] for case in bank["cases"]]
    if set(ids) != set(plan_cases) or set(ids) != set(raw_cases):
        raise SystemExit("The question bank and paired result files do not contain identical case IDs.")

    expected_answer_ids = [case_id for case_id in ids if bank_cases[case_id]["expected_behavior"] == "answer"]
    expected_safe_ids = [case_id for case_id in ids if bank_cases[case_id]["expected_behavior"] != "answer"]
    plan_answer_coverage = sum(plan_cases[case_id]["actual_behavior"] == "answer" for case_id in expected_answer_ids)
    raw_answer_coverage = sum(raw_cases[case_id]["actual_behavior"] == "answer" for case_id in expected_answer_ids)
    plan_safe = sum(
        plan_cases[case_id]["actual_behavior"] == bank_cases[case_id]["expected_behavior"]
        for case_id in expected_safe_ids
    )
    raw_safe = sum(
        raw_cases[case_id]["actual_behavior"] == bank_cases[case_id]["expected_behavior"]
        for case_id in expected_safe_ids
    )
    plan_duration = sum(float(case.get("duration_seconds") or 0) for case in plan_cases.values())
    raw_duration = sum(float(case.get("duration_seconds") or 0) for case in raw_cases.values())

    paired: list[dict[str, Any]] = []
    assessments: Counter[str] = Counter()
    for case_id in ids:
        expected = bank_cases[case_id]["expected_behavior"]
        assessment, reason = _default_assessment(expected, plan_cases[case_id], raw_cases[case_id])
        if case_id in MANUAL_AUDIT:
            assessment = MANUAL_AUDIT[case_id]["assessment"]
            reason = MANUAL_AUDIT[case_id]["reason"]
        assessments[assessment] += 1
        paired.append(
            {
                "id": case_id,
                "category": bank_cases[case_id]["category"],
                "question": bank_cases[case_id]["question"],
                "expected_behavior": expected,
                "planning": {
                    "actual_behavior": plan_cases[case_id]["actual_behavior"],
                    "actual_code": plan_cases[case_id].get("actual_code"),
                    "duration_seconds": plan_cases[case_id].get("duration_seconds"),
                    "output": _message(plan_cases[case_id]),
                    "evidence": plan_cases[case_id].get("evidence"),
                },
                "raw_sql": {
                    "actual_behavior": raw_cases[case_id]["actual_behavior"],
                    "actual_code": raw_cases[case_id].get("actual_code"),
                    "duration_seconds": raw_cases[case_id].get("duration_seconds"),
                    "output": _message(raw_cases[case_id]),
                    "evidence": raw_cases[case_id].get("evidence"),
                },
                "assessment": assessment,
                "reason": reason,
                "manually_audited": case_id in MANUAL_AUDIT,
            }
        )

    summary = {
        "case_count": len(ids),
        "expected_behavior_counts": dict(Counter(bank_cases[case_id]["expected_behavior"] for case_id in ids)),
        "planning": {
            "actual_behavior_counts": dict(Counter(plan_cases[case_id]["actual_behavior"] for case_id in ids)),
            "behavior_match_count": sum(bool(plan_cases[case_id]["behavior_match"]) for case_id in ids),
            "intended_answer_coverage_count": plan_answer_coverage,
            "intended_answer_count": len(expected_answer_ids),
            "intended_answer_coverage_percent": _percent(plan_answer_coverage, len(expected_answer_ids)),
            "safe_behavior_count": plan_safe,
            "safe_behavior_expected_count": len(expected_safe_ids),
            "safe_behavior_percent": _percent(plan_safe, len(expected_safe_ids)),
            "total_duration_seconds": round(plan_duration, 3),
            "mean_duration_seconds": round(plan_duration / len(ids), 3),
        },
        "raw_sql": {
            "actual_behavior_counts": dict(Counter(raw_cases[case_id]["actual_behavior"] for case_id in ids)),
            "behavior_match_count": sum(bool(raw_cases[case_id]["behavior_match"]) for case_id in ids),
            "intended_answer_coverage_count": raw_answer_coverage,
            "intended_answer_count": len(expected_answer_ids),
            "intended_answer_coverage_percent": _percent(raw_answer_coverage, len(expected_answer_ids)),
            "safe_behavior_count": raw_safe,
            "safe_behavior_expected_count": len(expected_safe_ids),
            "safe_behavior_percent": _percent(raw_safe, len(expected_safe_ids)),
            "total_duration_seconds": round(raw_duration, 3),
            "mean_duration_seconds": round(raw_duration / len(ids), 3),
        },
        "assessment_counts": dict(assessments),
        "manually_audited_case_count": len(MANUAL_AUDIT),
        "recommendation": (
            "Keep planning mode as the production default. Raw SQL has higher direct-answer "
            "coverage and slightly lower latency, but it must not be enabled as an unrestricted "
            "fallback because the paired run exposed silent field substitutions, ambiguous-term "
            "assumptions, and fabricated evidence. Improve planning coverage and add narrowly "
            "reviewed deterministic fallbacks for validated intents."
        ),
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bank": str(args.bank),
                "planning_results": str(args.plan),
                "raw_sql_results": str(args.raw),
                "summary": summary,
                "paired_results": paired,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Bridgette planning mode vs raw SQL: 100-question comparison",
        "",
        "The same versioned SNBI question bank and the same database copy were used for both runs. "
        "The bank expects 81 direct answers, 14 clarifications, and 5 abstentions. A returned answer "
        "is counted as coverage, not automatically as correctness.",
        "",
        "## Result",
        "",
        "Raw SQL is more complete by direct-answer coverage, but planning mode is much safer and more "
        "semantically reliable. Planning should remain the production default; raw SQL should only be "
        "used behind narrow reviewed templates and post-query validation.",
        "",
        "| Metric | Planning mode | Raw SQL mode |",
        "|---|---:|---:|",
        f"| Direct answers across all 100 | {plan_run['summary']['behavior_counts'].get('answer', 0)} | {raw_run['summary']['behavior_counts'].get('answer', 0)} |",
        f"| Expected-answer cases directly answered | {plan_answer_coverage}/81 ({_percent(plan_answer_coverage, 81)}%) | {raw_answer_coverage}/81 ({_percent(raw_answer_coverage, 81)}%) |",
        f"| Correct clarification/abstention behavior | {plan_safe}/19 ({_percent(plan_safe, 19)}%) | {raw_safe}/19 ({_percent(raw_safe, 19)}%) |",
        f"| Bank behavior match | {summary['planning']['behavior_match_count']}/100 | {summary['raw_sql']['behavior_match_count']}/100 |",
        f"| Total time | {plan_duration:.3f}s | {raw_duration:.3f}s |",
        f"| Mean time/question | {plan_duration / 100:.3f}s | {raw_duration / 100:.3f}s |",
        "",
        "The raw SQL behavior-match total is slightly higher only because its additional direct answers "
        "outnumber its safe-behavior failures. It is not a 71% factual-correctness score. In the 19 cases "
        "that deliberately require clarification or abstention, planning handled 18 safely; raw SQL "
        "handled only 1 safely.",
        "",
        "## High-impact audited defects",
        "",
        "- Raw SQL confused code letters with meanings: `approach_roadway_alignment = 'G'` became Good "
        "bridge condition, and `= 'F'` became Fair condition.",
        "- Raw SQL fabricated crash evidence with `0 AS crash_count`, then claimed the bridges had zero crashes.",
        "- Raw SQL converted a non-toll inventory code into a current toll price of $0, although no toll-price field exists.",
        "- Raw SQL silently invented definitions for ambiguous phrases such as “bad condition” and “NHS bridges.”",
        "- Planning dropped the Bergen County filter in one concrete-deck query, proving that validated plans "
        "still need intent-to-plan constraint checks.",
        "- Planning also substituted the concrete-deck metric for a reinforced-concrete substructure question; "
        "unsupported reviewed concepts must be blocked before plan generation.",
        "",
        "## Per-question comparison",
        "",
        "Full answer text, SQL/evidence, timing, and decision metadata for every row are in the companion JSON report.",
        "",
        "| ID | Question | Expected | Planning output | Raw SQL output | Assessment |",
        "|---|---|---|---|---|---|",
    ]
    for item in paired:
        plan_label = f"{item['planning']['actual_behavior']}: {_compact(item['planning']['output'])}"
        raw_label = f"{item['raw_sql']['actual_behavior']}: {_compact(item['raw_sql']['output'])}"
        assessment = f"**{item['assessment']}** — {_compact(item['reason'], 260)}"
        lines.append(
            f"| {item['id']} | {_compact(item['question'], 160)} | {item['expected_behavior']} | "
            f"{plan_label} | {raw_label} | {assessment} |"
        )

    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            summary["recommendation"],
            "",
            "The priority is to raise planning coverage from 48/81 without weakening its safe handling. "
            "The best path is to fix plan-generation retries/timeouts, verify every requested constraint "
            "survives into the canonical plan, expand reviewed metrics/resources, and add deterministic "
            "fallback templates only for intents whose field mappings and definitions are validated.",
            "",
        ]
    )
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
