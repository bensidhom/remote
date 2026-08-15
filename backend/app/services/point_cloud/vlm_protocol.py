"""Typed, fail-closed multimodal protocol for point-cloud review.

The provider is allowed to make bounded semantic judgements about server-owned
candidate IDs.  It is never allowed to create IDs, classes, materials, or an
output shape.  This module deliberately contains no provider client code so the
contract and response parser can be tested without network access.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence


VLM_PROTOCOL_VERSION = "bridgeguard-point-cloud-vlm-v1"
MAX_MESSAGE_CHARS = 96 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"


class VlmProtocolError(ValueError):
    code = "point_cloud_vlm_protocol_error"


class VlmCapabilityError(VlmProtocolError):
    code = "point_cloud_vlm_capability_unavailable"


class VlmResponseError(VlmProtocolError):
    code = "point_cloud_vlm_response_invalid"


class VlmRole(str, Enum):
    SYSTEM = "system"
    USER = "user"


class VlmTask(str, Enum):
    COMPONENT = "component_decision"
    SCENE_CELL = "scene_cell_decision"


class ComponentClass(str, Enum):
    DECK_SLAB = "deck_slab"
    GIRDER_BEAM = "girder_beam"
    DIAPHRAGM_BRACING = "diaphragm_bracing"
    BEARING = "bearing"
    PIER_COLUMN = "pier_column"
    ABUTMENT_WINGWALL = "abutment_wingwall"
    PARAPET_RAILING_BARRIER = "parapet_railing_barrier"
    UNDER_SURFACE = "under_surface"
    ATTACHED_RESTRICTION = "attached_restriction_appurtenance"
    NONSTRUCTURAL_CLUTTER = "nonstructural_clutter"
    UNKNOWN_STRUCTURE = "unknown_structure"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class SceneVerdict(str, Enum):
    BRIDGE = "bridge"
    CLUTTER = "clutter"
    MIXED = "mixed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class MaterialClass(str, Enum):
    REINFORCED_CONCRETE = "reinforced_concrete"
    PRESTRESSED_CONCRETE = "prestressed_concrete"
    CONCRETE = "concrete"
    STRUCTURAL_STEEL = "structural_steel"
    TIMBER = "timber"
    MASONRY = "masonry"
    ASPHALT = "asphalt"
    EARTH = "earth"
    WATER = "water"
    UNKNOWN = "unknown"


_DISPLAY_LABELS: Mapping[ComponentClass, str] = {
    ComponentClass.DECK_SLAB: "deck / slab",
    ComponentClass.GIRDER_BEAM: "girder / beam",
    ComponentClass.DIAPHRAGM_BRACING: "diaphragm / bracing",
    ComponentClass.BEARING: "bearing",
    ComponentClass.PIER_COLUMN: "pier / column",
    ComponentClass.ABUTMENT_WINGWALL: "abutment / wingwall",
    ComponentClass.PARAPET_RAILING_BARRIER: "parapet / railing / barrier",
    ComponentClass.UNDER_SURFACE: "surface under the bridge",
    ComponentClass.ATTACHED_RESTRICTION: "attached restriction / appurtenance",
    ComponentClass.NONSTRUCTURAL_CLUTTER: "nonstructural clutter",
    ComponentClass.UNKNOWN_STRUCTURE: "unknown bridge structure",
    ComponentClass.INSUFFICIENT_EVIDENCE: "insufficient evidence",
}


@dataclass(frozen=True, slots=True)
class VlmMessage:
    role: VlmRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, VlmRole):
            raise VlmProtocolError("VLM message role is invalid.")
        if not isinstance(self.content, str) or not self.content.strip():
            raise VlmProtocolError("VLM message content is empty.")
        if len(self.content) > MAX_MESSAGE_CHARS:
            raise VlmProtocolError("VLM message exceeds its character limit.")

    def provider_dict(self) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.content}


@dataclass(frozen=True, slots=True)
class VlmImage:
    image_id: str
    mime_type: str
    data: bytes
    sha256: str
    purpose: str
    view_name: str
    width: int
    height: int
    camera_receipt: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", self.image_id):
            raise VlmProtocolError("VLM image ID is invalid.")
        if self.mime_type not in {"image/png", "image/jpeg"}:
            raise VlmProtocolError("VLM image MIME type is unsupported.")
        if not isinstance(self.data, bytes) or not self.data:
            raise VlmProtocolError("VLM image bytes are empty.")
        if len(self.data) > MAX_IMAGE_BYTES:
            raise VlmProtocolError("VLM image exceeds its byte limit.")
        if self.mime_type == "image/png" and not self.data.startswith(_PNG_SIGNATURE):
            raise VlmProtocolError("VLM image MIME type does not match its bytes.")
        if self.mime_type == "image/jpeg" and not self.data.startswith(_JPEG_SIGNATURE):
            raise VlmProtocolError("VLM image MIME type does not match its bytes.")
        digest = hashlib.sha256(self.data).hexdigest()
        if not _SHA256_RE.fullmatch(self.sha256) or digest != self.sha256:
            raise VlmProtocolError("VLM image hash does not match its bytes.")
        if self.width < 1 or self.height < 1:
            raise VlmProtocolError("VLM image dimensions are invalid.")
        if not self.purpose.strip() or not self.view_name.strip():
            raise VlmProtocolError("VLM image purpose/view is required.")

    def provider_content(self) -> dict[str, Any]:
        encoded = base64.b64encode(self.data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.mime_type};base64,{encoded}",
                "detail": "high",
            },
        }

    def receipt(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "byte_size": len(self.data),
            "purpose": self.purpose,
            "view_name": self.view_name,
            "width": self.width,
            "height": self.height,
            "camera": dict(self.camera_receipt),
        }


@dataclass(frozen=True, slots=True)
class MaterialHypothesis:
    value: MaterialClass
    confidence: float


@dataclass(frozen=True, slots=True)
class VlmAbstention:
    reason: str


@dataclass(frozen=True, slots=True)
class ComponentDecision:
    component_id: int
    is_bridge: bool
    component_class: ComponentClass
    material: MaterialHypothesis
    confidence: float
    evidence: str
    evidence_ids: tuple[str, ...]
    abstention: VlmAbstention | None

    @property
    def display_label(self) -> str:
        return _DISPLAY_LABELS[self.component_class]


@dataclass(frozen=True, slots=True)
class SceneCellDecision:
    cell_id: int
    verdict: SceneVerdict
    component_class: ComponentClass
    confidence: float
    evidence: str
    evidence_ids: tuple[str, ...]
    abstention: VlmAbstention | None


@dataclass(frozen=True, slots=True)
class RestrictionDecision:
    component_id: int
    protected: bool
    reason: str


@dataclass(frozen=True, slots=True)
class VlmCallReceipt:
    protocol_version: str
    task: str
    prompt_version: str
    messages_sha256: str
    response_schema_sha256: str
    image_receipts: tuple[dict[str, Any], ...]
    provider: str
    model: str
    response_sha256: str
    usage: dict[str, int]
    latency_ms: int
    retry_count: int
    termination: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task": self.task,
            "prompt_version": self.prompt_version,
            "messages_sha256": self.messages_sha256,
            "response_schema_sha256": self.response_schema_sha256,
            "images": [dict(item) for item in self.image_receipts],
            "provider": self.provider,
            "model": self.model,
            "response_sha256": self.response_sha256,
            "usage": dict(self.usage),
            "latency_ms": self.latency_ms,
            "retry_count": self.retry_count,
            "termination": self.termination,
        }


class VlmProviderCallError(RuntimeError):
    """Sanitized provider failure carrying a content-free attempt receipt."""

    code = "point_cloud_vlm_provider_failed"

    def __init__(self, receipt: VlmCallReceipt):
        super().__init__("Point-cloud VLM provider call failed.")
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class VlmProviderOutput:
    text: str
    receipt: VlmCallReceipt


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def provider_messages(
    messages: Sequence[VlmMessage], images: Sequence[VlmImage]
) -> list[dict[str, Any]]:
    if not messages or messages[0].role is not VlmRole.SYSTEM:
        raise VlmProtocolError("VLM requests require a leading system message.")
    if len(images) < 1 or len(images) > MAX_IMAGES:
        raise VlmProtocolError("VLM request image count is invalid.")
    if len({image.image_id for image in images}) != len(images):
        raise VlmProtocolError("VLM image IDs must be unique.")
    output = [message.provider_dict() for message in messages]
    user_indices = [
        index for index, message in enumerate(messages) if message.role is VlmRole.USER
    ]
    if len(user_indices) != 1:
        raise VlmProtocolError("VLM requests require exactly one user message.")
    user_index = user_indices[0]
    output[user_index] = {
        "role": "user",
        "content": [
            {"type": "text", "text": messages[user_index].content},
            *(image.provider_content() for image in images),
        ],
    }
    return output


def response_format(
    task: VlmTask,
    expected_ids: Iterable[int],
    allowed_evidence_ids: Iterable[str] = (),
) -> dict[str, Any]:
    ids = tuple(int(value) for value in expected_ids)
    if not ids or len(set(ids)) != len(ids) or any(value < 0 for value in ids):
        raise VlmProtocolError("Expected VLM IDs are invalid.")
    evidence_ids = tuple(str(value) for value in allowed_evidence_ids)
    if (
        len(set(evidence_ids)) != len(evidence_ids)
        or len(evidence_ids) > 512
        or any(not value or len(value) > 200 for value in evidence_ids)
    ):
        raise VlmProtocolError("Allowed VLM evidence IDs are invalid.")
    evidence_items: dict[str, Any] = {"type": "string", "maxLength": 200}
    if evidence_ids:
        evidence_items["enum"] = list(evidence_ids)
    common = {
        "id": {"type": "integer", "enum": list(ids)},
        "component_class": {
            "type": "string",
            "enum": [item.value for item in ComponentClass],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "string", "maxLength": 500},
        "evidence_ids": {
            "type": "array",
            "items": evidence_items,
            "uniqueItems": True,
            "maxItems": min(8, len(evidence_ids)),
        },
        "abstained": {"type": "boolean"},
    }
    if task is VlmTask.COMPONENT:
        row_properties = {
            **common,
            "is_bridge": {"type": "boolean"},
            "material": {
                "type": "string",
                "enum": [item.value for item in MaterialClass],
            },
        }
        root_key = "regions"
    else:
        row_properties = {
            **common,
            "verdict": {
                "type": "string",
                "enum": [item.value for item in SceneVerdict],
            },
        }
        root_key = "cells"
    row = {
        "type": "object",
        "properties": row_properties,
        "required": list(row_properties),
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            root_key: {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": row,
            }
        },
        "required": [root_key],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"point_cloud_{task.value}_v1",
            "strict": True,
            "schema": schema,
        },
    }


def _reject_constant(value: str) -> None:
    raise VlmResponseError(f"Non-finite JSON constant {value!r} is not allowed.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise VlmResponseError("Duplicate JSON keys are not allowed.")
        output[key] = value
    return output


def strict_json_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise VlmResponseError("VLM response is empty.")
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise VlmResponseError("VLM response exceeds its byte limit.")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except VlmResponseError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VlmResponseError("VLM response is not one strict JSON object.") from exc
    if not isinstance(value, dict):
        raise VlmResponseError("VLM response root must be an object.")
    return value


def _bounded_text(value: Any, field: str, *, limit: int = 500) -> str:
    if not isinstance(value, str):
        raise VlmResponseError(f"VLM {field} must be a string.")
    text = value.strip()
    if len(text) > limit:
        raise VlmResponseError(f"VLM {field} exceeds its length limit.")
    return text


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VlmResponseError("VLM confidence must be numeric.")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise VlmResponseError("VLM confidence must be finite and between 0 and 1.")
    return result


def _evidence_ids(value: Any, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise VlmResponseError("VLM evidence IDs must be a bounded array.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise VlmResponseError("VLM evidence ID is invalid.")
        if item not in allowed or item in result:
            raise VlmResponseError("VLM evidence ID is unknown or duplicated.")
        result.append(item)
    return tuple(result)


def _rows(
    raw: str,
    *,
    root_key: str,
    expected_ids: Iterable[int],
    allowed_keys: frozenset[str],
) -> list[dict[str, Any]]:
    data = strict_json_object(raw)
    if set(data) != {root_key} or not isinstance(data[root_key], list):
        raise VlmResponseError("VLM response root shape is invalid.")
    expected = tuple(int(value) for value in expected_ids)
    rows = data[root_key]
    if len(rows) != len(expected):
        raise VlmResponseError("VLM response omitted or added candidate decisions.")
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != allowed_keys:
            raise VlmResponseError("VLM response decision shape is invalid.")
        identifier = row.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            raise VlmResponseError("VLM response ID must be an integer.")
        if identifier not in expected or identifier in seen:
            raise VlmResponseError("VLM response contains an unknown or duplicate ID.")
        seen.add(identifier)
    if seen != set(expected):
        raise VlmResponseError("VLM response does not cover every candidate ID.")
    return rows


def parse_component_decisions(
    raw: str,
    expected_ids: Iterable[int],
    allowed_evidence_ids: Iterable[str] = (),
) -> tuple[ComponentDecision, ...]:
    keys = frozenset(
        {
            "id",
            "is_bridge",
            "component_class",
            "material",
            "confidence",
            "evidence",
            "evidence_ids",
            "abstained",
        }
    )
    rows = _rows(
        raw, root_key="regions", expected_ids=expected_ids, allowed_keys=keys
    )
    output: list[ComponentDecision] = []
    allowed_evidence = frozenset(str(value) for value in allowed_evidence_ids)
    for row in rows:
        if type(row["is_bridge"]) is not bool or type(row["abstained"]) is not bool:
            raise VlmResponseError("VLM boolean decision fields are invalid.")
        try:
            component_class = ComponentClass(row["component_class"])
            material = MaterialClass(row["material"])
        except (TypeError, ValueError) as exc:
            raise VlmResponseError("VLM component class or material is invalid.") from exc
        confidence = _confidence(row["confidence"])
        evidence = _bounded_text(row["evidence"], "evidence")
        evidence_ids = _evidence_ids(row["evidence_ids"], allowed_evidence)
        abstained = row["abstained"]
        if abstained and component_class is not ComponentClass.INSUFFICIENT_EVIDENCE:
            raise VlmResponseError("An abstention must use insufficient_evidence.")
        if not abstained and component_class is ComponentClass.INSUFFICIENT_EVIDENCE:
            raise VlmResponseError("insufficient_evidence must be an abstention.")
        output.append(
            ComponentDecision(
                component_id=row["id"],
                is_bridge=row["is_bridge"],
                component_class=component_class,
                material=MaterialHypothesis(material, confidence),
                confidence=confidence,
                evidence=evidence,
                evidence_ids=evidence_ids,
                abstention=(VlmAbstention(evidence or "insufficient visual evidence") if abstained else None),
            )
        )
    return tuple(output)


def parse_scene_cell_decisions(
    raw: str,
    expected_ids: Iterable[int],
    allowed_evidence_ids: Iterable[str] = (),
) -> tuple[SceneCellDecision, ...]:
    keys = frozenset(
        {
            "id",
            "verdict",
            "component_class",
            "confidence",
            "evidence",
            "evidence_ids",
            "abstained",
        }
    )
    rows = _rows(raw, root_key="cells", expected_ids=expected_ids, allowed_keys=keys)
    output: list[SceneCellDecision] = []
    allowed_evidence = frozenset(str(value) for value in allowed_evidence_ids)
    for row in rows:
        if type(row["abstained"]) is not bool:
            raise VlmResponseError("VLM abstention field is invalid.")
        try:
            verdict = SceneVerdict(row["verdict"])
            component_class = ComponentClass(row["component_class"])
        except (TypeError, ValueError) as exc:
            raise VlmResponseError("VLM scene verdict or component class is invalid.") from exc
        confidence = _confidence(row["confidence"])
        evidence = _bounded_text(row["evidence"], "evidence")
        evidence_ids = _evidence_ids(row["evidence_ids"], allowed_evidence)
        abstained = row["abstained"]
        if abstained != (verdict is SceneVerdict.INSUFFICIENT_EVIDENCE):
            raise VlmResponseError("VLM scene abstention and verdict disagree.")
        output.append(
            SceneCellDecision(
                cell_id=row["id"],
                verdict=verdict,
                component_class=component_class,
                confidence=confidence,
                evidence=evidence,
                evidence_ids=evidence_ids,
                abstention=(VlmAbstention(evidence or "insufficient visual evidence") if abstained else None),
            )
        )
    return tuple(output)


def build_call_receipt(
    *,
    task: VlmTask,
    prompt_version: str,
    messages: Sequence[VlmMessage],
    images: Sequence[VlmImage],
    response_format_value: Mapping[str, Any],
    provider: str,
    model: str,
    response_text: str,
    usage: Mapping[str, Any] | None,
    latency_ms: int,
    retry_count: int,
    termination: str,
) -> VlmCallReceipt:
    if not prompt_version.strip() or not provider.strip() or not model.strip():
        raise VlmProtocolError("VLM receipt identity is incomplete.")
    normalized_usage = {
        key: int((usage or {}).get(key) or 0)
        for key in ("prompt_tokens", "completion_tokens", "tokens")
    }
    if any(value < 0 for value in normalized_usage.values()):
        raise VlmProtocolError("VLM usage receipt is invalid.")
    return VlmCallReceipt(
        protocol_version=VLM_PROTOCOL_VERSION,
        task=task.value,
        prompt_version=prompt_version,
        messages_sha256=_sha256_json(
            [message.provider_dict() for message in messages]
        ),
        response_schema_sha256=_sha256_json(response_format_value),
        image_receipts=tuple(image.receipt() for image in images),
        provider=provider,
        model=model,
        response_sha256=hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
        usage=normalized_usage,
        latency_ms=max(0, int(latency_ms)),
        retry_count=max(0, int(retry_count)),
        termination=str(termination),
    )


__all__ = [
    "ComponentClass",
    "ComponentDecision",
    "MaterialClass",
    "MaterialHypothesis",
    "RestrictionDecision",
    "SceneCellDecision",
    "SceneVerdict",
    "VLM_PROTOCOL_VERSION",
    "VlmAbstention",
    "VlmCallReceipt",
    "VlmCapabilityError",
    "VlmImage",
    "VlmMessage",
    "VlmProtocolError",
    "VlmProviderCallError",
    "VlmProviderOutput",
    "VlmResponseError",
    "VlmRole",
    "VlmTask",
    "build_call_receipt",
    "parse_component_decisions",
    "parse_scene_cell_decisions",
    "provider_messages",
    "response_format",
    "strict_json_object",
]
