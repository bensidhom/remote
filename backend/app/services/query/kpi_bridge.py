"""Deterministic Bridgette adapter for the KPI dashboard.

The dashboard computations are the authority for these answers.  This module
recognizes only KPI-shaped questions and returns ``None`` for everything else,
so the general Bridgette planner keeps its existing behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Iterator, Mapping, Protocol

from app.services.kpis_v2.fhwa_tpm import compute_fhwa_tpm_diagnostic
from app.services.kpis_v2.program import list_program_kpis
from app.services.kpis_v2.registry import DEFAULT_KPI_REGISTRY
from app.services.kpis_v2.repository import (
    compute_age_condition_area,
    compute_age_condition_counts,
    compute_future_condition_scenarios,
    compute_operational_kpis,
)
from app.services.query.reference_data import NEW_JERSEY_COUNTY_NAMES


KPI_QUERY_VERSION = "bridgette-kpi-query-v1"
_LOGGER = logging.getLogger(__name__)
_CONDITIONS = ("Good", "Upper Fair", "Lower Fair", "Fair", "Poor", "Unknown")
_COMPONENTS = ("Deck", "Superstructure", "Substructure", "Culvert")
_AGE_BINS = tuple([f"{start}-{start + 4}" for start in range(0, 95, 5)] + ["95-100", ">100"])


class _Scope(Protocol):
    workspace_id: str
    state_code: int | str
    time_mode: object
    bridge_number: str | None
    data_year: int | None
    start_year: int | None
    end_year: int | None


class _Request(Protocol):
    question: str
    scope: _Scope
    cancellation_token: object


@dataclass(frozen=True, slots=True)
class _Intent:
    family: str
    measure: str
    population: str = "statewide"
    condition: str | None = None
    component: str | None = None
    age_bin: str | None = None
    county: str | None = None
    scenario: str | None = None
    projection_year: int | None = None
    inventory_year: int | None = None
    program_measure_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "family": self.family,
                "measure": self.measure,
                "population": self.population,
                "condition": self.condition,
                "component": self.component,
                "age_bin": self.age_bin,
                "county": self.county,
                "scenario": self.scenario,
                "projection_year": self.projection_year,
                "inventory_year": self.inventory_year,
                "program_measure_id": self.program_measure_id,
            }.items()
            if value is not None
        }


_FAMILY_MEASURES: Mapping[str, frozenset[str]] = {
    "definition": frozenset({
        "condition_bands",
        "deck_area_by_age_condition",
        "state_maintained_deck_area_acceptable",
        "state_maintained_20ft_acceptable",
        "average_snapshot_age",
        "deck_area_total",
        "bridge_condition_counts",
        "bridge_count_total",
    }),
    "age_condition": frozenset({"deck_area", "bridge_count"}),
    "fhwa_tpm": frozenset({"deck_area", "bridge_count", "percent"}),
    "future_condition": frozenset({"bridge_count"}),
    "acceptable_condition": frozenset({"deck_area", "bridge_count"}),
    "component_condition": frozenset({"bridge_count", "percent"}),
    "program": frozenset({"program_value"}),
    "operational": frozenset({
        "total_deck_area",
        "average_age",
        "high_traffic_count",
        "county_count",
        "condition_count",
        "condition_percent",
        "total_bridge_count",
    }),
}


def intent_from_model_payload(payload: Mapping[str, object]) -> _Intent | None:
    """Validate one model-produced semantic KPI route.

    The model decides meaning; this function only enforces the finite dashboard
    contract before any calculation runs. A ``database`` route returns ``None``
    so ordinary SNBI questions continue through the general query engine.
    """

    route = str(payload.get("route") or "").strip().casefold()
    if route == "database":
        return None
    if route != "kpi":
        raise ValueError("the semantic route must be 'database' or 'kpi'")

    raw_intent = payload.get("kpi_intent")
    if not isinstance(raw_intent, Mapping):
        raise ValueError("a KPI route requires kpi_intent")
    family = str(raw_intent.get("family") or "").strip()
    measure = str(raw_intent.get("measure") or "").strip()
    allowed_measures = _FAMILY_MEASURES.get(family, frozenset())
    if family == "definition":
        allowed_measures = frozenset({*allowed_measures, *_PROGRAM_ALIASES})
    if family not in _FAMILY_MEASURES or measure not in allowed_measures:
        raise ValueError("the model selected an unsupported KPI family or measure")

    def optional_text(key: str) -> str | None:
        value = raw_intent.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    population = optional_text("population") or "statewide"
    if population not in {"statewide", "state_maintained"}:
        raise ValueError("unsupported KPI population")
    condition = optional_text("condition")
    if condition not in {None, *_CONDITIONS}:
        raise ValueError("unsupported KPI condition")
    component = optional_text("component")
    if component not in {None, *_COMPONENTS}:
        raise ValueError("unsupported KPI component")
    age_bin = optional_text("age_bin")
    if age_bin not in {None, *_AGE_BINS}:
        raise ValueError("unsupported KPI age bin")
    county = optional_text("county")
    if county is not None and county not in set(NEW_JERSEY_COUNTY_NAMES.values()):
        raise ValueError("unsupported New Jersey county")
    scenario = optional_text("scenario")
    if scenario not in {None, "historical_trend", "no_rehabilitation"}:
        raise ValueError("unsupported KPI forecast scenario")
    program_measure_id = optional_text("program_measure_id")
    if program_measure_id is not None and program_measure_id not in _PROGRAM_ALIASES:
        raise ValueError("unsupported program KPI")

    def optional_year(key: str) -> int | None:
        value = raw_intent.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a year")
        year = int(value)
        if year < 1900 or year > 2200:
            raise ValueError(f"{key} is outside the supported range")
        return year

    if family == "age_condition" and age_bin is None:
        # A full age-by-condition series is valid only when the user asked for
        # the series. The model expresses that by leaving condition empty too.
        pass
    if family == "component_condition" and (
        component is None or condition not in {"Good", "Fair", "Poor"}
    ):
        raise ValueError("component-condition KPI requires a component and Good/Fair/Poor")
    if family == "program" and program_measure_id is None:
        raise ValueError("program KPI requires program_measure_id")

    return _Intent(
        family=family,
        measure=measure,
        population=population,
        condition=condition,
        component=component,
        age_bin=age_bin,
        county=county,
        scenario=scenario,
        projection_year=optional_year("projection_year"),
        inventory_year=optional_year("inventory_year"),
        program_measure_id=program_measure_id,
    )


def _normalized(question: str) -> str:
    return " ".join(
        str(question)
        .casefold()
        .replace("–", "-")
        .replace("—", "-")
        .replace("≥", ">=")
        .replace("²", "2")
        .split()
    )


def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def _condition(text: str) -> str | None:
    for label, pattern in (
        ("Upper Fair", r"\bupper[ -]?fair\b"),
        ("Lower Fair", r"\blower[ -]?fair\b"),
        ("Good", r"\bgood\b"),
        ("Fair", r"\bfair\b"),
        ("Poor", r"\bpoor\b"),
        ("Unknown", r"\b(?:unknown|unrated|missing condition)\b"),
    ):
        if _has(text, pattern):
            return label
    return None


def _component(text: str) -> str | None:
    for label in _COMPONENTS:
        if _has(text, rf"\b{label.casefold()}s?\b"):
            return label
    return None


def _population(text: str) -> str:
    if _has(
        text,
        r"\b(?:njdot|state)[ -]maintained\b|\bmaintained by (?:the )?(?:state|njdot)\b",
    ):
        return "state_maintained"
    return "statewide"


def _county(text: str) -> str | None:
    for name in NEW_JERSEY_COUNTY_NAMES.values():
        if name != "Unknown" and _has(text, rf"\b{re.escape(name)}(?:\s+county)?\b"):
            return name
    return None


def _age_bin(text: str) -> tuple[str | None, bool]:
    """Return the dashboard bin and whether an age request was explicit."""

    if _has(text, r"\b(?:over|older than|more than)\s+100\s*(?:years?)?\b"):
        return ">100", True
    patterns = (
        r"\baged?\s+(?:between\s+)?(?P<low>\d{1,3})\s*(?:-|to|through|and)\s*(?P<high>\d{1,3})(?:\s+years?(?:\s+old)?)?\b",
        r"\bage(?:s| band| bin| group| range)?\s+(?:between\s+)?(?P<low>\d{1,3})\s*(?:-|to|through|and)\s*(?P<high>\d{1,3})\b",
        r"\b(?:bridges?\s+)?(?:that\s+are\s+|which\s+are\s+|are\s+)?between\s+(?P<low>\d{1,3})\s+and\s+(?P<high>\d{1,3})\s+years?(?:\s+old)?\b",
        r"\b(?P<low>\d{1,3})\s*(?:-|to|through)\s*(?P<high>\d{1,3})\s*(?:year|years)[ -]old\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match is None:
            continue
        low, high = int(match.group("low")), int(match.group("high"))
        label = f"{low}-{high}"
        return (label if label in _AGE_BINS else None), True
    return None, False


def _inventory_year(text: str) -> int | None:
    match = re.search(
        r"\b(?:inventory|data|snapshot|cy)\s*(?:year\s*)?(20\d{2})\b|"
        r"\b(20\d{2})\s+(?:inventory|snapshot)\b",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return int(next(value for value in match.groups() if value is not None))


def _projection_year(text: str) -> int | None:
    match = re.search(
        r"\b(?:project(?:ed|ion)?(?:\s+year)?|forecast(?:ed)?(?:\s+for)?|in)\s+(20\d{2})\b",
        text,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


_PROGRAM_ALIASES: Mapping[str, tuple[str, ...]] = {
    "inhouse_bridge_inspections_count": ("in-house bridge inspections", "in house bridge inspections"),
    "consultant_bridge_inspections_count": ("consultant bridge inspections",),
    "county_bridge_inspections_count": ("county bridge safety inspections", "county bridge inspections"),
    "planned_projects_awarded_percent": ("planned construction projects awarded", "planned projects awarded"),
    "construction_contracts_awarded_count": ("construction contracts awarded", "contracts awarded count"),
    "construction_contracts_awarded_value": ("construction contracts awarded value", "contract award value"),
    "railroad_grade_crossings_inspected_percent": ("railroad grade crossings inspected", "grade crossings inspected"),
    "row_acquisition_cost": ("row acquisition cost", "right-of-way acquisition cost", "right of way acquisition cost"),
    "bridges_completed_count": ("bridges completed",),
    "lane_miles_under_construction": ("lane-miles under construction", "lane miles under construction"),
    "projects_designed_in_house_value": ("projects designed in-house", "projects designed in house"),
}


def _program_measure(text: str) -> str | None:
    for measure_id, aliases in _PROGRAM_ALIASES.items():
        if any(alias in text for alias in aliases):
            return measure_id
    return None


def _definition_measure(text: str, program_measure: str | None) -> str | None:
    if _has(text, r"\bsplit|\bthreshold|\bclassif|\brating bands?\b|\bupper fair\b|\blower fair\b"):
        return "condition_bands"
    if program_measure is not None:
        return program_measure
    if _has(text, r"\bage[ -]by[ -]condition\b|\bdeck area by age\b"):
        return "deck_area_by_age_condition"
    if _has(text, r"\bacceptable\b") and _has(text, r"\bdeck area\b"):
        return "state_maintained_deck_area_acceptable"
    if _has(text, r"\bacceptable\b"):
        return "state_maintained_20ft_acceptable"
    if _has(text, r"\baverage (?:bridge )?age\b"):
        return "average_snapshot_age"
    if _has(text, r"\bdeck area\b"):
        return "deck_area_total"
    if _has(text, r"\bcondition\b"):
        return "bridge_condition_counts"
    if _has(text, r"\bbridge count\b|\bnumber of bridges\b"):
        return "bridge_count_total"
    return None


def _recognize(question: str) -> tuple[_Intent | None, str | None]:
    text = _normalized(question)
    explicit_kpi = _has(text, r"\bkpis?\b|\bkey performance indicator(?:s)?\b")
    population = _population(text)
    condition = _condition(text)
    component = _component(text)
    age_bin, age_explicit = _age_bin(text)
    inventory_year = _inventory_year(text)
    county = _county(text)
    future_requested = _has(
        text,
        r"\bhistorical trend\b|\bno rehabilitation\b|\bwithout rehabilitation\b|\bfuture condition\b|\bforecast|\bproject",
    )
    federal_requested = _has(
        text,
        r"\b(?:fhwa|tpm|national highway system|nhs)[ -]?(?:designated)?\b",
    )
    component_condition_requested = _has(
        text,
        r"\b(?:deck|superstructure|substructure|culvert)\s+(?:condition|rating)\b",
    )
    program_measure = _program_measure(text)
    definition_requested = _has(
        text,
        r"\bhow (?:is|are|do|does)\b|\bdefinition\b|\bdefined\b|\bdenominator\b|\bnumerator\b|"
        r"\bwhat (?:does|do)\b.*\bmean\b|\bwhat is included\b|\bwhat is excluded\b|\bsource fields?\b",
    )
    if explicit_kpi and definition_requested:
        definition_measure = _definition_measure(text, program_measure)
        if definition_measure is None:
            return None, "Name the KPI whose definition, population, numerator, or denominator you want."
        return _Intent(
            family="definition",
            measure=definition_measure,
            program_measure_id=(program_measure if program_measure == definition_measure else None),
        ), None

    if age_explicit:
        if age_bin is None:
            return None, (
                "The age-by-condition KPI uses the displayed five-year bins "
                "(for example 50-54, 55-59, 95-100, and over 100). "
                "Please choose one of those bins."
            )
        if federal_requested:
            return None, (
                "The NHS deck-condition KPI is not broken down by bridge age. "
                "Ask for the NHS Good/Fair/Poor area or percentage, or use the separate age-by-condition KPI."
            )
        if component_condition_requested:
            return None, (
                "The age-by-condition KPI uses overall controlling bridge condition, not an individual component rating. "
                "Ask for controlling Good/Fair/Poor by age, or ask for the component KPI without an age bin."
            )
        if not future_requested and (
            condition is not None or explicit_kpi or _has(text, r"\bage[ -]by[ -]condition\b")
        ):
            measure = "deck_area" if _has(text, r"\bdeck area\b|\bsquare (?:feet|foot)\b|\bsq\.?\s*ft\b|\bfootage\b") else "bridge_count"
            return _Intent(
                family="age_condition",
                measure=measure,
                population=population,
                condition=condition,
                age_bin=age_bin,
                inventory_year=inventory_year,
            ), None

    if _has(text, r"\bage[ -]by[ -]condition\b|\bdeck area by age(?: and condition)?\b") and explicit_kpi:
        measure = "deck_area" if _has(text, r"\bdeck area\b|\bsquare (?:feet|foot)\b") else "bridge_count"
        return _Intent(
            family="age_condition",
            measure=measure,
            population=population,
            condition=condition,
            inventory_year=inventory_year,
        ), None

    if federal_requested and (
        explicit_kpi
        or _has(text, r"\bfhwa\b|\btpm\b")
        or _has(text, r"\bdeck (?:area|condition)\b|\bcondition measure\b")
    ):
        if condition in {"Upper Fair", "Lower Fair", "Unknown"}:
            return None, "The NHS deck-condition KPI reports only Good, Fair, and Poor. Choose one of those classes."
        if _has(text, r"\bpercent(?:age)?\b|\bshare\b|\brate\b"):
            measure = "percent"
        elif _has(text, r"\b(?:bridge )?count\b|\bhow many\b"):
            measure = "bridge_count"
        else:
            measure = "deck_area"
        return _Intent(
            family="fhwa_tpm",
            measure=measure,
            condition=condition,
            inventory_year=inventory_year,
        ), None

    if future_requested and _has(
        text, r"\bforecast|\bproject|\bfuture|\bscenario|\bhistorical trend\b|\bno rehabilitation\b"
    ):
        scenario = (
            "no_rehabilitation"
            if _has(text, r"\bno rehabilitation\b|\bwithout rehabilitation\b")
            else "historical_trend"
            if _has(text, r"\bhistorical trend\b")
            else None
        )
        return _Intent(
            family="future_condition",
            measure="bridge_count",
            condition=condition,
            age_bin=age_bin,
            scenario=scenario,
            projection_year=_projection_year(text),
            inventory_year=inventory_year,
        ), None

    if _has(text, r"\bacceptable condition\b|\bacceptable deck area\b") and (
        explicit_kpi
        or population == "state_maintained"
        or _has(text, r"\b20\s*(?:feet|foot|ft)\b")
    ):
        measure = "deck_area" if _has(text, r"\bdeck area\b|\bsquare (?:feet|foot)\b") else "bridge_count"
        return _Intent(
            family="acceptable_condition",
            measure=measure,
            population="state_maintained",
            inventory_year=inventory_year,
        ), None

    if component is not None and condition is not None and (
        explicit_kpi
        or _has(text, r"\bhow many\b|\bcount\b|\bnumber of\b|\bpercent(?:age)?\b|\bshare\b")
    ):
        if condition not in {"Good", "Fair", "Poor"}:
            return None, "Component-condition KPIs report Good, Fair, and Poor. Choose one of those classes."
        measure = "percent" if _has(text, r"\bpercent(?:age)?\b|\bshare\b") else "bridge_count"
        return _Intent(
            family="component_condition",
            measure=measure,
            condition=condition,
            component=component,
            inventory_year=inventory_year,
        ), None

    if program_measure is not None and (explicit_kpi or _has(text, r"\b(?:fiscal|fy|actual|target|program)\b")):
        return _Intent(
            family="program",
            measure="program_value",
            program_measure_id=program_measure,
            inventory_year=inventory_year,
        ), None

    if explicit_kpi:
        if _has(text, r"\btotal (?:bridge )?deck area\b|\bvalid bridge deck area\b"):
            measure = "total_deck_area"
        elif _has(text, r"\baverage (?:bridge )?age\b"):
            measure = "average_age"
        elif _has(text, r"\bhigh[ -]traffic\b"):
            measure = "high_traffic_count"
        elif county is not None or _has(text, r"\bcount(?:s)? by county\b|\beach (?:new jersey )?county\b|\bby (?:new jersey )?county\b"):
            measure = "county_count"
        elif condition is not None and _has(text, r"\bcondition\b|\bbridges?\b"):
            measure = "condition_percent" if _has(text, r"\bpercent(?:age)?\b|\bshare\b") else "condition_count"
        elif _has(text, r"\btotal (?:number of )?bridges?\b|\bloaded bridge count\b"):
            measure = "total_bridge_count"
        else:
            return None, (
                "I recognized this as a KPI question, but not which dashboard measure you want. "
                "Specify operational inventory, acceptable condition, age by condition, NHS deck condition, "
                "future condition scenario, or a named program KPI."
            )
        return _Intent(
            family="operational",
            measure=measure,
            condition=condition,
            county=county,
            inventory_year=inventory_year,
        ), None

    return None, None


def _scope_year(intent: _Intent, scope: _Scope) -> int | None:
    if intent.inventory_year is not None:
        return intent.inventory_year
    mode = getattr(scope.time_mode, "value", str(scope.time_mode)).casefold()
    return int(scope.data_year) if mode == "exact" and scope.data_year is not None else None


def _scope_payload(scope: _Scope, *, selected_year: int | None) -> dict[str, object]:
    return {
        "workspace_id": scope.workspace_id,
        "state_code": scope.state_code,
        "bridge_number": scope.bridge_number,
        "time_mode": getattr(scope.time_mode, "value", str(scope.time_mode)),
        "year": selected_year,
        "start_year": scope.start_year,
        "end_year": scope.end_year,
    }


def _number(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)


def _area(value: object) -> str:
    return f"{float(value or 0):,.1f} ft²"


def _percent(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.1f}%"


def _row_result(
    report: Mapping[str, object],
    intent: _Intent,
) -> tuple[list[str], list[list[object]], str, list[str]]:
    warnings = [str(item) for item in report.get("warnings", [])]
    if intent.family == "definition":
        if intent.measure == "condition_bands":
            columns = ["class", "rating_rule", "use"]
            rows = [
                ["Good", "7-9", "Official Good class"],
                ["Upper Fair", "6", "Fair split used in age and future charts"],
                ["Lower Fair", "5", "Fair split used in age and future charts"],
                ["Poor", "0-4", "Official Poor class"],
                ["Unknown", "Missing or invalid", "Reported separately; never coerced"],
            ]
            answer = (
                "The KPI condition bands are **Good = 7-9**, **Upper Fair = 6**, "
                "**Lower Fair = 5**, and **Poor = 0-4**. Missing or invalid ratings are **Unknown**. "
                "For overall bridge condition, BridgeGuard uses the lowest applicable Deck, "
                "Superstructure, Substructure, or Culvert rating."
            )
            return columns, rows, answer, warnings
        definition = DEFAULT_KPI_REGISTRY.get(intent.measure)
        columns = [
            "measure_id",
            "title",
            "purpose",
            "population",
            "numerator",
            "denominator",
            "unit",
            "time_basis",
            "source_fields",
            "status",
        ]
        rows = [[
            definition.measure_id,
            definition.title,
            definition.purpose,
            definition.population,
            definition.numerator,
            definition.denominator,
            definition.unit,
            definition.time_basis,
            ", ".join(definition.source_fields),
            definition.status,
        ]]
        answer = (
            f"**{definition.title}.** {definition.purpose} Population: {definition.population} "
            f"Numerator: {definition.numerator} Denominator: {definition.denominator}"
        )
        return columns, rows, answer, warnings
    year = int(report["year"])

    if intent.family == "age_condition":
        series = report.get("series")
        if not isinstance(series, list):
            raise RuntimeError("the KPI age series is unavailable")
        if intent.age_bin is None:
            if intent.condition is not None:
                columns = ["inventory_year", "age_reference_year", "population", "age_bin", "condition", "value"]
                rows = []
                for item in series:
                    if not isinstance(item, Mapping):
                        continue
                    if intent.condition == "Fair" and intent.measure == "deck_area":
                        value = float(item.get("Upper Fair", 0) or 0) + float(item.get("Lower Fair", 0) or 0)
                    else:
                        value = item.get(intent.condition, 0)
                    rows.append([year, report.get("age_reference_year"), intent.population, item.get("age_bin"), intent.condition, value])
            else:
                value_columns = ["Good", "Upper Fair", "Lower Fair", "Poor"]
                if any(isinstance(item, Mapping) and "Unknown" in item for item in series):
                    value_columns.append("Unknown")
                total_column = "total_ft2" if intent.measure == "deck_area" else "total_bridges"
                columns = ["inventory_year", "age_reference_year", "population", "age_bin", *value_columns, total_column]
                rows = [
                    [year, report.get("age_reference_year"), intent.population, item.get("age_bin"), *[item.get(key, 0) for key in value_columns], item.get(total_column, 0)]
                    for item in series
                    if isinstance(item, Mapping)
                ]
            unit = "deck-area" if intent.measure == "deck_area" else "bridge-count"
            answer = (
                f"The {year} KPI {unit} age-by-condition series uses reference year "
                f"{report.get('age_reference_year')} and is shown in the result table. "
                f"Inventory year {year} selects the records; bridge age is calculated as "
                f"{report.get('age_reference_year')} minus year built to match the dashboard cycle."
            )
            return columns, rows, answer, warnings
        selected = next(
            (row for row in series if isinstance(row, Mapping) and row.get("age_bin") == intent.age_bin),
            None,
        )
        if selected is None:
            raise RuntimeError("the requested KPI age bin is unavailable")
        comparison_series = report.get("inventory_year_series")
        inventory_selected = next(
            (
                row
                for row in comparison_series
                if isinstance(row, Mapping) and row.get("age_bin") == intent.age_bin
            ),
            None,
        ) if isinstance(comparison_series, list) else selected
        if inventory_selected is None:
            inventory_selected = selected
        condition = intent.condition

        def selected_value(item: Mapping[str, object], *, area: bool) -> object:
            if area:
                if condition == "Fair":
                    return float(item.get("Upper Fair", 0) or 0) + float(item.get("Lower Fair", 0) or 0)
                if condition is None:
                    return item.get("total_ft2", 0)
                return item.get(condition, 0)
            return item.get(condition or "total_bridges", 0)

        reporting_date = str(report.get("reporting_date") or "June 15")
        reporting_reference_year = report.get("age_reference_year")
        inventory_reference_year = report.get("inventory_age_reference_year", year)
        if intent.measure == "deck_area":
            value = selected_value(selected, area=True)
            inventory_value = selected_value(inventory_selected, area=True)
            columns = [
                "inventory_year", "basis", "reporting_date", "age_reference_year",
                "population", "age_bin", "condition", "deck_area_ft2",
            ]
            rows = [
                [year, "Governed KPI reporting", reporting_date, reporting_reference_year, intent.population, intent.age_bin, condition or "All", value],
                [year, "Inventory-year age", None, inventory_reference_year, intent.population, intent.age_bin, condition or "All", inventory_value],
            ]
            answer = (
                f"Using the same {year} inventory records and deck-area formula, "
                f"{intent.population.replace('_', ' ')} bridges in {condition or 'all condition classes'} "
                f"and age band {intent.age_bin} have **{_area(value)}** under the governed KPI/reporting "
                f"basis (reporting date {reporting_date}; age = {reporting_reference_year} minus year built), "
                f"and **{_area(inventory_value)}** when age is measured directly as "
                f"{inventory_reference_year} minus year built. The totals differ only because some bridges "
                "move between five-year age bands; the bridge inventory and deck-area calculation are unchanged."
            )
        else:
            value = selected_value(selected, area=False)
            inventory_value = selected_value(inventory_selected, area=False)
            columns = [
                "inventory_year", "basis", "reporting_date", "age_reference_year",
                "population", "age_bin", "condition", "bridge_count",
            ]
            rows = [
                [year, "Governed KPI reporting", reporting_date, reporting_reference_year, intent.population, intent.age_bin, condition or "All", value],
                [year, "Inventory-year age", None, inventory_reference_year, intent.population, intent.age_bin, condition or "All", inventory_value],
            ]
            answer = (
                f"Using the same {year} inventory records, there are **{_number(value)}** "
                f"{intent.population.replace('_', ' ')} bridges in {condition or 'all condition classes'} "
                f"and age band {intent.age_bin} under the governed KPI/reporting basis "
                f"(reporting date {reporting_date}; age = {reporting_reference_year} minus year built), "
                f"and **{_number(inventory_value)}** when age is measured directly as "
                f"{inventory_reference_year} minus year built. The counts differ only because some bridges "
                "move between five-year age bands; the inventory population is unchanged."
            )
        return columns, rows, answer, warnings

    if intent.family == "fhwa_tpm":
        condition = intent.condition
        conditions = [condition] if condition in {"Good", "Fair", "Poor"} else ["Good", "Fair", "Poor"]
        areas = report["areas_ft2"]
        counts = report["bridge_counts"]
        rows = []
        for label in conditions:
            key = label.casefold()
            rows.append([
                year,
                label,
                float(areas[key]),  # type: ignore[index]
                counts[key],  # type: ignore[index]
                float(report[f"{key}_deck_area_percent"]),
            ])
        columns = ["inventory_year", "condition", "deck_area_ft2", "bridge_count", "deck_area_percent"]
        if len(rows) == 1:
            answer = (
                f"In the {year} FHWA TPM diagnostic population, **{rows[0][1]}** bridges account for "
                f"**{_area(rows[0][2])}** ({_percent(rows[0][4])}) across **{_number(rows[0][3])}** bridges."
            )
        else:
            answer = (
                f"The {year} FHWA TPM diagnostic includes **{_number(report['population']['included_bridge_count'])}** bridges "  # type: ignore[index]
                f"and **{_area(report['total_applicable_deck_area_ft2'])}** of applicable deck area. "
                + "; ".join(f"{row[1]}: {_area(row[2])} ({_percent(row[4])})" for row in rows)
                + "."
            )
        return columns, rows, answer, warnings

    if intent.family == "future_condition":
        scenarios = report.get("scenarios")
        if not isinstance(scenarios, list):
            raise RuntimeError("the KPI scenario series is unavailable")
        selected_scenarios = [
            row for row in scenarios
            if isinstance(row, Mapping) and (intent.scenario is None or row.get("scenario_id") == intent.scenario)
        ]
        projection_year = intent.projection_year or year + int(report.get("horizon_years", 10))
        rows = []
        for scenario in selected_scenarios:
            if intent.age_bin is None:
                point = next((row for row in scenario["series"] if int(row["year"]) == projection_year), None)  # type: ignore[index]
            else:
                age_year = next((row for row in scenario["age_series"] if int(row["year"]) == projection_year), None)  # type: ignore[index]
                point = next(
                    (
                        row
                        for row in age_year["age_bins"]
                        if row.get("age_bin") == intent.age_bin
                    ),
                    None,
                ) if age_year is not None else None
            if point is None:
                raise RuntimeError("the requested projection year is outside the available KPI horizon")
            conditions = [intent.condition] if intent.condition else ["Good", "Upper Fair", "Lower Fair", "Fair", "Poor", "All"]
            for condition in conditions:
                key = "total_bridges" if condition == "All" else str(condition)
                rows.append([year, projection_year, scenario["label"], intent.age_bin or "All", condition, point.get(key, 0)])
        columns = ["inventory_year", "projection_year", "scenario", "age_bin", "condition", "projected_bridge_count"]
        answer = "Projected bridge counts for " + str(projection_year) + ": " + "; ".join(
            f"{row[2]} {('age ' + str(row[3]) + ' ') if row[3] != 'All' else ''}{row[4]} = **{_number(row[5])}**"
            for row in rows
        ) + "."
        return columns, rows, answer, warnings

    if intent.family == "program":
        definition = DEFAULT_KPI_REGISTRY.get(str(intent.program_measure_id))
        values = [
            row for row in report.get("values", [])
            if isinstance(row, Mapping)
            and row.get("measure_id") == intent.program_measure_id
            and (intent.inventory_year is None or int(row.get("period_year", 0)) == intent.inventory_year)
        ]
        columns = ["measure", "period_year", "value_kind", "value", "unit", "organization"]
        rows = [[definition.title, row.get("period_year"), row.get("value_kind"), row.get("value_decimal"), row.get("unit"), row.get("organization")] for row in values]
        if rows:
            answer = f"Approved values for **{definition.title}** are shown in the result table."
        else:
            rows = [[definition.title, None, None, None, definition.unit, None]]
            answer = (
                f"There is currently **no approved program-source value** for **{definition.title}**. "
                "BridgeGuard does not infer this program KPI from the bridge inventory."
            )
        return columns, rows, answer, warnings

    values = report["values"]
    if intent.family == "acceptable_condition":
        if intent.measure == "deck_area":
            item = values["state_maintained_deck_area_acceptable"]  # type: ignore[index]
            columns = ["inventory_year", "acceptable_deck_area_ft2", "classifiable_deck_area_ft2", "acceptable_percent"]
            rows = [[year, item["acceptable_area_numerator_ft2"], item["valid_classifiable_area_denominator_ft2"], item["percent_classifiable_area"]]]
            answer = (
                f"For {year}, state-maintained bridges have **{_area(rows[0][1])}** of acceptable deck area "
                f"out of **{_area(rows[0][2])}** classifiable deck area (**{_percent(rows[0][3])}**)."
            )
        else:
            item = values["state_maintained_20ft_acceptable"]  # type: ignore[index]
            columns = ["inventory_year", "acceptable_bridge_count", "classifiable_bridge_count", "acceptable_percent"]
            rows = [[year, item["acceptable_numerator"], item["classifiable_denominator"], item["percent_classifiable"]]]
            answer = (
                f"For {year}, **{_number(rows[0][1])}** of **{_number(rows[0][2])}** classifiable "
                f"state-maintained bridges at least 20 ft long are acceptable (**{_percent(rows[0][3])}**)."
            )
        return columns, rows, answer, warnings

    if intent.family == "component_condition":
        item = next(row for row in values["component_condition_counts"] if row["component"] == intent.component)  # type: ignore[index]
        count = int(item[str(intent.condition)])
        denominator = int(item["rated_bridges"])
        percent = round(count * 100 / denominator, 1) if denominator else None
        columns = ["inventory_year", "component", "condition", "bridge_count", "rated_bridge_count", "percent_of_rated"]
        rows = [[year, intent.component, intent.condition, count, denominator, percent]]
        answer = (
            f"For {year}, **{_number(count)}** bridges have a {intent.condition} {intent.component.casefold()} rating "
            f"out of **{_number(denominator)}** rated bridges (**{_percent(percent)}**)."
        )
        return columns, rows, answer, warnings

    measure = intent.measure
    if measure == "total_deck_area":
        columns, rows = ["inventory_year", "total_deck_area_ft2"], [[year, values["total_valid_deck_area_ft2"]]]  # type: ignore[index]
        answer = f"The {year} KPI total valid bridge deck area is **{_area(rows[0][1])}**."
    elif measure == "average_age":
        columns, rows = ["inventory_year", "average_age_years"], [[year, values["average_age_at_snapshot"]]]  # type: ignore[index]
        answer = f"The {year} KPI average bridge age is **{_number(rows[0][1])} years**."
    elif measure == "high_traffic_count":
        columns, rows = ["inventory_year", "aadt_threshold", "bridge_count"], [[year, values["high_traffic_threshold"], values["high_traffic_bridge_count"]]]  # type: ignore[index]
        answer = f"The {year} KPI counts **{_number(rows[0][2])}** high-traffic bridges at AADT ≥ {_number(rows[0][1])}."
    elif measure in {"condition_count", "condition_percent"}:
        count = values["condition_counts"][str(intent.condition)]  # type: ignore[index]
        percent = values["condition_percent_of_all"][str(intent.condition)]  # type: ignore[index]
        columns, rows = ["inventory_year", "condition", "bridge_count", "percent_of_all"], [[year, intent.condition, count, percent]]
        answer = f"For {year}, **{_number(count)}** bridges are {intent.condition} (**{_percent(percent)}** of all loaded bridges)."
    elif measure == "county_count":
        columns = ["inventory_year", "county", "bridge_count"]
        if intent.condition is None:
            rows = [[year, county, count] for county, count in values["county_counts"].items()]  # type: ignore[index]
        else:
            rows = [[year, item["county"], item[str(intent.condition)]] for item in values["county_condition_counts"]]  # type: ignore[index]
        if intent.county is not None:
            rows = [row for row in rows if row[1] == intent.county]
            answer = (
                f"For {year}, **{intent.county} County** has **{_number(rows[0][2] if rows else 0)}** "
                f"{(intent.condition + ' ') if intent.condition else ''}bridges in the KPI inventory."
            )
        elif intent.condition is None:
            answer = f"The {year} KPI bridge counts for all counties are shown in the result table."
        else:
            answer = f"The {year} KPI {intent.condition} bridge counts for all counties are shown in the result table."
    else:
        columns, rows = ["inventory_year", "bridge_count"], [[year, values["total_bridges"]]]  # type: ignore[index]
        answer = f"The {year} KPI loaded bridge count is **{_number(rows[0][1])}**."
    return columns, rows, answer, warnings


def _clarification(message: str) -> list[dict[str, object]]:
    return [{
        "type": "clarification",
        "code": "kpi_scope_required",
        "message": message,
        "options": [],
    }]


def _compute_report(
    *,
    database_path: str,
    workspace_id: str,
    state_code: int,
    year: int | None,
    intent: _Intent,
) -> tuple[Mapping[str, object], int | None]:
    if intent.family == "definition":
        return {
            "year": year,
            "data_generation": 0,
            "schema_generation": 0,
            "definition_version": DEFAULT_KPI_REGISTRY.version,
        }, year
    if intent.family == "age_condition":
        compute = (
            compute_age_condition_area
            if intent.measure == "deck_area"
            else compute_age_condition_counts
        )
        return compute(
            database_path=database_path,
            workspace_id=workspace_id,
            state_code=state_code,
            year=year,
            population=intent.population,
        ), year
    if intent.family == "fhwa_tpm":
        if year is None:
            operational = compute_operational_kpis(
                database_path=database_path,
                workspace_id=workspace_id,
                state_code=state_code,
                year=None,
            )
            year = int(operational["year"])
        return compute_fhwa_tpm_diagnostic(
            database_path=database_path,
            workspace_id=workspace_id,
            state_code=state_code,
            year=year,
        ), year
    if intent.family == "future_condition":
        if year is None:
            operational = compute_operational_kpis(
                database_path=database_path,
                workspace_id=workspace_id,
                state_code=state_code,
                year=None,
            )
            year = int(operational["year"])
        horizon = 10
        if intent.projection_year is not None:
            horizon = intent.projection_year - year
        return compute_future_condition_scenarios(
            database_path=database_path,
            workspace_id=workspace_id,
            state_code=state_code,
            year=year,
            horizon_years=max(1, min(30, horizon)),
        ), year
    if intent.family == "program":
        report = list_program_kpis(database_path, workspace_id=workspace_id)
        return {**report, "year": year or 0}, year
    return compute_operational_kpis(
        database_path=database_path,
        workspace_id=workspace_id,
        state_code=state_code,
        year=year,
    ), year


def stream_bridgette_kpi_query(
    *,
    database_path: str,
    workspace_id: str,
    request: _Request,
    interpreted_intent: _Intent | None = None,
) -> Iterator[dict[str, object]] | None:
    """Return a KPI event iterator, or ``None`` for a non-KPI question."""

    if interpreted_intent is None:
        intent, clarification = _recognize(request.question)
    else:
        intent, clarification = interpreted_intent, None
    if clarification is not None:
        return iter(_clarification(clarification))
    if intent is None:
        return None
    if request.scope.bridge_number:
        return iter(_clarification(
            "Dashboard KPIs describe bridge populations, not a single bridge. Remove the bridge filter or ask for that bridge's inventory fields."
        ))
    mode = getattr(request.scope.time_mode, "value", str(request.scope.time_mode)).casefold()
    if mode == "trend" and intent.family != "definition":
        return iter(_clarification(
            "Select one inventory year (or Latest) for this KPI question. KPI trend tables are not yet exposed through Bridgette."
        ))

    def _events() -> Iterator[dict[str, object]]:
        cancellation = getattr(request, "cancellation_token", None)
        if cancellation is not None and cancellation.is_set():
            return
        year = _scope_year(intent, request.scope)
        report, year = _compute_report(
            database_path=database_path,
            workspace_id=workspace_id,
            state_code=int(request.scope.state_code),
            year=year,
            intent=intent,
        )
        columns, rows, answer, warnings = _row_result(report, intent)
        selected_year_value = report.get("year") or year
        selected_year = int(selected_year_value) if selected_year_value is not None else None
        yield {
            "type": "evidence",
            "sql": "",
            "query_language": "kpi_registry",
            "answer_mode": "verified_kpi",
            "answer_status": "verified",
            "columns": columns,
            "rows": rows,
            "count": len(rows),
            "truncated": False,
            "scope": _scope_payload(request.scope, selected_year=selected_year),
            "catalog_version": KPI_QUERY_VERSION,
            "schema_generation": int(report.get("schema_generation", 0)),
            "data_generation": int(report.get("data_generation", 0)),
            "definition_version": report.get("definition_version"),
            "revision_set_sha256": report.get("revision_set_sha256"),
            "kpi_plan": intent.as_dict(),
            "warnings": warnings,
        }
        if cancellation is not None and cancellation.is_set():
            return
        yield {"type": "token", "content": answer}

    try:
        events = tuple(_events())
    except Exception:
        _LOGGER.info(
            "governed KPI route unavailable; preserving the existing Bridgette path",
            extra={"kpi_family": intent.family},
            exc_info=True,
        )
        return None
    return iter(events)


__all__ = [
    "KPI_QUERY_VERSION",
    "intent_from_model_payload",
    "stream_bridgette_kpi_query",
]
