"""Immutable KPI definition registry.

Every displayed value is tied to a definition that says what was counted,
what was excluded, which denominator was used, and whether agency approval is
still pending.  This prevents similarly named inventory, program, and federal
measures from being silently interchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Mapping


REGISTRY_VERSION = "bridgeguard-kpi-registry-v1"
OPERATIONAL_DEFINITION_VERSION = "bridgeguard-operational-v1"
ACCEPTABLE_DEFINITION_VERSION = "njdot-state-maintained-candidate-v1"


@dataclass(frozen=True, slots=True)
class KpiDefinition:
    measure_id: str
    title: str
    purpose: str
    population: str
    numerator: str
    denominator: str
    unit: str
    time_basis: str
    rounding: str
    exclusions: tuple[str, ...]
    source_fields: tuple[str, ...]
    status: str
    definition_version: str
    approval: str

    def as_dict(self) -> dict[str, object]:
        return {
            "measure_id": self.measure_id,
            "title": self.title,
            "purpose": self.purpose,
            "population": self.population,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "unit": self.unit,
            "time_basis": self.time_basis,
            "rounding": self.rounding,
            "exclusions": list(self.exclusions),
            "source_fields": list(self.source_fields),
            "status": self.status,
            "definition_version": self.definition_version,
            "approval": self.approval,
        }


@dataclass(frozen=True, slots=True)
class KpiRegistry:
    version: str
    definitions: Mapping[str, KpiDefinition]
    sha256: str

    def get(self, measure_id: str) -> KpiDefinition:
        try:
            return self.definitions[str(measure_id)]
        except KeyError as exc:
            raise KeyError(f"unknown KPI measure: {measure_id}") from exc

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "definitions": [
                self.definitions[key].as_dict() for key in sorted(self.definitions)
            ],
        }


def _definition(
    measure_id: str,
    title: str,
    *,
    purpose: str,
    population: str,
    numerator: str,
    denominator: str,
    unit: str,
    source_fields: tuple[str, ...],
    exclusions: tuple[str, ...] = (),
    status: str = "operational_research",
    version: str = OPERATIONAL_DEFINITION_VERSION,
    approval: str = "BridgeGuard reviewed operational definition; not an FHWA measure",
    rounding: str = "Decimal half-up to one decimal place for displayed percentages",
) -> KpiDefinition:
    return KpiDefinition(
        measure_id=measure_id,
        title=title,
        purpose=purpose,
        population=population,
        numerator=numerator,
        denominator=denominator,
        unit=unit,
        time_basis="Exact selected published inventory year; age uses selected year minus B.W.01",
        rounding=rounding,
        exclusions=exclusions,
        source_fields=source_fields,
        status=status,
        definition_version=version,
        approval=approval,
    )


def _program_definition(
    measure_id: str,
    title: str,
    unit: str,
    purpose: str,
) -> KpiDefinition:
    return _definition(
        measure_id,
        title,
        purpose=purpose,
        population="Defined by the approved annual program source; never inferred from bridge inventory.",
        numerator="Approved source-recorded value for the selected fiscal year and value kind.",
        denominator=(
            "Approved source-recorded program denominator documented by the source owner."
            if unit == "percent"
            else "Not applicable."
        ),
        unit=unit,
        source_fields=("governed_program_source", "fiscal_year", "value_kind"),
        exclusions=("Missing program values remain missing and are never replaced with zero.",),
        status="program_source_required",
        version="bridgeguard-program-kpi-v1",
        approval="A different authenticated reviewer must approve the imported source before display as approved actual/target data.",
    )


_DEFINITIONS = (
    _definition(
        "bridge_count_total",
        "Published bridge count",
        purpose="Count exact published bridge snapshots.",
        population="All published bridges in the selected state and year.",
        numerator="One per exact published state/bridge/year identity.",
        denominator="Not applicable.",
        unit="bridges",
        source_fields=("BL01", "BID01", "data_year", "snapshot_revision_id"),
    ),
    _definition(
        "bridge_condition_counts",
        "Bridge counts by condition",
        purpose="Show Good, Fair, Poor, and Unknown inventory counts without hiding missing ratings.",
        population="All published bridges in the selected state and year.",
        numerator="Count in each controlling-condition class.",
        denominator="Both all published bridges and classifiable bridges are returned.",
        unit="bridges and percent",
        source_fields=("BC01", "BC02", "BC03", "BC04", "BC13"),
        exclusions=("Invalid condition ratings remain Unknown; they are not coerced." ,),
    ),
    _definition(
        "average_snapshot_age",
        "Average bridge age at snapshot",
        purpose="Describe inventory age without using the current wall-clock year.",
        population="Published bridges with a valid B.W.01 not later than the selected year.",
        numerator="Sum of selected year minus year built.",
        denominator="Bridges with a valid snapshot age.",
        unit="years",
        source_fields=("BW01", "data_year"),
        exclusions=("Missing or future year-built values are reported separately.",),
    ),
    _definition(
        "deck_area_total",
        "Valid bridge deck area",
        purpose="Quantify exact valid deck-area coverage and missing geometry.",
        population="All published bridges in the selected state and year.",
        numerator="Positive BG15; otherwise positive BG02 multiplied by positive BG05.",
        denominator="Not applicable.",
        unit="square feet",
        source_fields=("BG15", "BG02", "BG05"),
        exclusions=("Missing, zero, negative, or non-finite geometry is excluded and counted.",),
    ),
    _definition(
        "state_maintained_20ft_acceptable",
        "State-maintained bridges 20 ft or more in acceptable condition",
        purpose="Candidate NJDOT-style count KPI from the supplied budget example.",
        population="Maintenance responsibility B.CL.02=S01 and BG01 at least 20.0 ft.",
        numerator="Eligible classifiable bridges with controlling rating at least 5.",
        denominator="Eligible classifiable bridges; all-eligible and Unknown effect are also returned.",
        unit="percent",
        source_fields=("BCL02", "BG01", "BC01", "BC02", "BC03", "BC04", "BC13"),
        exclusions=("Unknown/invalid controlling condition is excluded from the default denominator.",),
        status="research_candidate_definition_pending_agency_approval",
        version=ACCEPTABLE_DEFINITION_VERSION,
        approval="S01 is the SNBI State Transportation Department maintenance code; NJDOT must approve the program denominator policy before official use.",
    ),
    _definition(
        "state_maintained_deck_area_acceptable",
        "State-maintained bridge deck area in acceptable condition",
        purpose="Candidate NJDOT-style deck-area KPI from the supplied budget example.",
        population="Maintenance responsibility B.CL.02=S01 with valid deck area.",
        numerator="Valid deck area for classifiable bridges with controlling rating at least 5.",
        denominator="Valid deck area for classifiable eligible bridges; unknown-condition area is returned separately.",
        unit="percent",
        source_fields=("BCL02", "BG15", "BG02", "BG05", "BC01", "BC02", "BC03", "BC04", "BC13"),
        exclusions=("Missing geometry and Unknown/invalid condition are separately disclosed.",),
        status="research_candidate_definition_pending_agency_approval",
        version=ACCEPTABLE_DEFINITION_VERSION,
        approval="S01 and the classifiable-area denominator require NJDOT program-owner approval before official use.",
    ),
    _definition(
        "deck_area_by_age_condition",
        "Deck area by age and condition",
        purpose="Reproduce the supplied five-year age/condition stacked-area view with explicit Unknowns.",
        population="Published bridges with valid deck area and valid snapshot age.",
        numerator="Deck area summed by age bin and Good, Upper Fair (6), Lower Fair (5), Poor, or Unknown.",
        denominator="Not applicable; excluded geometry and age are returned.",
        unit="square feet",
        source_fields=("BW01", "BG15", "BG02", "BG05", "BC01", "BC02", "BC03", "BC04", "BC13"),
    ),
    _program_definition("inhouse_bridge_inspections_count", "State-maintained bridge inspections in-house", "inspections", "Track approved fiscal-year inspection delivery by in-house staff."),
    _program_definition("consultant_bridge_inspections_count", "State-maintained bridge inspections by consultants", "inspections", "Track approved fiscal-year inspection delivery by consultants."),
    _program_definition("county_bridge_inspections_count", "County bridge safety inspections", "inspections", "Track approved fiscal-year county inspection delivery."),
    _program_definition("planned_projects_awarded_percent", "Planned construction projects awarded", "percent", "Track the approved share of planned projects awarded by the fiscal-year cutoff."),
    _program_definition("construction_contracts_awarded_count", "Construction contracts awarded", "contracts", "Track approved fiscal-year construction contract awards."),
    _program_definition("construction_contracts_awarded_value", "Construction contracts awarded value", "USD", "Track approved fiscal-year construction award value."),
    _program_definition("railroad_grade_crossings_inspected_percent", "Railroad grade crossings inspected", "percent", "Track the approved fiscal-year grade-crossing inspection percentage."),
    _program_definition("row_acquisition_cost", "ROW acquisition cost", "USD", "Track approved fiscal-year right-of-way acquisition cost."),
    _program_definition("bridges_completed_count", "Bridges completed", "bridges", "Track structures meeting the approved fiscal-year completion definition."),
    _program_definition("lane_miles_under_construction", "Lane-miles under construction", "lane-miles", "Track approved fiscal-year lane-mile exposure without overlap double counting."),
    _program_definition("projects_designed_in_house_value", "Construction projects designed in-house", "USD", "Track the approved fiscal-year value of projects designed in-house."),
)


def _build_registry() -> KpiRegistry:
    mapping = {definition.measure_id: definition for definition in _DEFINITIONS}
    if len(mapping) != len(_DEFINITIONS):
        raise RuntimeError("duplicate KPI measure ID")
    canonical = json.dumps(
        [mapping[key].as_dict() for key in sorted(mapping)],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return KpiRegistry(
        version=REGISTRY_VERSION,
        definitions=MappingProxyType(mapping),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


DEFAULT_KPI_REGISTRY = _build_registry()
