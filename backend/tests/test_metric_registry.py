from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.query import (
    ConditionBasis,
    ConditionClass,
    DEFAULT_METRIC_REGISTRY,
    MetricClassification,
    MetricStatus,
    build_semantic_catalog,
    compute_controlling_condition,
    validate_metric_registry,
)


def _catalog():
    return build_semantic_catalog(
        database_identity="test-db",
        workspace_id="shared",
        schema_generation=5,
        data_generation=2,
    )


def test_registry_is_complete_deterministic_and_compatible_with_catalog():
    registry = DEFAULT_METRIC_REGISTRY
    assert validate_metric_registry(_catalog(), registry) == ()
    assert registry.registry_hash == DEFAULT_METRIC_REGISTRY.registry_hash

    required = {
        "inventory.bridge_count",
        "inventory.concrete_deck_bridge_count",
        "inventory.concrete_deck_bridge_percent",
        "inventory.current_concrete_deck_bridge_count",
        "inventory.current_concrete_deck_bridge_percent",
        "inventory.good_bridge_count",
        "inventory.fair_bridge_count",
        "inventory.poor_bridge_count",
        "inventory.good_bridge_percent",
        "inventory.fair_bridge_percent",
        "inventory.poor_bridge_percent",
        "inventory.average_age_at_snapshot",
        "inventory.average_current_age",
        "inventory.average_daily_traffic",
        "inventory.total_bridge_length",
        "inventory.total_deck_area",
        "inventory.average_deck_area",
    }
    assert required.issubset(registry.metrics_by_id)
    for metric in registry.metrics:
        assert metric.source_resource_id
        assert metric.expression
        assert metric.unit
        assert metric.null_semantics
        assert metric.denominator_semantics
        assert metric.time_semantics
        assert isinstance(metric.required_columns, tuple)
        assert isinstance(metric.allowed_dimensions, tuple)


def test_only_operational_metrics_are_active_and_federal_tpm_is_explicitly_disabled():
    registry = DEFAULT_METRIC_REGISTRY
    assert registry.active_metrics
    assert all(
        metric.classification is MetricClassification.OPERATIONAL_INVENTORY
        for metric in registry.active_metrics
    )
    assert {metric.metric_id for metric in registry.disabled_metrics} == {
        "federal.good_deck_area_percent",
        "federal.poor_deck_area_percent",
    }
    assert all(
        metric.classification is MetricClassification.FEDERAL_PERFORMANCE_MEASURE
        and metric.status is MetricStatus.DISABLED
        and metric.disabled_reason
        for metric in registry.disabled_metrics
    )
    with pytest.raises(KeyError):
        registry.get("federal.good_deck_area_percent")
    assert (
        registry.get("federal.good_deck_area_percent", include_disabled=True).status
        is MetricStatus.DISABLED
    )


def test_registry_contract_is_frozen():
    metric = DEFAULT_METRIC_REGISTRY.get("inventory.bridge_count")
    with pytest.raises(FrozenInstanceError):
        metric.unit = "records"  # type: ignore[misc]
    with pytest.raises(TypeError):
        DEFAULT_METRIC_REGISTRY.metrics_by_id["unsafe.metric"] = metric  # type: ignore[index]


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        (9, ConditionClass.GOOD),
        (7, ConditionClass.GOOD),
        (6.9, ConditionClass.FAIR),
        (5, ConditionClass.FAIR),
        (4.9, ConditionClass.POOR),
        (0, ConditionClass.POOR),
    ],
)
def test_bc13_is_preferred_and_classified(rating, expected):
    result = compute_controlling_condition({"BC13": rating})
    assert result.rating == rating
    assert result.condition_class is expected
    assert result.basis is ConditionBasis.REPORTED_BC13
    assert result.basis_fields == ("BC13",)
    assert not result.conflict


def test_component_minimum_controls_and_bc13_disagreement_is_exposed():
    result = compute_controlling_condition(
        {"BC13": 7, "BC01": 6, "BC02": 8, "BC03": 7, "BC04": 3}
    )
    assert result.rating == 3
    assert result.basis is ConditionBasis.APPLICABLE_MINIMUM
    assert result.basis_fields == ("BC04",)
    assert result.conflict
    assert "bc13_disagrees_with_applicable_minimum" in result.conflict_reasons


def test_primary_minimum_is_used_when_bc13_is_absent():
    result = compute_controlling_condition({"BC01": 7, "BC02": 5, "BC03": 5, "BC04": "N"})
    assert result.rating == 5
    assert result.basis is ConditionBasis.PRIMARY_MINIMUM
    assert result.basis_fields == ("BC02", "BC03")
    assert result.condition_class is ConditionClass.FAIR
    assert not result.conflict


def test_culvert_is_used_only_when_all_three_primary_values_are_absent():
    culvert = compute_controlling_condition({"BC04": 4})
    assert culvert.rating == 4
    assert culvert.basis is ConditionBasis.CULVERT_FALLBACK
    assert culvert.condition_class is ConditionClass.POOR

    not_applicable_primary = compute_controlling_condition(
        {"BC01": "N", "BC02": "N", "BC03": "N", "BC04": 4}
    )
    assert not_applicable_primary.rating == 4
    assert not_applicable_primary.basis is ConditionBasis.CULVERT_FALLBACK
    assert not not_applicable_primary.conflict
    assert not_applicable_primary.invalid_fields == ()


def test_invalid_bc13_falls_back_to_primary_but_is_not_silently_lost():
    result = compute_controlling_condition({"BC13": 12, "BC01": 8, "BC02": 6, "BC03": 7})
    assert result.rating == 6
    assert result.basis is ConditionBasis.PRIMARY_MINIMUM
    assert result.conflict
    assert "invalid_rating:BC13" in result.conflict_reasons


def test_physical_projection_names_and_keyword_inputs_have_identical_semantics():
    record_result = compute_controlling_condition(
        {
            "deck_condition_rating": 8,
            "superstructure_condition_rating": 7,
            "substructure_condition_rating": 6,
            "culvert_condition_rating": None,
            "lowest_condition_rating_code": None,
        }
    )
    keyword_result = compute_controlling_condition(bc01=8, bc02=7, bc03=6)
    assert record_result == keyword_result
    assert record_result.to_dict()["basis"] == "derived_primary_min"


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -1, 10, "not-rated"])
def test_invalid_values_never_become_ratings(bad):
    result = compute_controlling_condition({"BC13": bad})
    assert result.rating is None
    assert result.condition_class is None
    assert result.basis is ConditionBasis.UNAVAILABLE
    assert result.conflict
