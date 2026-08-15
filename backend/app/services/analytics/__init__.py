"""Shared analytical contracts for forecasting, LCCA, and KPIs."""

from .identity import AnalysisSnapshotIdentity
from .life_history import BridgeLifeHistory, EvidenceClass, LifeHistoryEvent
from .treatments import (
    DEFAULT_NJDOT_TREATMENT_LIBRARY,
    TreatmentApplicability,
    TreatmentApplicabilityResult,
    TreatmentDefinition,
    TreatmentLibrary,
    TreatmentTriggerKind,
)

__all__ = [
    "AnalysisSnapshotIdentity",
    "BridgeLifeHistory",
    "DEFAULT_NJDOT_TREATMENT_LIBRARY",
    "EvidenceClass",
    "LifeHistoryEvent",
    "TreatmentApplicability",
    "TreatmentApplicabilityResult",
    "TreatmentDefinition",
    "TreatmentLibrary",
    "TreatmentTriggerKind",
]
