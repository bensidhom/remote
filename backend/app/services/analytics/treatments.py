"""Versioned bridge-treatment definitions and fail-closed applicability checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Iterable, Mapping


class TreatmentTriggerKind(str, Enum):
    CYCLIC = "cyclic"
    CONDITION = "condition"
    AS_NEEDED = "as_needed"


class TreatmentApplicability(str, Enum):
    APPLICABLE = "applicable"
    INCOMPATIBLE = "incompatible"
    NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True, slots=True)
class TreatmentApplicabilityResult:
    status: TreatmentApplicability
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TreatmentDefinition:
    treatment_id: str
    title: str
    component: str
    trigger_kind: TreatmentTriggerKind
    cycle_years: int | None
    compatible_material_codes: tuple[str, ...] = ()
    minimum_condition_rating: int | None = 5
    maximum_condition_rating: int | None = 9
    requires_element_evidence: bool = False
    requires_concurrent_work: bool = False
    duration_years: int = 1
    minimum_spacing_years: int = 1
    cost_unit: str = "lump_sum"
    residual_life_rule: str = "none"
    source_reference: str = ""

    def __post_init__(self) -> None:
        treatment_id = str(self.treatment_id).strip().lower()
        title = str(self.title).strip()
        component = str(self.component).strip().lower()
        if not treatment_id or not title or not component:
            raise ValueError("treatment_id, title, and component are required")
        if self.trigger_kind is TreatmentTriggerKind.CYCLIC:
            if self.cycle_years is None or int(self.cycle_years) <= 0:
                raise ValueError("cyclic treatments require a positive cycle_years")
        elif self.cycle_years is not None:
            raise ValueError("non-cyclic treatments cannot declare cycle_years")
        if self.minimum_condition_rating is not None and not 0 <= int(
            self.minimum_condition_rating
        ) <= 9:
            raise ValueError("minimum_condition_rating must be between 0 and 9")
        if self.maximum_condition_rating is not None and not 0 <= int(
            self.maximum_condition_rating
        ) <= 9:
            raise ValueError("maximum_condition_rating must be between 0 and 9")
        if (
            self.minimum_condition_rating is not None
            and self.maximum_condition_rating is not None
            and self.minimum_condition_rating > self.maximum_condition_rating
        ):
            raise ValueError("condition-rating range is inverted")
        if int(self.duration_years) < 1 or int(self.minimum_spacing_years) < 1:
            raise ValueError("duration and minimum spacing must be positive")
        if not str(self.cost_unit).strip() or not str(self.residual_life_rule).strip():
            raise ValueError("cost unit and residual-life rule are required")
        materials = tuple(
            sorted({str(value).strip().upper() for value in self.compatible_material_codes if str(value).strip()})
        )
        object.__setattr__(self, "treatment_id", treatment_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "component", component)
        object.__setattr__(self, "compatible_material_codes", materials)
        if self.cycle_years is not None:
            object.__setattr__(self, "cycle_years", int(self.cycle_years))

    def evaluate(
        self,
        *,
        component: str,
        material_code: str | None,
        condition_rating: int | None,
        element_evidence_available: bool,
        concurrent_work: bool,
    ) -> TreatmentApplicabilityResult:
        incompatible: list[str] = []
        unavailable: list[str] = []
        if str(component).strip().lower() != self.component:
            incompatible.append("component")
        normalized_material = str(material_code).strip().upper() if material_code else None
        if self.compatible_material_codes:
            if normalized_material is None:
                unavailable.append("material")
            elif normalized_material not in self.compatible_material_codes:
                incompatible.append("material")
        if condition_rating is None:
            unavailable.append("condition_rating")
        else:
            rating = int(condition_rating)
            if self.minimum_condition_rating is not None and rating < self.minimum_condition_rating:
                incompatible.append("condition_rating")
            if self.maximum_condition_rating is not None and rating > self.maximum_condition_rating:
                incompatible.append("condition_rating")
        if self.requires_element_evidence and not element_evidence_available:
            unavailable.append("element_evidence")
        if self.requires_concurrent_work and not concurrent_work:
            incompatible.append("concurrent_work")
        if incompatible:
            return TreatmentApplicabilityResult(
                TreatmentApplicability.INCOMPATIBLE,
                tuple(dict.fromkeys(incompatible)),
            )
        if unavailable:
            return TreatmentApplicabilityResult(
                TreatmentApplicability.NOT_EVALUABLE,
                tuple(dict.fromkeys(unavailable)),
            )
        return TreatmentApplicabilityResult(TreatmentApplicability.APPLICABLE)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "treatment_id": self.treatment_id,
            "title": self.title,
            "component": self.component,
            "trigger_kind": self.trigger_kind.value,
            "cycle_years": self.cycle_years,
            "compatible_material_codes": list(self.compatible_material_codes),
            "minimum_condition_rating": self.minimum_condition_rating,
            "maximum_condition_rating": self.maximum_condition_rating,
            "requires_element_evidence": self.requires_element_evidence,
            "requires_concurrent_work": self.requires_concurrent_work,
            "duration_years": self.duration_years,
            "minimum_spacing_years": self.minimum_spacing_years,
            "cost_unit": self.cost_unit,
            "residual_life_rule": self.residual_life_rule,
            "source_reference": self.source_reference,
        }


@dataclass(frozen=True, slots=True)
class TreatmentLibrary:
    library_id: str
    version: str
    source_title: str
    source_url: str
    treatments: tuple[TreatmentDefinition, ...]

    def __post_init__(self) -> None:
        if not str(self.library_id).strip() or not str(self.version).strip():
            raise ValueError("library_id and version are required")
        treatments = tuple(self.treatments)
        ids = tuple(item.treatment_id for item in treatments)
        if not treatments or len(ids) != len(set(ids)):
            raise ValueError("treatment IDs must be nonempty and unique")
        object.__setattr__(self, "treatments", treatments)

    @property
    def treatments_by_id(self) -> Mapping[str, TreatmentDefinition]:
        return MappingProxyType({item.treatment_id: item for item in self.treatments})

    def get(self, treatment_id: str) -> TreatmentDefinition:
        normalized = str(treatment_id).strip().lower()
        try:
            return self.treatments_by_id[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown treatment: {normalized}") from exc

    @property
    def library_sha256(self) -> str:
        payload = {
            "library_id": self.library_id,
            "version": self.version,
            "source_title": self.source_title,
            "source_url": self.source_url,
            "treatments": [item.canonical_payload() for item in self.treatments],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


_NJDOT_TAMP_URL = (
    "https://www.nj.gov/transportation/about/asset/pdf/NJ_2019_TAMP_FHWA.pdf"
)
_NJDOT_TAMP_SOURCE = "NJDOT Bridge Preventive Maintenance Program, Revision IV, April 2016"
_FHWA_PRESERVATION_SOURCE = "FHWA Bridge Preservation Guide, Spring 2018"
_FHWA_PRESERVATION_URL = "https://www.fhwa.dot.gov/bridge/preservation/guide/guide.pdf"
_CONCRETE_DECK_CODES = ("CR-T", "RC", "PC", "CONCRETE")


def _treatment(
    treatment_id: str,
    title: str,
    component: str,
    *,
    cycle: int | None,
    trigger: TreatmentTriggerKind | None = None,
    materials: Iterable[str] = (),
    elements: bool = False,
    concurrent: bool = False,
    source_reference: str = _NJDOT_TAMP_SOURCE,
    cost_unit: str = "lump_sum",
    residual_life_rule: str = "none",
    minimum_spacing_years: int = 1,
    minimum_condition_rating: int | None = 5,
    maximum_condition_rating: int | None = 9,
) -> TreatmentDefinition:
    return TreatmentDefinition(
        treatment_id=treatment_id,
        title=title,
        component=component,
        trigger_kind=trigger or (
            TreatmentTriggerKind.CYCLIC if cycle is not None else TreatmentTriggerKind.AS_NEEDED
        ),
        cycle_years=cycle,
        compatible_material_codes=tuple(materials),
        requires_element_evidence=elements,
        requires_concurrent_work=concurrent,
        minimum_spacing_years=minimum_spacing_years,
        cost_unit=cost_unit,
        residual_life_rule=residual_life_rule,
        minimum_condition_rating=minimum_condition_rating,
        maximum_condition_rating=maximum_condition_rating,
        source_reference=source_reference,
    )


DEFAULT_NJDOT_TREATMENT_LIBRARY = TreatmentLibrary(
    library_id="njdot_preventive_maintenance_2016_tamp",
    version="1",
    source_title=_NJDOT_TAMP_SOURCE,
    source_url=_NJDOT_TAMP_URL,
    treatments=(
        _treatment(
            "njdot_preservation_package",
            "Generic preventive-preservation project",
            "bridge",
            cycle=None,
            trigger=TreatmentTriggerKind.AS_NEEDED,
            cost_unit="deck_area",
            residual_life_rule="reviewed_service_life",
        ),
        _treatment("bridge_cleaning", "Bridge cleaning, washing, and sweeping", "bridge", cycle=2),
        _treatment("wearing_surface_crack_seal", "Seal asphalt wearing-surface cracks", "deck", cycle=2),
        _treatment("deck_crack_seal", "Seal concrete deck cracks", "deck", cycle=2, materials=_CONCRETE_DECK_CODES, elements=True),
        _treatment("parapet_crack_seal", "Seal parapet cracks", "parapet", cycle=2, materials=_CONCRETE_DECK_CODES),
        _treatment("deck_seal", "Seal concrete deck", "deck", cycle=5, materials=_CONCRETE_DECK_CODES),
        _treatment("deck_corrosion_inhibitor", "Apply deck corrosion inhibitor", "deck", cycle=5, materials=_CONCRETE_DECK_CODES, elements=True),
        _treatment("joint_repair_replace", "Repair or replace joints", "deck", cycle=10),
        _treatment("deck_sidewalk_concrete_repair", "Repair deck or sidewalk concrete", "deck", cycle=10, materials=_CONCRETE_DECK_CODES),
        _treatment("parapet_concrete_repair", "Repair parapet concrete", "parapet", cycle=10, materials=_CONCRETE_DECK_CODES),
        _treatment("base_plate_curb_reseal", "Reseal base plates and curb", "deck", cycle=10),
        _treatment("approach_slab_repair", "Repair approach slabs", "deck", cycle=None),
        _treatment("hma_patch", "Hot-mix asphalt patch", "deck", cycle=None),
        _treatment("safety_improvement", "Safety improvement", "bridge", cycle=None),
        _treatment("superstructure_concrete_repair", "Repair superstructure concrete", "superstructure", cycle=None, materials=_CONCRETE_DECK_CODES),
        _treatment("bearing_lubrication", "Lubricate bearings", "bearing", cycle=4, concurrent=True),
        _treatment("substructure_crack_seal", "Seal substructure cracks", "substructure", cycle=5, materials=_CONCRETE_DECK_CODES),
        _treatment("substructure_concrete_repair", "Repair substructure concrete", "substructure", cycle=10, materials=_CONCRETE_DECK_CODES),
        _treatment("substructure_carbon_wrap", "Apply substructure carbon wrap", "substructure", cycle=None, materials=_CONCRETE_DECK_CODES),
        _treatment("substructure_concrete_seal", "Seal substructure concrete", "substructure", cycle=None, materials=_CONCRETE_DECK_CODES),
        _treatment("scour_repair", "Repair erosion or scour", "substructure", cycle=None),
    ),
)


DEFAULT_FHWA_TREATMENT_LIBRARY = TreatmentLibrary(
    library_id="fhwa_bridge_preservation_2018",
    version="1",
    source_title=_FHWA_PRESERVATION_SOURCE,
    source_url=_FHWA_PRESERVATION_URL,
    treatments=(
        _treatment("deck_overlay", "Deck overlay and waterproofing", "deck", cycle=None, trigger=TreatmentTriggerKind.CONDITION, materials=_CONCRETE_DECK_CODES, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="deck_area", residual_life_rule="reviewed_service_life"),
        _treatment("deck_patching", "Deck patching", "deck", cycle=None, trigger=TreatmentTriggerKind.CONDITION, materials=_CONCRETE_DECK_CODES, elements=True, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="repair_area", residual_life_rule="none"),
        _treatment("deck_replacement", "Partial or full deck replacement", "deck", cycle=None, trigger=TreatmentTriggerKind.CONDITION, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="deck_area", residual_life_rule="new_component_life", minimum_spacing_years=20, minimum_condition_rating=0, maximum_condition_rating=6),
        _treatment("steel_spot_painting", "Steel spot or zone painting", "superstructure", cycle=None, trigger=TreatmentTriggerKind.CONDITION, materials=("S", "ST", "STEEL"), source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="painted_area", residual_life_rule="reviewed_service_life"),
        _treatment("steel_full_painting", "Full steel superstructure painting", "superstructure", cycle=None, trigger=TreatmentTriggerKind.CONDITION, materials=("S", "ST", "STEEL"), source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="painted_area", residual_life_rule="reviewed_service_life", minimum_spacing_years=10),
        _treatment("bearing_replacement", "Bearing restoration or replacement", "bearing", cycle=None, trigger=TreatmentTriggerKind.CONDITION, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="bearing_count", residual_life_rule="new_component_life"),
        _treatment("superstructure_rehabilitation", "Superstructure rehabilitation", "superstructure", cycle=None, trigger=TreatmentTriggerKind.CONDITION, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="lump_sum", residual_life_rule="reviewed_service_life", minimum_spacing_years=15, minimum_condition_rating=0, maximum_condition_rating=6),
        _treatment("substructure_rehabilitation", "Substructure rehabilitation", "substructure", cycle=None, trigger=TreatmentTriggerKind.CONDITION, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="lump_sum", residual_life_rule="reviewed_service_life", minimum_spacing_years=15, minimum_condition_rating=0, maximum_condition_rating=6),
        _treatment("pile_jacket_wrap", "Pile jacket or wrap", "substructure", cycle=None, trigger=TreatmentTriggerKind.CONDITION, materials=_CONCRETE_DECK_CODES, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="pile_count", residual_life_rule="reviewed_service_life"),
        _treatment("scour_countermeasure", "Channel or scour countermeasure", "substructure", cycle=None, trigger=TreatmentTriggerKind.AS_NEEDED, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="lump_sum", residual_life_rule="none"),
        _treatment("full_replacement", "Full bridge replacement", "bridge", cycle=None, trigger=TreatmentTriggerKind.CONDITION, source_reference=_FHWA_PRESERVATION_SOURCE, cost_unit="deck_area", residual_life_rule="new_structure_life", minimum_spacing_years=40, minimum_condition_rating=0, maximum_condition_rating=6),
    ),
)


DEFAULT_BRIDGE_TREATMENT_LIBRARY = TreatmentLibrary(
    library_id="bridgeguard_reviewed_treatments",
    version="1",
    source_title="NJDOT preventive-maintenance profile plus FHWA preservation families",
    source_url=_NJDOT_TAMP_URL,
    treatments=(
        *DEFAULT_NJDOT_TREATMENT_LIBRARY.treatments,
        *DEFAULT_FHWA_TREATMENT_LIBRARY.treatments,
    ),
)
