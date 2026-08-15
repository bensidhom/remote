"""Bounded point-cloud source inspection and E57 ingestion.

Only formats with a reviewed server parser are eligible for source ingestion.
Other formats may still be rendered locally by the research UI, but that does
not make them eligible for metric analysis or persistence as source evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import importlib.util
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import numpy as np

from .coordinates import (
    CoordinateContractError,
    CoordinateFrame,
    FrameKind,
    GravitySource,
    LinearUnit,
    RegistrationReceipt,
    ReversibleCoordinateTransform,
    registration_hash,
)


SOURCE_CONTRACT_VERSION = "bridgeguard-point-cloud-source-v1"
E57_PARSER_VERSION = "pye57-cartesian-multiscan-v1"


class PointCloudSourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


class PointCloudFormat(str, Enum):
    E57 = "e57"
    LAS = "las"
    LAZ = "laz"
    PLY = "ply"
    PCD = "pcd"
    XYZ_TEXT = "xyz_text"
    NPZ_INTERNAL = "npz_internal"


_EXTENSION_FORMAT = {
    ".e57": PointCloudFormat.E57,
    ".las": PointCloudFormat.LAS,
    ".laz": PointCloudFormat.LAZ,
    ".ply": PointCloudFormat.PLY,
    ".pcd": PointCloudFormat.PCD,
    ".xyz": PointCloudFormat.XYZ_TEXT,
    ".csv": PointCloudFormat.XYZ_TEXT,
    ".txt": PointCloudFormat.XYZ_TEXT,
    ".npz": PointCloudFormat.NPZ_INTERNAL,
}


@dataclass(frozen=True)
class SourceLimits:
    maximum_source_bytes: int = 500 * 1024 * 1024
    maximum_points: int = 25_000_000
    maximum_scans: int = 64
    maximum_dimensions: int = 32
    maximum_materialized_bytes: int = 2 * 1024 * 1024 * 1024
    maximum_metadata_bytes: int = 2 * 1024 * 1024
    maximum_absolute_coordinate: float = 100_000_000.0

    def __post_init__(self) -> None:
        integers = (
            self.maximum_source_bytes,
            self.maximum_points,
            self.maximum_scans,
            self.maximum_dimensions,
            self.maximum_materialized_bytes,
            self.maximum_metadata_bytes,
        )
        if any(not isinstance(value, int) or value < 1 for value in integers):
            raise PointCloudSourceError("invalid_source_limits", "Source limits must be positive integers.")
        if not math.isfinite(self.maximum_absolute_coordinate) or self.maximum_absolute_coordinate <= 0:
            raise PointCloudSourceError(
                "invalid_source_limits", "Coordinate magnitude limit must be finite and positive."
            )


@dataclass(frozen=True)
class ParserCapability:
    format: PointCloudFormat
    extensions: tuple[str, ...]
    parser: str | None
    server_available: bool
    source_ingest_allowed: bool
    metric_attributes_supported: tuple[str, ...]
    limitation: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format.value,
            "extensions": list(self.extensions),
            "parser": self.parser,
            "server_available": self.server_available,
            "source_ingest_allowed": self.source_ingest_allowed,
            "metric_attributes_supported": list(self.metric_attributes_supported),
            "limitation": self.limitation,
        }


def parser_capabilities(*, pye57_available: bool | None = None) -> tuple[ParserCapability, ...]:
    if pye57_available is None:
        pye57_available = importlib.util.find_spec("pye57") is not None
    unavailable = "No reviewed server parser is deployed; local visualization does not authorize metric analysis."
    return (
        ParserCapability(
            format=PointCloudFormat.E57,
            extensions=(".e57",),
            parser=E57_PARSER_VERSION if pye57_available else None,
            server_available=bool(pye57_available),
            source_ingest_allowed=bool(pye57_available),
            metric_attributes_supported=("xyz", "rgb", "intensity", "scan_id"),
            limitation=None if pye57_available else "pye57 is unavailable in this runtime.",
        ),
        ParserCapability(PointCloudFormat.LAS, (".las",), None, False, False, (), unavailable),
        ParserCapability(PointCloudFormat.LAZ, (".laz",), None, False, False, (), unavailable),
        ParserCapability(PointCloudFormat.PLY, (".ply",), None, False, False, (), unavailable),
        ParserCapability(PointCloudFormat.PCD, (".pcd",), None, False, False, (), unavailable),
        ParserCapability(
            PointCloudFormat.XYZ_TEXT, (".xyz", ".csv", ".txt"), None, False, False, (), unavailable
        ),
        ParserCapability(
            PointCloudFormat.NPZ_INTERNAL,
            (".npz",),
            "bridgeguard-internal-npz-v1",
            True,
            False,
            ("xyz", "rgb"),
            "Internal derived-artifact preview only; not accepted as immutable source evidence.",
        ),
    )


def source_ingest_extensions(*, pye57_available: bool | None = None) -> frozenset[str]:
    return frozenset(
        extension
        for capability in parser_capabilities(pye57_available=pye57_available)
        if capability.source_ingest_allowed
        for extension in capability.extensions
    )


def _signature_format(prefix: bytes) -> PointCloudFormat | None:
    if prefix.startswith(b"ASTM-E57"):
        return PointCloudFormat.E57
    if prefix.startswith(b"LASF"):
        return PointCloudFormat.LAS
    if prefix.startswith(b"ply\n") or prefix.startswith(b"ply\r\n"):
        return PointCloudFormat.PLY
    upper = prefix[:512].upper()
    if upper.startswith(b"# .PCD") or (b"FIELDS" in upper and b"DATA" in upper):
        return PointCloudFormat.PCD
    if prefix.startswith(b"PK\x03\x04"):
        return PointCloudFormat.NPZ_INTERNAL
    return None


def detect_source_format(path: str | Path, *, declared_filename: str | None = None) -> PointCloudFormat:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            prefix = handle.read(4096)
    except OSError as exc:
        raise PointCloudSourceError("source_unreadable", "Point-cloud source could not be read.") from exc
    if not prefix:
        raise PointCloudSourceError("source_empty", "Point-cloud source is empty.")
    extension = Path(declared_filename or source.name).suffix.lower()
    expected = _EXTENSION_FORMAT.get(extension)
    if expected is None:
        raise PointCloudSourceError("source_extension_unsupported", "Point-cloud source extension is unsupported.")
    detected = _signature_format(prefix)
    if expected is PointCloudFormat.LAZ:
        # Compressed LAS uses the LASF file signature; extension distinguishes it.
        detected = PointCloudFormat.LAZ if detected is PointCloudFormat.LAS else detected
    if expected is PointCloudFormat.XYZ_TEXT:
        if b"\x00" in prefix or detected is not None:
            raise PointCloudSourceError(
                "source_signature_mismatch", "Point-cloud contents do not match the declared text format."
            )
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PointCloudSourceError(
                "source_text_encoding_unsupported", "Text point clouds must use UTF-8."
            ) from exc
        return expected
    if detected is not expected:
        raise PointCloudSourceError(
            "source_signature_mismatch", "Point-cloud contents do not match the filename extension."
        )
    return expected


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise PointCloudSourceError("source_unreadable", "Point-cloud source could not be hashed.") from exc
    return digest.hexdigest()


@dataclass(frozen=True)
class PointCloudSourceProfile:
    contract_version: str
    source_sha256: str
    source_size_bytes: int
    format: PointCloudFormat
    parser: str
    total_scan_count: int
    selected_scan_indices: tuple[int, ...]
    source_point_count: int
    parsed_point_count: int
    dimensions: tuple[str, ...]
    coordinate_frame: CoordinateFrame
    registration: RegistrationReceipt | None
    visualization_only: bool
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "format": self.format.value,
            "parser": self.parser,
            "total_scan_count": self.total_scan_count,
            "selected_scan_indices": list(self.selected_scan_indices),
            "source_point_count": self.source_point_count,
            "parsed_point_count": self.parsed_point_count,
            "dimensions": list(self.dimensions),
            "coordinate_frame": self.coordinate_frame.to_dict(),
            "registration": self.registration.to_dict() if self.registration else None,
            "visualization_only": self.visualization_only,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class E57ScanSummary:
    scan_index: int
    point_count: int
    dimensions: tuple[str, ...]
    transform_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "scan_index": self.scan_index,
            "point_count": self.point_count,
            "dimensions": list(self.dimensions),
            "transform_sha256": self.transform_sha256,
        }


@dataclass(frozen=True)
class E57SourceInspection:
    source_sha256: str
    source_size_bytes: int
    total_scan_count: int
    total_declared_point_count: int
    scans: tuple[E57ScanSummary, ...]
    registration_transform_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "total_scan_count": self.total_scan_count,
            "total_declared_point_count": self.total_declared_point_count,
            "scans": [scan.to_dict() for scan in self.scans],
            "registration_transform_sha256": self.registration_transform_sha256,
        }


@dataclass(frozen=True)
class ParsedPointCloud:
    profile: PointCloudSourceProfile
    world_points: np.ndarray
    local_points: np.ndarray | None
    local_transform: ReversibleCoordinateTransform | None
    attributes: Mapping[str, np.ndarray]

    def __post_init__(self) -> None:
        for field_name in ("world_points", "local_points"):
            value = getattr(self, field_name)
            if value is None:
                continue
            value = np.ascontiguousarray(value, dtype=np.float64)
            value.setflags(write=False)
            object.__setattr__(self, field_name, value)
        frozen: dict[str, np.ndarray] = {}
        for name, values in self.attributes.items():
            array = np.ascontiguousarray(values)
            array.setflags(write=False)
            frozen[str(name)] = array
        object.__setattr__(self, "attributes", MappingProxyType(frozen))

    def require_metric_ready(self) -> None:
        self.profile.coordinate_frame.require_metric_ready()
        if len(self.profile.selected_scan_indices) > 1:
            if self.profile.registration is None:
                raise CoordinateContractError(
                    "registration_required", "Metric processing of multiple scans requires registration evidence."
                )
            self.profile.registration.require_accepted()
        if self.local_points is None or self.local_transform is None:
            raise CoordinateContractError(
                "metric_transform_unavailable", "Metric local coordinates are unavailable."
            )


def _e57_scan_count(reader: Any) -> int:
    value = getattr(reader, "scan_count", None)
    if value is not None:
        return int(value)
    try:
        return len(reader.image_file.root()["data3D"])
    except Exception:
        return 1


def _e57_header_fields(header: Any) -> tuple[str, ...]:
    fields = tuple(str(value) for value in getattr(header, "point_fields", ()) or ())
    return fields


def _header_transform(header: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rotation = np.asarray(getattr(header, "rotation_matrix", np.eye(3)), dtype=np.float64)
    translation = np.asarray(getattr(header, "translation", (0.0, 0.0, 0.0)), dtype=np.float64)
    if rotation.shape != (3, 3) or translation.shape != (3,) or not (
        np.isfinite(rotation).all() and np.isfinite(translation).all()
    ):
        raise PointCloudSourceError("invalid_scan_transform", "E57 scan transform is invalid.")
    homogeneous = np.eye(4, dtype=np.float64)
    homogeneous[:3, :3] = rotation
    homogeneous[:3, 3] = translation
    return rotation, translation, homogeneous


def inspect_e57_source(
    path: str | Path,
    *,
    declared_filename: str | None = None,
    limits: SourceLimits = SourceLimits(),
    pye57_module: Any | None = None,
) -> E57SourceInspection:
    source = Path(path)
    if detect_source_format(source, declared_filename=declared_filename) is not PointCloudFormat.E57:
        raise PointCloudSourceError("source_format_not_e57", "The reviewed source reader requires E57 input.")
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise PointCloudSourceError("source_unreadable", "Point-cloud source metadata is unavailable.") from exc
    if size > limits.maximum_source_bytes:
        raise PointCloudSourceError("source_size_limit", "Point-cloud source exceeds the configured byte limit.")
    if pye57_module is None:
        try:
            import pye57 as pye57_module  # type: ignore[no-redef]
        except ImportError as exc:
            raise PointCloudSourceError(
                "e57_parser_unavailable", "The reviewed E57 parser is unavailable in this runtime."
            ) from exc
    try:
        reader = pye57_module.E57(str(source))
        total_scans = _e57_scan_count(reader)
    except Exception as exc:
        raise PointCloudSourceError("e57_open_failed", "E57 source could not be opened.") from exc
    if total_scans < 1 or total_scans > limits.maximum_scans:
        raise PointCloudSourceError("scan_count_limit", "E57 scan count is outside the accepted range.")
    summaries: list[E57ScanSummary] = []
    transforms: list[np.ndarray] = []
    total_points = 0
    metadata_bytes = 0
    for scan_index in range(total_scans):
        try:
            header = reader.get_header(scan_index)
        except Exception as exc:
            raise PointCloudSourceError("e57_header_failed", "E57 scan metadata could not be read.") from exc
        fields = _e57_header_fields(header)
        if fields and not {"cartesianX", "cartesianY", "cartesianZ"}.issubset(fields):
            raise PointCloudSourceError(
                "e57_cartesian_required", "Spherical-only E57 scans are not supported by this parser profile."
            )
        if len(fields) > limits.maximum_dimensions:
            raise PointCloudSourceError("dimension_count_limit", "E57 dimension count exceeds the limit.")
        point_count = int(getattr(header, "point_count", 0) or 0)
        if point_count < 0:
            raise PointCloudSourceError("invalid_point_count", "E57 point count is invalid.")
        total_points += point_count
        if total_points > limits.maximum_points:
            raise PointCloudSourceError("point_count_limit", "E57 point count exceeds the configured limit.")
        metadata_bytes += sum(len(field.encode("utf-8")) for field in fields) + 16 * 8
        if metadata_bytes > limits.maximum_metadata_bytes:
            raise PointCloudSourceError("metadata_size_limit", "E57 metadata exceeds the configured limit.")
        _rotation, _translation, transform = _header_transform(header)
        transform_digest = registration_hash((transform,))
        summaries.append(E57ScanSummary(scan_index, point_count, fields, transform_digest))
        transforms.append(transform)
    estimated_bytes = total_points * min(
        limits.maximum_dimensions,
        max(4, max((len(summary.dimensions) for summary in summaries), default=4)),
    ) * 8
    if estimated_bytes > limits.maximum_materialized_bytes:
        raise PointCloudSourceError(
            "materialized_memory_limit", "E57 decompressed memory estimate exceeds the configured limit."
        )
    return E57SourceInspection(
        source_sha256=_sha256_file(source),
        source_size_bytes=size,
        total_scan_count=total_scans,
        total_declared_point_count=total_points,
        scans=tuple(summaries),
        registration_transform_sha256=registration_hash(transforms),
    )


def _read_e57_scan_untransformed(reader: Any, scan_index: int) -> Mapping[str, Any]:
    attempts = (
        {"colors": True, "intensity": True, "ignore_missing_fields": True, "transform": False},
        {"colors": True, "intensity": True, "transform": False},
    )
    for kwargs in attempts:
        try:
            return reader.read_scan(scan_index, **kwargs)
        except (TypeError, ValueError):
            continue
    try:
        return reader.read_scan_raw(scan_index, ignore_unsupported_fields=True)
    except TypeError:
        return reader.read_scan_raw(scan_index)


def _normalise_rgb(data: Mapping[str, Any], count: int) -> np.ndarray | None:
    keys = ("colorRed", "colorGreen", "colorBlue")
    if not all(key in data for key in keys):
        return None
    rgb = np.column_stack(tuple(np.asarray(data[key]) for key in keys)).astype(np.float64)
    if rgb.shape != (count, 3) or not np.isfinite(rgb).all():
        raise PointCloudSourceError("invalid_rgb", "E57 RGB attributes are malformed or non-finite.")
    maximum = float(rgb.max(initial=0.0))
    if maximum > 1.0:
        rgb /= 255.0 if maximum <= 255.0 else 65535.0
    return np.clip(rgb, 0.0, 1.0)


def _optional_attribute(data: Mapping[str, Any], key: str, count: int) -> np.ndarray | None:
    if key not in data:
        return None
    value = np.asarray(data[key])
    if value.ndim != 1 or len(value) != count:
        raise PointCloudSourceError("invalid_point_attribute", f"E57 attribute {key} is malformed.")
    if np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
        raise PointCloudSourceError("nonfinite_point_attribute", f"E57 attribute {key} is non-finite.")
    return value


def read_e57_source(
    path: str | Path,
    *,
    declared_filename: str | None = None,
    selected_scan_indices: Sequence[int] | None = None,
    coordinate_frame: CoordinateFrame | None = None,
    registration: RegistrationReceipt | None = None,
    limits: SourceLimits = SourceLimits(),
    pye57_module: Any | None = None,
) -> ParsedPointCloud:
    source = Path(path)
    if detect_source_format(source, declared_filename=declared_filename) is not PointCloudFormat.E57:
        raise PointCloudSourceError("source_format_not_e57", "The reviewed source reader requires E57 input.")
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise PointCloudSourceError("source_unreadable", "Point-cloud source metadata is unavailable.") from exc
    if size > limits.maximum_source_bytes:
        raise PointCloudSourceError("source_size_limit", "Point-cloud source exceeds the configured byte limit.")
    if pye57_module is None:
        try:
            import pye57 as pye57_module  # type: ignore[no-redef]
        except ImportError as exc:
            raise PointCloudSourceError(
                "e57_parser_unavailable", "The reviewed E57 parser is unavailable in this runtime."
            ) from exc
    try:
        reader = pye57_module.E57(str(source))
        total_scans = _e57_scan_count(reader)
    except Exception as exc:
        raise PointCloudSourceError("e57_open_failed", "E57 source could not be opened.") from exc
    if total_scans < 1 or total_scans > limits.maximum_scans:
        raise PointCloudSourceError("scan_count_limit", "E57 scan count is outside the accepted range.")
    if selected_scan_indices is None:
        if total_scans != 1:
            raise PointCloudSourceError(
                "scan_selection_required", "A multi-scan E57 source requires an explicit merge selection."
            )
        selected = (0,)
    else:
        selected = tuple(int(value) for value in selected_scan_indices)
        if not selected or len(set(selected)) != len(selected) or any(
            value < 0 or value >= total_scans for value in selected
        ):
            raise PointCloudSourceError("invalid_scan_selection", "Selected E57 scans are invalid.")
    if len(selected) > 1:
        if registration is None or registration.selected_scan_indices != selected:
            raise PointCloudSourceError(
                "registration_required", "A multi-scan E57 merge requires a matching registration receipt."
            )
        try:
            registration.require_accepted()
        except CoordinateContractError as exc:
            raise PointCloudSourceError(exc.code, exc.public_message) from exc

    headers: list[Any] = []
    estimated_points = 0
    dimension_names: set[str] = {"x", "y", "z", "scan_id"}
    for scan_index in selected:
        try:
            header = reader.get_header(scan_index)
        except Exception as exc:
            raise PointCloudSourceError("e57_header_failed", "E57 scan metadata could not be read.") from exc
        fields = _e57_header_fields(header)
        if fields and not {"cartesianX", "cartesianY", "cartesianZ"}.issubset(fields):
            raise PointCloudSourceError(
                "e57_cartesian_required", "Spherical-only E57 scans are not supported by this parser profile."
            )
        if len(fields) > limits.maximum_dimensions:
            raise PointCloudSourceError("dimension_count_limit", "E57 dimension count exceeds the limit.")
        point_count = int(getattr(header, "point_count", 0) or 0)
        if point_count < 0:
            raise PointCloudSourceError("invalid_point_count", "E57 point count is invalid.")
        estimated_points += point_count
        headers.append(header)
        dimension_names.update(fields)
    if estimated_points > limits.maximum_points:
        raise PointCloudSourceError("point_count_limit", "E57 point count exceeds the configured limit.")
    estimated_bytes = estimated_points * min(limits.maximum_dimensions, max(4, len(dimension_names))) * 8
    if estimated_bytes > limits.maximum_materialized_bytes:
        raise PointCloudSourceError(
            "materialized_memory_limit", "E57 decompressed memory estimate exceeds the configured limit."
        )

    point_parts: list[np.ndarray] = []
    scan_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray | None] = []
    intensity_parts: list[np.ndarray | None] = []
    timestamp_parts: list[np.ndarray | None] = []
    classification_parts: list[np.ndarray | None] = []
    return_number_parts: list[np.ndarray | None] = []
    return_count_parts: list[np.ndarray | None] = []
    actual_points = 0
    for scan_index, header in zip(selected, headers, strict=True):
        try:
            data = _read_e57_scan_untransformed(reader, scan_index)
            raw = np.column_stack(
                (
                    np.asarray(data["cartesianX"], dtype=np.float64),
                    np.asarray(data["cartesianY"], dtype=np.float64),
                    np.asarray(data["cartesianZ"], dtype=np.float64),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PointCloudSourceError("e57_scan_parse_failed", "E57 Cartesian points could not be read.") from exc
        if raw.ndim != 2 or raw.shape[1] != 3 or not len(raw):
            raise PointCloudSourceError("e57_scan_empty", "A selected E57 scan contains no Cartesian points.")
        if not np.isfinite(raw).all():
            raise PointCloudSourceError("nonfinite_coordinates", "E57 points contain non-finite coordinates.")
        rotation, translation, _homogeneous = _header_transform(header)
        transformed = raw @ rotation.T + translation
        if float(np.max(np.abs(transformed))) > limits.maximum_absolute_coordinate:
            raise PointCloudSourceError(
                "coordinate_magnitude_limit", "E57 coordinates exceed the configured magnitude limit."
            )
        point_parts.append(transformed)
        count = len(transformed)
        actual_points += count
        if actual_points > limits.maximum_points:
            raise PointCloudSourceError("point_count_limit", "Parsed E57 points exceed the configured limit.")
        scan_parts.append(np.full(count, scan_index, dtype=np.int32))
        rgb_parts.append(_normalise_rgb(data, count))
        intensity_parts.append(_optional_attribute(data, "intensity", count))
        timestamp_parts.append(_optional_attribute(data, "timeStamp", count))
        classification_parts.append(_optional_attribute(data, "classification", count))
        return_number_parts.append(
            _optional_attribute(
                data,
                "returnNumber" if "returnNumber" in data else "returnIndex",
                count,
            )
            if "returnNumber" in data or "returnIndex" in data
            else None
        )
        return_count_parts.append(_optional_attribute(data, "returnCount", count))

    world = np.ascontiguousarray(np.concatenate(point_parts, axis=0), dtype=np.float64)
    attributes: dict[str, np.ndarray] = {"scan_id": np.concatenate(scan_parts)}
    for name, parts, width in (
        ("rgb", rgb_parts, 3),
        ("intensity", intensity_parts, 1),
        ("timestamp", timestamp_parts, 1),
        ("classification", classification_parts, 1),
        ("return_number", return_number_parts, 1),
        ("return_count", return_count_parts, 1),
    ):
        if any(part is not None for part in parts):
            filled: list[np.ndarray] = []
            for point_part, value in zip(point_parts, parts, strict=True):
                if value is not None:
                    filled.append(np.asarray(value))
                elif width == 1:
                    filled.append(np.full(len(point_part), np.nan, dtype=np.float64))
                else:
                    filled.append(np.full((len(point_part), width), np.nan, dtype=np.float64))
            attributes[name] = np.concatenate(filled, axis=0)

    frame = coordinate_frame or CoordinateFrame(
        kind=FrameKind.SCANNER_LOCAL,
        horizontal_unit=LinearUnit.METRE,
        vertical_unit=LinearUnit.METRE,
        up_vector=(0.0, 0.0, 1.0),
        gravity_source=GravitySource.UNKNOWN,
        metadata_source="E57 Cartesian coordinates use SI metres; gravity/up was not verified.",
    )
    local_transform: ReversibleCoordinateTransform | None = None
    local_points: np.ndarray | None = None
    issues: list[str] = []
    if frame.metric_ready:
        local_transform = ReversibleCoordinateTransform.from_world_points(world, frame)
        local_points = local_transform.to_local(world)
    else:
        issues.append("units_or_gravity_unverified")
    profile = PointCloudSourceProfile(
        contract_version=SOURCE_CONTRACT_VERSION,
        source_sha256=_sha256_file(source),
        source_size_bytes=size,
        format=PointCloudFormat.E57,
        parser=E57_PARSER_VERSION,
        total_scan_count=total_scans,
        selected_scan_indices=selected,
        source_point_count=estimated_points or actual_points,
        parsed_point_count=actual_points,
        dimensions=tuple(sorted(set(dimension_names) | set(attributes))),
        coordinate_frame=frame,
        registration=registration,
        visualization_only=not frame.metric_ready,
        issues=tuple(issues),
    )
    return ParsedPointCloud(profile, world, local_points, local_transform, attributes)
