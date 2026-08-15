from __future__ import annotations

import hashlib
import json

import pytest

from app.services.point_cloud.vlm_protocol import (
    ComponentClass,
    MaterialClass,
    SceneVerdict,
    VlmImage,
    VlmMessage,
    VlmProtocolError,
    VlmResponseError,
    VlmRole,
    VlmTask,
    build_call_receipt,
    parse_component_decisions,
    parse_scene_cell_decisions,
    provider_messages,
    response_format,
    strict_json_object,
)


_PNG = b"\x89PNG\r\n\x1a\n" + b"test-png"


def _image(identifier: str = "scene-oblique") -> VlmImage:
    return VlmImage(
        image_id=identifier,
        mime_type="image/png",
        data=_PNG,
        sha256=hashlib.sha256(_PNG).hexdigest(),
        purpose="scene",
        view_name="oblique",
        width=20,
        height=10,
        camera_receipt={"projection": "orthographic"},
    )


def _component(identifier: int, **overrides):
    value = {
        "id": identifier,
        "is_bridge": True,
        "component_class": "deck_slab",
        "material": "reinforced_concrete",
        "confidence": 0.9,
        "evidence": "planar elevated region",
        "evidence_ids": [],
        "abstained": False,
    }
    value.update(overrides)
    return value


def _cell(identifier: int, **overrides):
    value = {
        "id": identifier,
        "verdict": "bridge",
        "component_class": "girder_beam",
        "confidence": 0.8,
        "evidence": "linear structural region",
        "evidence_ids": [],
        "abstained": False,
    }
    value.update(overrides)
    return value


def test_typed_messages_preserve_roles_and_exact_png_mime():
    messages = (
        VlmMessage(VlmRole.SYSTEM, "policy"),
        VlmMessage(VlmRole.USER, "task"),
    )
    provider = provider_messages(messages, (_image(),))

    assert provider[0] == {"role": "system", "content": "policy"}
    assert provider[1]["role"] == "user"
    assert provider[1]["content"][0] == {"type": "text", "text": "task"}
    assert provider[1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


@pytest.mark.parametrize(
    ("mime", "payload"),
    [("image/jpeg", _PNG), ("image/png", b"not-a-png")],
)
def test_image_mime_must_match_bytes(mime, payload):
    with pytest.raises(VlmProtocolError):
        VlmImage(
            image_id="bad",
            mime_type=mime,
            data=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            purpose="scene",
            view_name="side",
            width=1,
            height=1,
            camera_receipt={},
        )


def test_response_schema_is_strict_and_enumerates_server_ids():
    result = response_format(VlmTask.COMPONENT, (4, 9))
    schema = result["json_schema"]["schema"]
    row = schema["properties"]["regions"]["items"]

    assert result["json_schema"]["strict"] is True
    assert schema["additionalProperties"] is False
    assert row["additionalProperties"] is False
    assert row["properties"]["id"]["enum"] == [4, 9]
    assert set(row["required"]) == set(row["properties"])


def test_component_response_requires_exact_coverage_and_typed_values():
    raw = json.dumps({"regions": [_component(2), _component(7)]})
    decisions = parse_component_decisions(raw, (2, 7))

    assert [item.component_id for item in decisions] == [2, 7]
    assert decisions[0].component_class is ComponentClass.DECK_SLAB
    assert decisions[0].material.value is MaterialClass.REINFORCED_CONCRETE
    assert decisions[0].display_label == "deck / slab"


def test_component_evidence_ids_are_allowlisted_and_preserved():
    evidence_id = "snbi:/SpanSets/0/BSP04"
    raw = json.dumps(
        {"regions": [_component(2, evidence_ids=[evidence_id])]}
    )
    result = parse_component_decisions(raw, (2,), (evidence_id,))[0]
    assert result.evidence_ids == (evidence_id,)

    with pytest.raises(VlmResponseError):
        parse_component_decisions(raw, (2,), ("snbi:/BG01",))


@pytest.mark.parametrize(
    "payload",
    [
        {"regions": [_component(2)]},
        {"regions": [_component(2), _component(2)]},
        {"regions": [_component(2), _component(99)]},
        {"regions": [{**_component(2), "extra": 1}, _component(7)]},
        {"regions": [_component(2, confidence=float("nan")), _component(7)]},
        {"regions": [_component(2, component_class="invented"), _component(7)]},
    ],
)
def test_component_response_rejects_missing_duplicate_unknown_extra_nonfinite_or_class_injection(
    payload,
):
    raw = json.dumps(payload, allow_nan=True)
    with pytest.raises(VlmResponseError):
        parse_component_decisions(raw, (2, 7))


def test_abstention_is_explicit_and_cannot_hide_behind_another_class():
    raw = json.dumps(
        {
            "regions": [
                _component(
                    2,
                    component_class="insufficient_evidence",
                    material="unknown",
                    confidence=0.1,
                    evidence="occluded",
                    abstained=True,
                )
            ]
        }
    )
    decision = parse_component_decisions(raw, (2,))[0]
    assert decision.abstention.reason == "occluded"

    invalid = json.dumps({"regions": [_component(2, abstained=True)]})
    with pytest.raises(VlmResponseError):
        parse_component_decisions(invalid, (2,))


def test_scene_cells_use_closed_verdicts_and_cover_all_ids():
    raw = json.dumps({"cells": [_cell(1), _cell(3, verdict="mixed")]})
    result = parse_scene_cell_decisions(raw, (1, 3))
    assert result[0].verdict is SceneVerdict.BRIDGE
    assert result[1].verdict is SceneVerdict.MIXED


def test_strict_json_never_extracts_an_object_from_prose_or_duplicate_keys():
    with pytest.raises(VlmResponseError):
        strict_json_object('Here is JSON: {"regions": []}')
    with pytest.raises(VlmResponseError):
        strict_json_object('{"regions":[],"regions":[]}')
    with pytest.raises(VlmResponseError):
        strict_json_object('{"value":Infinity}')


def test_call_receipt_hashes_prompts_images_schema_and_response_without_content():
    messages = (
        VlmMessage(VlmRole.SYSTEM, "private policy"),
        VlmMessage(VlmRole.USER, "private prompt"),
    )
    image = _image()
    schema = response_format(VlmTask.COMPONENT, (1,))
    receipt = build_call_receipt(
        task=VlmTask.COMPONENT,
        prompt_version="component-v1",
        messages=messages,
        images=(image,),
        response_format_value=schema,
        provider="cerebras",
        model="gemma-4-31b",
        response_text='{"regions":[]}',
        usage={"prompt_tokens": 10, "completion_tokens": 3, "tokens": 13},
        latency_ms=25,
        retry_count=0,
        termination="completed",
    ).to_dict()

    serialized = json.dumps(receipt, sort_keys=True)
    assert "private policy" not in serialized
    assert "private prompt" not in serialized
    assert receipt["images"][0]["sha256"] == image.sha256
    assert receipt["usage"]["tokens"] == 13
