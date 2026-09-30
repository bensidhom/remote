from __future__ import annotations

from dataclasses import dataclass
from threading import Event

import pytest

from app.services.query import http_service, kpi_bridge
from app.services.query.sql_ast import QueryScope


@dataclass
class Request:
    question: str
    scope: QueryScope = QueryScope(
        workspace_id="workspace-a",
        state_code=34,
        time_mode="latest",
    )
    cancellation_token: Event = Event()
    request_id: str = "12345678-1234-4234-9234-123456789abc"
    model: str = "ignored"
    surface: str = "full"
    row_limit: int = 200
    actor_id: str = "viewer@example.com"
    actor_role: str = "viewer"


@pytest.mark.parametrize(
    "question",
    [
        "What is the total deck area of bridges in Good condition aged between 50 and 54?",
        "What is the total deck area of bridges in Good condition that are between 50 and 54 years old?",
        "How many Poor bridges are aged 75-79?",
        "For the state-maintained population, how much Lower Fair deck area is in age bin 60 through 64?",
        "Show the KPI deck area by age and condition.",
        "Show the KPI age-by-condition bridge counts.",
        "How many bridges have Good deck condition?",
        "What percentage of rated bridges have Fair superstructure condition?",
        "How many bridges have Poor substructure condition?",
        "KPI count of Good culverts",
        "What percentage of NJDOT-maintained bridges at least 20 ft are in acceptable condition?",
        "What is the acceptable deck area KPI for state-maintained bridges?",
        "For the NHS deck condition KPI, what is the Good deck area?",
        "What percentage of NHS deck area is Poor?",
        "How many bridges are in the Fair FHWA TPM diagnostic population?",
        "Under the historical trend scenario, how many bridges are projected Poor in 2035?",
        "Under no rehabilitation, project Lower Fair bridges in 2030.",
        "Show the future condition KPI scenarios.",
        "What is the KPI total bridge count?",
        "What is the KPI total bridge deck area?",
        "What is the KPI average bridge age?",
        "How many high-traffic bridges are in the KPI?",
        "How many bridges are Good in the operational KPI?",
        "What percentage of bridges are Poor in the KPI?",
        "Show KPI bridge counts by county.",
        "Show KPI Good bridge counts by New Jersey county.",
        "What is the program KPI for in-house bridge inspections?",
        "What is the FY 2025 program KPI for county bridge safety inspections?",
        "Show the program KPI for bridges completed.",
        "What is the lane miles under construction KPI actual?",
        "What is the right-of-way acquisition cost program KPI?",
    ],
)
def test_thirty_dashboard_questions_are_recognized(question):
    intent, clarification = kpi_bridge._recognize(question)
    assert intent is not None or clarification is not None


@pytest.mark.parametrize(
    "question",
    [
        "What is the deck area of bridge 1234567?",
        "What is the total deck area of state-owned bridges?",
        "Rank counties by average deck area for county-owned bridges.",
        "List bridges with element quantity in condition state four.",
        "Which bridges had major superstructure rehabilitation?",
        "Show vertical clearance for bridges carrying Route 1.",
        "Count concrete decks with AADT above 20,000.",
        "List bridge names and owners in Bergen County.",
        "Which bridges have a legal load rating below 1.0?",
        "What is the latest work year for each Poor bridge?",
    ],
)
def test_non_kpi_questions_stay_on_existing_bridgette_path(question):
    intent, clarification = kpi_bridge._recognize(question)
    assert intent is None
    assert clarification is None


def test_age_condition_answer_uses_governed_dashboard_value(monkeypatch):
    monkeypatch.setattr(
        kpi_bridge,
        "compute_age_condition_area",
        lambda **_kwargs: {
            "year": 2025,
            "age_reference_year": 2024,
            "reporting_date": "June 15",
            "inventory_age_reference_year": 2025,
            "population": "statewide",
            "data_generation": 12,
            "schema_generation": 18,
            "definition_version": "age-v1",
            "revision_set_sha256": "a" * 64,
            "series": [
                {
                    "age_bin": "50-54",
                    "Good": 548134.0,
                    "Upper Fair": 3867703.0,
                    "Lower Fair": 5846821.4,
                    "Poor": 1225142.1,
                    "Unknown": 0.0,
                    "total_ft2": 11487800.5,
                }
            ],
            "inventory_year_series": [
                {
                    "age_bin": "50-54",
                    "Good": 428656.5,
                    "Upper Fair": 0.0,
                    "Lower Fair": 0.0,
                    "Poor": 0.0,
                    "Unknown": 0.0,
                    "total_ft2": 428656.5,
                }
            ],
        },
    )

    events = list(
        kpi_bridge.stream_bridgette_kpi_query(
            database_path="unused.sqlite3",
            workspace_id="workspace-a",
            request=Request(
                "What is the total deck area of bridges in Good condition aged between 50 and 54?"
            ),
        )
    )

    assert [event["type"] for event in events] == ["evidence", "token"]
    assert events[0]["answer_mode"] == "verified_kpi"
    assert events[0]["rows"] == [
        [2025, "Governed KPI reporting", "June 15", 2024, "statewide", "50-54", "Good", 548134.0],
        [2025, "Inventory-year age", None, 2025, "statewide", "50-54", "Good", 428656.5],
    ]
    assert events[0]["kpi_plan"] == {
        "family": "age_condition",
        "measure": "deck_area",
        "population": "statewide",
        "condition": "Good",
        "age_bin": "50-54",
    }
    assert "548,134.0" in events[1]["content"]
    assert "428,656.5" in events[1]["content"]
    assert "June 15" in events[1]["content"]


def test_natural_between_ages_wording_keeps_kpi_condition_and_age_filters():
    intent, clarification = kpi_bridge._recognize(
        "What is the total deck area of bridges in Good condition that are between 50 and 54 years old?"
    )

    assert clarification is None
    assert intent is not None
    assert intent.as_dict() == {
        "family": "age_condition",
        "measure": "deck_area",
        "population": "statewide",
        "condition": "Good",
        "age_bin": "50-54",
    }


def test_model_kpi_intent_is_validated_without_question_wording_rules():
    intent = kpi_bridge.intent_from_model_payload(
        {
            "route": "kpi",
            "kpi_intent": {
                "family": "age_condition",
                "measure": "deck_area",
                "population": "statewide",
                "condition": "Good",
                "component": "",
                "age_bin": "50-54",
                "county": "",
                "scenario": "",
                "projection_year": None,
                "inventory_year": None,
                "program_measure_id": "",
            },
        }
    )

    assert intent is not None
    assert intent.as_dict() == {
        "family": "age_condition",
        "measure": "deck_area",
        "population": "statewide",
        "condition": "Good",
        "age_bin": "50-54",
    }


def test_future_age_question_does_not_drop_scenario_or_age_facets():
    intent, clarification = kpi_bridge._recognize(
        "Under no rehabilitation, how many Poor bridges aged 50-54 are projected in 2035?"
    )

    assert clarification is None
    assert intent is not None
    assert intent.family == "future_condition"
    assert intent.scenario == "no_rehabilitation"
    assert intent.condition == "Poor"
    assert intent.age_bin == "50-54"
    assert intent.projection_year == 2035


def test_kpi_condition_definition_returns_the_dashboard_rating_bands():
    events = list(
        kpi_bridge.stream_bridgette_kpi_query(
            database_path="unused.sqlite3",
            workspace_id="workspace-a",
            request=Request(
                "How are Good, Upper Fair, Lower Fair, and Poor classified in the KPI?"
            ),
        )
    )

    assert events[0]["rows"] == [
        ["Good", "7-9", "Official Good class"],
        ["Upper Fair", "6", "Fair split used in age and future charts"],
        ["Lower Fair", "5", "Fair split used in age and future charts"],
        ["Poor", "0-4", "Official Poor class"],
        ["Unknown", "Missing or invalid", "Reported separately; never coerced"],
    ]
    assert "Good = 7-9" in events[1]["content"]


def test_legacy_kpi_fallback_runs_before_legacy_query_engine(monkeypatch):
    evidence = {
        "type": "evidence",
        "sql": "",
        "columns": ["value"],
        "rows": [[1]],
        "count": 1,
        "truncated": False,
        "scope": {},
        "catalog_version": "kpi-test",
        "schema_generation": 1,
        "data_generation": 1,
    }
    monkeypatch.setattr(
        http_service,
        "stream_bridgette_kpi_query",
        lambda **_kwargs: iter([evidence, {"type": "token", "content": "KPI answer"}]),
    )
    monkeypatch.setattr(
        http_service,
        "_stream_agentic_bridge_query",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("agentic path must not run")),
    )
    monkeypatch.setenv("BRIDGETTE_QUERY_ENGINE", "legacy")

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="unused.sqlite3",
            workspace_id="workspace-a",
            request=Request("Show the KPI total bridge count"),
        )
    )

    assert events == [evidence, {"type": "token", "content": "KPI answer"}]
