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
    qwen = llm_providers.generation_model_capabilities("qwen-3.8-27b")
    assert qwen["text"] is True
    assert qwen["vision"] is True
    assert qwen["json_schema"] is True
    assert llm_providers.generation_model_capabilities("gemma-4-31b")["vision"] is True
    assert llm_providers.generation_model_capabilities("gpt-oss-120b")["vision"] is False
    with pytest.raises(RuntimeError, match="does not have reviewed vision capability"):
        llm_providers.require_generation_capability("vision", model="gpt-oss-120b")


def test_qwen_is_default_and_explicitly_disables_provider_default_reasoning(
    monkeypatch,
):
    monkeypatch.delenv("CEREBRAS_MODEL", raising=False)
    monkeypatch.delenv("CEREBRAS_VISION_MODEL", raising=False)
    monkeypatch.delenv("CEREBRAS_REASONING_EFFORT", raising=False)

    kwargs = llm_providers._cerebras_create_kwargs(
        None,
        [{"role": "user", "content": "Reply OK"}],
        32,
        0,
        False,
    )

    assert llm_providers.generation_model() == "qwen-3.8-27b"
    assert llm_providers.vision_model() == "qwen-3.8-27b"
    assert kwargs["model"] == "qwen-3.8-27b"
    assert kwargs["reasoning_effort"] == "none"


@pytest.mark.parametrize("configured", ["none", "off", "0"])
def test_reasoning_disable_aliases_are_sent_as_none(monkeypatch, configured):
    monkeypatch.setenv("CEREBRAS_MODEL", "qwen-3.8-27b")
    monkeypatch.setenv("CEREBRAS_REASONING_EFFORT", configured)

    kwargs = llm_providers._cerebras_create_kwargs(
        None,
        [{"role": "user", "content": "Reply OK"}],
        32,
        0,
        False,
    )

    assert kwargs["reasoning_effort"] == "none"


def test_explicit_qwen_reasoning_effort_is_preserved(monkeypatch):
    monkeypatch.setenv("CEREBRAS_MODEL", "qwen-3.8-27b")
    monkeypatch.setenv("CEREBRAS_REASONING_EFFORT", "high")

    kwargs = llm_providers._cerebras_create_kwargs(
        None,
        [{"role": "user", "content": "Reply OK"}],
        512,
        0,
        False,
    )

    assert kwargs["reasoning_effort"] == "high"


def test_cerebras_call_retries_transient_connection_failures(monkeypatch):
    class APIConnectionError(Exception):
        pass

    responses = iter((APIConnectionError(), APIConnectionError(), "completed"))
    def create(**_kwargs):
        result = next(responses)
        if isinstance(result, BaseException):
            raise result
        return result

    completion_api = SimpleNamespace(create=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completion_api))
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    assert llm_providers._cerebras_call(client, {"model": "test"}) == "completed"
    assert sleeps == [1.5, 3.0]


def test_cerebras_call_does_not_retry_permanent_validation_failure(monkeypatch):
    calls = 0

    def create(**_kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("invalid response schema")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(
        "time.sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    with pytest.raises(ValueError, match="invalid response schema"):
        llm_providers._cerebras_call(client, {"model": "test"})
    assert calls == 1


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
        server_model_override="qwen-3.8-27b",
    )

    assert captured["messages"] == messages
    assert captured["response_format"] == schema
    assert captured["model"] == "qwen-3.8-27b"
    assert captured["reasoning_effort"] == "none"
    assert result == {
        "text": '{"regions":[]}',
        "provider": "cerebras",
        "model": "qwen-3.8-27b",
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
