from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.services.point_cloud.coordinates import (
    CoordinateContractError,
    CoordinateFrame,
    FrameKind,
    GravitySource,
    LinearUnit,
    RegistrationReceipt,
    registration_hash,
)
from app.services.point_cloud.source_reader import (
    E57_PARSER_VERSION,
    PointCloudFormat,
    PointCloudSourceError,
    SourceLimits,
    detect_source_format,
    inspect_e57_source,
    parser_capabilities,
    read_e57_source,
    source_ingest_extensions,
)


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


@pytest.mark.parametrize(
    ("filename", "prefix", "expected"),
    [
        ("scan.e57", b"ASTM-E57" + b"\0" * 32, PointCloudFormat.E57),
        ("scan.las", b"LASF" + b"\0" * 32, PointCloudFormat.LAS),
        ("scan.laz", b"LASF" + b"\0" * 32, PointCloudFormat.LAZ),
        ("scan.ply", b"ply\nformat ascii 1.0\n", PointCloudFormat.PLY),
        ("scan.pcd", b"# .PCD v0.7\nFIELDS x y z\nDATA ascii\n", PointCloudFormat.PCD),
        ("scan.xyz", b"1.0 2.0 3.0\n", PointCloudFormat.XYZ_TEXT),
        ("scan.npz", b"PK\x03\x04" + b"\0" * 32, PointCloudFormat.NPZ_INTERNAL),
    ],
)
def test_format_detection_requires_matching_signature(tmp_path, filename, prefix, expected):
    path = _write(tmp_path / filename, prefix)
    assert detect_source_format(path) is expected


def test_extension_signature_mismatch_and_binary_text_fail_closed(tmp_path):
    with pytest.raises(PointCloudSourceError) as caught:
        detect_source_format(_write(tmp_path / "fake.e57", b"LASF" + b"\0" * 20))
    assert caught.value.code == "source_signature_mismatch"
    with pytest.raises(PointCloudSourceError) as caught:
        detect_source_format(_write(tmp_path / "bad.xyz", b"1,2\0,3"))
    assert caught.value.code == "source_signature_mismatch"


def test_capability_claims_do_not_advertise_undeployed_parsers():
    unavailable = parser_capabilities(pye57_available=False)
    assert source_ingest_extensions(pye57_available=False) == frozenset()
    assert all(not item.source_ingest_allowed for item in unavailable)
    available = parser_capabilities(pye57_available=True)
    e57 = next(item for item in available if item.format is PointCloudFormat.E57)
    assert e57.parser == E57_PARSER_VERSION
    assert e57.source_ingest_allowed is True
    assert source_ingest_extensions(pye57_available=True) == frozenset({".e57"})
    assert next(item for item in available if item.format is PointCloudFormat.LAZ).source_ingest_allowed is False


@dataclass
class _Header:
    point_count: int
    rotation_matrix: np.ndarray
    translation: np.ndarray
    point_fields: tuple[str, ...] = (
        "cartesianX",
        "cartesianY",
        "cartesianZ",
        "intensity",
        "colorRed",
        "colorGreen",
        "colorBlue",
    )


class _Reader:
    headers: list[_Header] = []
    scans: list[dict[str, np.ndarray]] = []

    def __init__(self, _path: str):
        self.scan_count = len(self.headers)

    def get_header(self, index: int):
        return self.headers[index]

    def read_scan(self, index: int, **kwargs):
        assert kwargs.get("transform") is False
        return self.scans[index]


def _module(headers, scans):
    class Reader(_Reader):
        pass

    Reader.headers = headers
    Reader.scans = scans
    return SimpleNamespace(E57=Reader)


def _scan(points, *, intensity=True, rgb=True):
    points = np.asarray(points, dtype=np.float64)
    result = {
        "cartesianX": points[:, 0],
        "cartesianY": points[:, 1],
        "cartesianZ": points[:, 2],
    }
    if intensity:
        result["intensity"] = np.linspace(1.0, 2.0, len(points))
    if rgb:
        result.update(
            colorRed=np.full(len(points), 255),
            colorGreen=np.full(len(points), 128),
            colorBlue=np.zeros(len(points)),
        )
    return result


def _verified_frame():
    return CoordinateFrame(
        kind=FrameKind.ENGINEERING_LOCAL,
        horizontal_unit=LinearUnit.METRE,
        vertical_unit=LinearUnit.METRE,
        up_vector=(0.0, 0.0, 1.0),
        gravity_source=GravitySource.CONTROL_REGISTRATION,
        metadata_source="survey control",
    )


def test_single_scan_preserves_float64_attributes_and_reversible_transform(tmp_path):
    path = _write(tmp_path / "one.e57", b"ASTM-E57" + b"fixture")
    points = np.asarray(((7_000_000.000001, 4_000_000.0, 10.0), (7_000_000.001001, 4_000_001.0, 11.0)))
    header = _Header(len(points), np.eye(3), np.asarray((1.0, 2.0, 3.0)))
    parsed = read_e57_source(
        path,
        coordinate_frame=_verified_frame(),
        pye57_module=_module([header], [_scan(points)]),
    )
    assert parsed.world_points.dtype == np.float64
    assert parsed.world_points.flags.writeable is False
    assert parsed.attributes["rgb"].flags.writeable is False
    assert parsed.profile.visualization_only is False
    assert parsed.profile.selected_scan_indices == (0,)
    assert parsed.profile.source_sha256
    parsed.require_metric_ready()
    assert np.allclose(parsed.local_transform.to_world(parsed.local_points), parsed.world_points)
    assert parsed.world_points[1, 0] - parsed.world_points[0, 0] == pytest.approx(0.001, abs=1e-9)


def test_multi_scan_requires_explicit_selection_and_reviewed_registration(tmp_path):
    path = _write(tmp_path / "multi.e57", b"ASTM-E57" + b"fixture")
    headers = [
        _Header(1, np.eye(3), np.asarray((0.0, 0.0, 0.0))),
        _Header(1, np.eye(3), np.asarray((10.0, 0.0, 0.0))),
    ]
    module = _module(headers, [_scan(((1.0, 2.0, 3.0),)), _scan(((1.0, 2.0, 3.0),))])
    with pytest.raises(PointCloudSourceError) as caught:
        read_e57_source(path, pye57_module=module)
    assert caught.value.code == "scan_selection_required"
    with pytest.raises(PointCloudSourceError) as caught:
        read_e57_source(path, selected_scan_indices=(0, 1), pye57_module=module)
    assert caught.value.code == "registration_required"
    transforms = (np.eye(4), np.eye(4))
    receipt = RegistrationReceipt(
        (0, 1), 0, 0.01, 0.02, "survey targets", registration_hash(transforms), True
    )
    parsed = read_e57_source(
        path,
        selected_scan_indices=(0, 1),
        coordinate_frame=_verified_frame(),
        registration=receipt,
        pye57_module=module,
    )
    assert parsed.profile.parsed_point_count == 2
    assert parsed.world_points[:, 0].tolist() == [1.0, 11.0]
    assert parsed.attributes["scan_id"].tolist() == [0, 1]


def test_metadata_inspection_hashes_every_scan_transform_without_materializing_points(tmp_path):
    path = _write(tmp_path / "inspect.e57", b"ASTM-E57" + b"fixture")
    headers = [
        _Header(2, np.eye(3), np.asarray((0.0, 0.0, 0.0))),
        _Header(3, np.eye(3), np.asarray((10.0, 0.0, 0.0))),
    ]

    class HeaderOnlyReader(_Reader):
        def read_scan(self, *_args, **_kwargs):
            raise AssertionError("metadata inspection must not materialize point arrays")

    HeaderOnlyReader.headers = headers
    HeaderOnlyReader.scans = []
    inspection = inspect_e57_source(path, pye57_module=SimpleNamespace(E57=HeaderOnlyReader))
    assert inspection.total_scan_count == 2
    assert inspection.total_declared_point_count == 5
    assert [scan.point_count for scan in inspection.scans] == [2, 3]
    assert len(inspection.registration_transform_sha256) == 64
    assert all(len(scan.transform_sha256) == 64 for scan in inspection.scans)


def test_default_e57_frame_keeps_si_units_but_does_not_guess_gravity(tmp_path):
    path = _write(tmp_path / "one.e57", b"ASTM-E57" + b"fixture")
    header = _Header(1, np.eye(3), np.zeros(3))
    parsed = read_e57_source(path, pye57_module=_module([header], [_scan(((1, 2, 3),))]))
    assert parsed.profile.coordinate_frame.horizontal_unit is LinearUnit.METRE
    assert parsed.profile.coordinate_frame.gravity_source is GravitySource.UNKNOWN
    assert parsed.profile.visualization_only is True
    assert parsed.local_points is None
    with pytest.raises(CoordinateContractError):
        parsed.require_metric_ready()


def test_spherical_only_scan_is_rejected(tmp_path):
    path = _write(tmp_path / "spherical.e57", b"ASTM-E57" + b"fixture")
    header = _Header(
        1,
        np.eye(3),
        np.zeros(3),
        point_fields=("sphericalRange", "sphericalAzimuth", "sphericalElevation"),
    )
    with pytest.raises(PointCloudSourceError) as caught:
        read_e57_source(path, pye57_module=_module([header], [{}]))
    assert caught.value.code == "e57_cartesian_required"


@pytest.mark.parametrize(
    ("limits", "points", "code"),
    [
        (SourceLimits(maximum_points=1), ((0, 0, 0), (1, 1, 1)), "point_count_limit"),
        (SourceLimits(maximum_materialized_bytes=24), ((0, 0, 0), (1, 1, 1)), "materialized_memory_limit"),
        (SourceLimits(maximum_absolute_coordinate=10), ((11, 0, 0),), "coordinate_magnitude_limit"),
    ],
)
def test_resource_and_coordinate_limits_fail_before_or_during_materialization(tmp_path, limits, points, code):
    path = _write(tmp_path / "limited.e57", b"ASTM-E57" + b"fixture")
    header = _Header(len(points), np.eye(3), np.zeros(3))
    with pytest.raises(PointCloudSourceError) as caught:
        read_e57_source(path, limits=limits, pye57_module=_module([header], [_scan(points)]))
    assert caught.value.code == code


def test_nonfinite_coordinates_are_rejected_not_filtered(tmp_path):
    path = _write(tmp_path / "nan.e57", b"ASTM-E57" + b"fixture")
    header = _Header(1, np.eye(3), np.zeros(3))
    with pytest.raises(PointCloudSourceError) as caught:
        read_e57_source(path, pye57_module=_module([header], [_scan(((np.nan, 0, 0),))]))
    assert caught.value.code == "nonfinite_coordinates"
