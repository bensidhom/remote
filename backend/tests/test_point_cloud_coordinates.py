from __future__ import annotations

import numpy as np
import pytest

from app.services.point_cloud.coordinates import (
    CoordinateContractError,
    CoordinateFrame,
    FrameKind,
    GravitySource,
    LinearUnit,
    RegistrationReceipt,
    ReversibleCoordinateTransform,
    registration_hash,
)


def _frame(**overrides):
    values = {
        "kind": FrameKind.ENGINEERING_LOCAL,
        "horizontal_unit": LinearUnit.METRE,
        "vertical_unit": LinearUnit.METRE,
        "up_vector": (0.0, 0.0, 1.0),
        "gravity_source": GravitySource.CONTROL_REGISTRATION,
        "metadata_source": "survey control receipt",
    }
    values.update(overrides)
    return CoordinateFrame(**values)


def test_unknown_units_and_assumed_gravity_are_visualization_only():
    frame = _frame(
        horizontal_unit=LinearUnit.UNKNOWN,
        vertical_unit=LinearUnit.UNKNOWN,
        gravity_source=GravitySource.ASSUMED,
    )
    assert frame.metric_ready is False
    with pytest.raises(CoordinateContractError, match="verified horizontal_unit") as caught:
        frame.require_metric_ready()
    assert caught.value.code == "metric_frame_unverified"


def test_editor_reviewed_gravity_requires_evidence():
    with pytest.raises(CoordinateContractError) as caught:
        _frame(gravity_source=GravitySource.EDITOR_REVIEWED)
    assert caught.value.code == "gravity_review_evidence_required"


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        (LinearUnit.METRE, 10.0),
        (LinearUnit.INTERNATIONAL_FOOT, 3.048),
        (LinearUnit.US_SURVEY_FOOT, 10.0 * 1200.0 / 3937.0),
    ],
)
def test_horizontal_and_vertical_unit_conversion_is_explicit(unit, expected):
    points = np.asarray(((0.0, 0.0, 0.0), (10.0, 10.0, 10.0)), dtype=np.float64)
    transform = ReversibleCoordinateTransform.from_world_points(
        points, _frame(horizontal_unit=unit, vertical_unit=unit)
    )
    local = transform.to_local(points)
    assert local[1, 0] - local[0, 0] == pytest.approx(expected)
    assert local[1, 2] - local[0, 2] == pytest.approx(expected)
    assert np.allclose(transform.to_world(local), points, rtol=0, atol=1e-12)


def test_gravity_rotation_aligns_reviewed_up_vector_to_positive_z():
    # Source +Y is vertical.  A two-metre rise must become +Z in local space.
    points = np.asarray(((5.0, 10.0, 2.0), (5.0, 12.0, 2.0)), dtype=np.float64)
    transform = ReversibleCoordinateTransform.from_world_points(
        points, _frame(up_vector=(0.0, 1.0, 0.0))
    )
    local = transform.to_local(points)
    assert local[1, 2] - local[0, 2] == pytest.approx(2.0)
    assert np.allclose(transform.to_world(local), points, rtol=0, atol=1e-12)


def test_large_world_offset_preserves_millimetre_differences():
    points = np.asarray(
        (
            (7_654_321.123456, 4_321_987.654321, 104.000001),
            (7_654_321.124456, 4_321_987.654321, 104.001001),
        ),
        dtype=np.float64,
    )
    transform = ReversibleCoordinateTransform.from_world_points(points, _frame())
    local = transform.to_local(points)
    assert local[1, 0] - local[0, 0] == pytest.approx(0.001, abs=1e-9)
    assert local[1, 2] - local[0, 2] == pytest.approx(0.001, abs=1e-9)
    assert np.allclose(transform.to_world(local), points, rtol=0, atol=1e-10)


def test_input_order_does_not_change_transform():
    points = np.asarray(((100.0, 20.0, 3.0), (101.0, 24.0, 8.0), (99.0, 21.0, 5.0)))
    first = ReversibleCoordinateTransform.from_world_points(points, _frame())
    second = ReversibleCoordinateTransform.from_world_points(points[::-1], _frame())
    assert first.transform_sha256 == second.transform_sha256
    assert first.metric_origin == second.metric_origin


def test_nonfinite_points_fail_instead_of_being_silently_removed():
    with pytest.raises(CoordinateContractError) as caught:
        ReversibleCoordinateTransform.from_world_points(
            np.asarray(((0.0, 0.0, 0.0), (1.0, np.nan, 2.0))), _frame()
        )
    assert caught.value.code == "nonfinite_coordinates"


def test_registration_receipt_enforces_review_and_rms():
    digest = registration_hash((np.eye(4), np.eye(4)))
    unreviewed = RegistrationReceipt((0, 1), 0, 0.01, 0.02, "targets", digest, False)
    with pytest.raises(CoordinateContractError) as caught:
        unreviewed.require_accepted()
    assert caught.value.code == "registration_unreviewed"
    too_large = RegistrationReceipt((0, 1), 0, 0.026, 0.04, "targets", digest, True)
    with pytest.raises(CoordinateContractError) as caught:
        too_large.require_accepted()
    assert caught.value.code == "registration_rms_exceeded"
    RegistrationReceipt((0, 1), 0, 0.025, 0.04, "targets", digest, True).require_accepted()


def test_registration_transform_hash_rejects_nonfinite_matrix():
    matrix = np.eye(4)
    matrix[0, 0] = np.inf
    with pytest.raises(CoordinateContractError) as caught:
        registration_hash((matrix,))
    assert caught.value.code == "invalid_registration_transform"

