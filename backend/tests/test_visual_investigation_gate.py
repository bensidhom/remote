import unittest
from pathlib import Path
from unittest.mock import patch

from app.routers.services import rag_engine
from app.routers.services import vision


class VisualInvestigationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_bridge_ask_excludes_image_descriptions(self):
        chunks = [
            {
                "kind": "text",
                "source": "inspection.pdf",
                "text": "TEXT_REPORT_FACT: the deck rating is 6.",
            },
            {
                "kind": "image",
                "source": "inspection.pdf",
                "text": "IMAGE_ONLY_SECRET: a visual crack description.",
                "caption": "IMAGE_ONLY_SECRET",
                "path": "unused.png",
                "ext": "png",
            },
        ]
        captured_prompt = {}

        def generate(prompt, _model):
            captured_prompt["value"] = prompt
            return "text answer"

        with (
            patch.object(
                vision,
                "ingest_bridge_reports",
                return_value={"chunks": chunks, "embeddings": None, "page_images": []},
            ),
            patch.object(
                rag_engine,
                "_get_relevant",
                side_effect=lambda _question, items, k, embeddings=None: items[:k],
            ),
            patch.object(vision, "bridge_sql_context", return_value=("", "")),
            patch.object(vision, "get_db_context", return_value=("", {})),
            patch.object(vision, "get_cerebras_answer", side_effect=generate),
        ):
            result = await vision.process_bridge_ask(
                "Describe the bridge condition",
                state_code=34,
                bridge_number="TEST-1",
                selected_sources=["inspection.pdf"],
            )

        self.assertIn("TEXT_REPORT_FACT", captured_prompt["value"])
        self.assertNotIn("IMAGE_ONLY_SECRET", captured_prompt["value"])
        self.assertEqual(result["images"], [])

    async def test_dedicated_visual_route_runs_detection(self):
        image_path = Path("inspection.png")
        chunks = [
            {
                "kind": "image",
                "source": "inspection.pdf",
                "text": "Possible deck crack.",
                "caption": "Deck photo",
                "path": str(image_path),
                "ext": "png",
                "page": 2,
            }
        ]
        detections = [{"label": "crack", "confidence": 0.91, "bbox": [1, 2, 3, 4]}]

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(
                vision,
                "ingest_bridge_reports",
                return_value={"chunks": chunks, "embeddings": None},
            ),
            patch.object(
                rag_engine,
                "_get_relevant",
                side_effect=lambda _question, items, k, embeddings=None: items[:k],
            ),
            patch.object(
                rag_engine,
                "_detect_defects_with_bboxes",
                return_value=detections,
            ) as detect,
            patch.object(
                rag_engine,
                "_refine_defect_detections_hybrid",
                return_value=detections,
            ),
            patch.object(
                rag_engine,
                "_annotate_image_with_detections",
                return_value={
                    "filename": "annotated.png",
                    "mime": "image/png",
                    "data": "encoded",
                },
            ),
        ):
            result = await vision.process_bridge_visual_investigation(
                query="find cracks",
                state_code=34,
                bridge_number="TEST-1",
            )

        detect.assert_called_once_with(str(image_path), "find cracks")
        self.assertEqual(result["checked_images"], 1)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["detections"][0]["detections"], detections)


if __name__ == "__main__":
    unittest.main()
