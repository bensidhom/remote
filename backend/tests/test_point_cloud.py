import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app.routers import point_cloud_api
from app.routers.services import point_cloud
from app.services.point_cloud.vlm_protocol import (
    VlmImage,
    VlmProviderCallError,
    VlmTask,
)


class TestHostedPointCloud:
    def test_hosted_vlm_uses_configured_provider_without_model_hint(self):
        image_bytes = b"\x89PNG\r\n\x1a\npoint-cloud-test"
        image = VlmImage(
            image_id="test-oblique",
            mime_type="image/png",
            data=image_bytes,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            purpose="component-review",
            view_name="oblique",
            width=10,
            height=10,
            camera_receipt={"projection": "orthographic"},
        )
        with patch.object(
            point_cloud.llm_providers,
            "generate_messages",
            return_value={
                "text": '{"regions":[]}',
                "provider": "cerebras",
                "model": "gemma-4-31b",
                "usage": {"tokens": 4},
            },
        ) as generate_messages, patch.object(
            point_cloud.llm_providers,
            "vision_model",
            return_value="gemma-4-31b",
        ), patch.object(point_cloud.llm_providers, "require_generation_capability"):
            response = point_cloud._hosted_vlm_generate(
                "Inspect this scene.",
                system="Return JSON only.",
                images=[image],
                task=VlmTask.COMPONENT,
                expected_ids=[1],
                prompt_version="test-v1",
                timeout=42,
                num_predict=128,
            )

        assert response.text == '{"regions":[]}'
        args, kwargs = generate_messages.call_args
        assert args[0][0] == {"role": "system", "content": "Return JSON only."}
        assert args[0][1]["role"] == "user"
        assert args[0][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        assert kwargs["timeout"] == 42
        assert kwargs["num_predict"] == 128
        assert kwargs["server_model_override"] == "gemma-4-31b"
        assert kwargs["response_format"]["json_schema"]["strict"] is True
        assert response.receipt.image_receipts[0]["sha256"] == image.sha256

    def test_hosted_vlm_timeout_is_sanitized_and_receipted(self):
        image_bytes = b"\x89PNG\r\n\x1a\npoint-cloud-timeout"
        image = VlmImage(
            image_id="timeout-oblique",
            mime_type="image/png",
            data=image_bytes,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            purpose="component-review",
            view_name="oblique",
            width=10,
            height=10,
            camera_receipt={"projection": "orthographic"},
        )
        with patch.object(
            point_cloud.llm_providers,
            "generate_messages",
            side_effect=TimeoutError("private provider detail"),
        ), patch.object(
            point_cloud.llm_providers,
            "vision_model",
            return_value="gemma-4-31b",
        ), patch.object(point_cloud.llm_providers, "require_generation_capability"):
            with pytest.raises(VlmProviderCallError) as captured:
                point_cloud._hosted_vlm_generate(
                    "Inspect.",
                    system="Policy.",
                    images=[image],
                    task=VlmTask.COMPONENT,
                    expected_ids=[1],
                    prompt_version="test-v1",
                )

        receipt = captured.value.receipt.to_dict()
        assert str(captured.value) == "Point-cloud VLM provider call failed."
        assert receipt["termination"] == "timeout"
        assert "private provider detail" not in json.dumps(receipt)

    def test_processed_cloud_is_persisted_as_npz_and_ply(self, test_workspace):
        points = np.asarray(
            [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]],
            dtype=np.float32,
        )
        labels = np.asarray([1, 0, 2], dtype=np.uint8)
        result = {
            "labels_b64u8": base64.b64encode(labels.tobytes()).decode("ascii"),
            "classes": [
                {"id": 1, "color_rgb": [1.0, 0.0, 0.0]},
                {"id": 2, "color_rgb": [0.0, 0.0, 1.0]},
            ],
            "component_count": 2,
        }

        with (
            patch.object(
                point_cloud_api,
                "ATTACHMENTS_DIR",
                test_workspace / "attachments",
            ),
            patch.object(point_cloud_api, "DATA_DIR", test_workspace),
        ):
            persisted = point_cloud_api._save_processed(
                "34", "bridge-1", points, result
            )
            loaded = point_cloud_api._load_processed("34", "bridge-1")
            preview = point_cloud_api._processed_preview(
                "34", "bridge-1", as_arrays=True
            )

            assert persisted["source_point_count"] == 3
            assert persisted["processed_point_count"] == 2
            assert loaded["component_count"] == 2
            assert preview["rendered_point_count"] == 2
            assert preview["points"].shape == (2, 3)
            assert Path(persisted["point_cloud_file"]).name == "processed_cloud.ply"
            assert (
                test_workspace / persisted["point_cloud_file"]
            ).is_file()
            assert persisted["analysis_version"] == 2
            assert persisted["source_sha256"]
            assert persisted["analysis_profile_sha256"]
