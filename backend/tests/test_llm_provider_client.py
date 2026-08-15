from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.routers.services import llm_providers


class _FakeCerebras:
    instances: list["_FakeCerebras"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.instances.append(self)


def test_cerebras_client_cache_honors_timeout_and_disables_sdk_retries(
    monkeypatch,
) -> None:
    fake_sdk = ModuleType("cerebras.cloud.sdk")
    fake_sdk.Cerebras = _FakeCerebras
    monkeypatch.setitem(sys.modules, "cerebras.cloud.sdk", fake_sdk)
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(llm_providers, "_cerebras_client", None)
    monkeypatch.setattr(llm_providers, "_cerebras_client_key", None)
    monkeypatch.setattr(llm_providers, "_cerebras_client_timeout", None)
    _FakeCerebras.instances.clear()

    first = llm_providers._get_cerebras_client(120)
    assert llm_providers._get_cerebras_client(120) is first
    second = llm_providers._get_cerebras_client(30)

    assert second is not first
    assert [item.kwargs["timeout"] for item in _FakeCerebras.instances] == [120.0, 30.0]
    assert all(item.kwargs["max_retries"] == 0 for item in _FakeCerebras.instances)


def test_model_capabilities_keep_text_only_gpt_oss_out_of_vision():
    assert llm_providers.generation_model_capabilities("gemma-4-31b")["vision"] is True
    assert llm_providers.generation_model_capabilities("gpt-oss-120b")["vision"] is False
    with pytest.raises(RuntimeError, match="does not have reviewed vision capability"):
        llm_providers.require_generation_capability("vision", model="gpt-oss-120b")


def test_typed_multimodal_generation_preserves_roles_png_mime_schema_and_receipt(
    monkeypatch,
):
    captured = {}
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"regions":[]}'))],
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=4,
            total_tokens=16,
        ),
    )
    monkeypatch.setattr(llm_providers, "_get_cerebras_client", lambda _timeout: object())

    def call(_client, kwargs, retries=3):
        captured.update(kwargs)
        return completion

    monkeypatch.setattr(llm_providers, "_cerebras_call", call)
    monkeypatch.setattr(llm_providers, "_record_usage", lambda *_args, **_kwargs: None)
    schema = {"type": "json_schema", "json_schema": {"name": "test", "strict": True}}
    messages = [
        {"role": "system", "content": "policy"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "task"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgo=",
                        "detail": "high",
                    },
                },
            ],
        },
    ]

    result = llm_providers.generate_messages(
        messages,
        response_format=schema,
        server_model_override="gemma-4-31b",
    )

    assert captured["messages"] == messages
    assert captured["response_format"] == schema
    assert captured["model"] == "gemma-4-31b"
    assert result == {
        "text": '{"regions":[]}',
        "provider": "cerebras",
        "model": "gemma-4-31b",
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "tokens": 16},
        "has_images": True,
    }


def test_typed_multimodal_generation_rejects_text_only_override_before_network(
    monkeypatch,
):
    monkeypatch.setattr(
        llm_providers,
        "_get_cerebras_client",
        lambda _timeout: (_ for _ in ()).throw(AssertionError("network must not run")),
    )
    with pytest.raises(RuntimeError, match="does not have reviewed vision capability"):
        llm_providers.generate_messages(
            [
                {"role": "system", "content": "policy"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "task"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,iVBORw0KGgo=",
                                "detail": "high",
                            },
                        },
                    ],
                },
            ],
            server_model_override="gpt-oss-120b",
        )
