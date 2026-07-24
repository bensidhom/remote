import base64
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.routers import point_cloud_api
from app.routers.services import point_cloud


class HostedPointCloudTests(unittest.TestCase):
    def test_hosted_vlm_uses_configured_provider_without_model_hint(self):
        with patch.object(
            point_cloud.llm_providers,
            "generate",
            return_value='{"keep": true}',
        ) as generate:
            response = point_cloud._hosted_vlm_generate(
                "Inspect this scene.",
                system="Return JSON only.",
                images_b64=["abc"],
                timeout=42,
                num_predict=128,
            )

        self.assertEqual(response, '{"keep": true}')
        args, kwargs = generate.call_args
        self.assertEqual(args[0], "Return JSON only.\n\nInspect this scene.")
        self.assertEqual(kwargs["images_b64"], ["abc"])
        self.assertEqual(kwargs["timeout"], 42)
        self.assertEqual(kwargs["num_predict"], 128)
        self.assertNotIn("model", kwargs)

    def test_processed_cloud_is_persisted_as_npz_and_ply(self):
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

        root = Path(__file__).resolve().parents[1] / "data" / f"point_cloud_test_{uuid.uuid4().hex}"
        root.mkdir()
        try:
            with (
                patch.object(point_cloud_api, "ATTACHMENTS_DIR", root / "attachments"),
                patch.object(point_cloud_api, "DATA_DIR", root),
            ):
                persisted = point_cloud_api._save_processed("34", "bridge-1", points, result)
                loaded = point_cloud_api._load_processed("34", "bridge-1")
                preview = point_cloud_api._processed_preview("34", "bridge-1", as_arrays=True)

                self.assertEqual(persisted["source_point_count"], 3)
                self.assertEqual(persisted["processed_point_count"], 2)
                self.assertEqual(loaded["component_count"], 2)
                self.assertEqual(preview["rendered_point_count"], 2)
                self.assertEqual(preview["points"].shape, (2, 3))
                self.assertTrue(
                    (root / "attachments" / "point_clouds" / "34_bridge-1" /
                     "processed" / "processed_cloud.ply").is_file()
                )
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
