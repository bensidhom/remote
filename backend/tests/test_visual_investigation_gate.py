import unittest
import hashlib
import json
import numpy as np
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.routers.services import rag_engine
from app.routers.services import vision
from app.services.ai.context import (
    AiRequestCancelled,
    AiRequestRegistry,
    VisualAnalysisLimits,
)
from app.services.bridge_identity import BridgeSnapshotIdentity


def _published_identity() -> BridgeSnapshotIdentity:
    return BridgeSnapshotIdentity(
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="TEST-1",
        data_year=2025,
        snapshot_revision_id="revision-1",
        revision_number=1,
        canonical_sha256="a" * 64,
        profile_id="profile-1",
        rule_bundle_sha256="b" * 64,
        published_at="2026-08-13T12:00:00Z",
        data_generation=1,
        schema_generation=7,
    )


class VisualInvestigationGateTests(unittest.IsolatedAsyncioTestCase):
    def test_image_description_query_removes_conversational_filler(self):
        self.assertEqual(
            vision._image_description_retrieval_question(
                "Is there any rust in the inspection report?"
            ),
            "rust",
        )

    def test_visual_retrieval_recovers_a_typo_from_report_vocabulary(self):
        profile = dict(vision._active_report_rag_profiles("visual").retrieval)
        chunks = [{
            "chunk_id": "1" * 64,
            "kind": "image",
            "source": "inspection.pdf",
            "text": "Priority repair: major superstructure defects.",
            "path": "defect.png",
            "page": 87,
        }]
        ingested = {
            "chunks": chunks,
            "embeddings": None,
            "lexical_index": vision._build_report_bm25_index(chunks, profile),
            "retrieval_profile": profile,
        }
        with patch.object(
            vision.llm_providers,
            "embed_query",
            side_effect=RuntimeError("embedding service unavailable"),
        ):
            ranked, evidence = vision._retrieve_visual_candidate_chunks(
                "is there any defcts?",
                ingested,
                limit=8,
            )

        self.assertEqual([chunk["page"] for chunk in ranked], [87])
        self.assertEqual(
            evidence["spelling_expansions"],
            [{"term": "defcts", "matches": ["defects"]}],
        )
        self.assertIn("typo_tolerant_lexical", evidence["channels"])
        self.assertFalse(evidence["fallback_used"])

    def test_visual_retrieval_accepts_semantic_candidates_without_word_overlap(self):
        chunks = [
            {
                "chunk_id": "1" * 64,
                "kind": "image",
                "source": "inspection.pdf",
                "text": "Brown oxidation is visible on the steel girder.",
                "path": "rust.png",
                "page": 7,
            },
            {
                "chunk_id": "2" * 64,
                "kind": "image",
                "source": "inspection.pdf",
                "text": "A clean concrete abutment is shown.",
                "path": "abutment.png",
                "page": 9,
            },
        ]
        profile = dict(vision._active_report_rag_profiles("visual").retrieval)
        profile["minimum_dense_cosine"] = 0.5
        ingested = {
            "chunks": chunks,
            "embeddings": np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
            "lexical_index": vision._build_report_bm25_index(chunks, profile),
            "retrieval_profile": profile,
        }
        with (
            patch.object(vision, "_retrieve_report_chunks", return_value=[]),
            patch.object(
                vision.llm_providers,
                "embed_query",
                return_value=np.asarray([[0.95, 0.05]], dtype=np.float32),
            ),
        ):
            ranked, evidence = vision._retrieve_visual_candidate_chunks(
                "Does the steel show deterioration?",
                ingested,
                limit=2,
            )

        self.assertEqual([chunk["page"] for chunk in ranked], [7])
        self.assertIn("dense_semantic", evidence["channels"])
        self.assertTrue(
            ranked[0]["retrieval"]["semantic_candidate_without_lexical_gate"]
        )

    def test_visual_retrieval_falls_back_to_a_bounded_diverse_scan(self):
        chunks = [
            {
                "kind": "image",
                "source": "inspection.pdf",
                "text": f"Photo {page}",
                "path": f"image-{page}.png",
                "page": page,
            }
            for page in range(1, 11)
        ]
        with patch.object(vision, "_retrieve_report_chunks", return_value=[]):
            ranked, evidence = vision._retrieve_visual_candidate_chunks(
                "look for an unusual visible issue",
                {"chunks": chunks, "embeddings": None},
                limit=3,
            )

        self.assertEqual([chunk["page"] for chunk in ranked], [1, 5, 10])
        self.assertEqual(evidence["channels"], ["bounded_diverse_visual_scan"])
        self.assertTrue(evidence["fallback_used"])

    def test_strict_visual_detection_surfaces_provider_failure(self):
        with (
            patch.object(rag_engine, "_resize_image_b64", return_value="encoded"),
            patch.object(
                rag_engine.llm_providers,
                "generate",
                side_effect=RuntimeError("provider unavailable"),
            ),
        ):
            self.assertEqual(
                rag_engine._detect_defects_with_bboxes("image.png", "rust"),
                [],
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "visual_detection_provider_failed",
            ):
                rag_engine._detect_defects_with_bboxes(
                    "image.png",
                    "rust",
                    raise_on_provider_error=True,
                )

    def test_verified_legacy_image_descriptions_bind_to_current_report_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache_dir = root / "rag_storage" / "bridge_reports"
            report_path = root / "inspection.pdf"
            report_bytes = b"current-report-bytes"
            report_path.write_bytes(report_bytes)
            report_sha256 = hashlib.sha256(report_bytes).hexdigest()
            image_dir = (
                root / "rag_storage" / "extracted_images" / report_sha256[:16]
            )
            image_dir.mkdir(parents=True)
            image_path = image_dir / "rust.png"
            image_path.write_bytes(b"previously-described-image")
            cache_dir.mkdir(parents=True)
            (cache_dir / "34_TEST-1.json").write_text(
                json.dumps({
                    "sig": "stale-mtime-signature",
                    "chunks": [{
                        "kind": "image",
                        "source": "inspection.pdf",
                        "text": "Previously indexed description: rust on the girder.",
                        "path": str(image_path.relative_to(root)),
                        "ext": "png",
                        "page": 7,
                        "caption": "Girder rust",
                    }],
                }),
                encoding="utf-8",
            )
            report_file = {
                "id": 5,
                "filename": "inspection.pdf",
                "filepath": str(report_path),
                "size_bytes": len(report_bytes),
            }
            with (
                patch.object(vision, "_bridge_cache_dir", return_value=cache_dir),
                patch.object(
                    vision,
                    "_existing_report_files",
                    return_value=[report_file],
                ),
                patch.object(
                    vision,
                    "_resolve_data_path",
                    side_effect=lambda value: str(root / value),
                ),
            ):
                result = vision._load_verified_legacy_image_description_index(
                    34,
                    "TEST-1",
                    workspace_id="workspace-a",
                    inventory_identity=_published_identity(),
                    selected_sources=["inspection.pdf"],
                )

        self.assertIsNotNone(result)
        self.assertEqual(len(result["chunks"]), 1)
        chunk = result["chunks"][0]
        self.assertEqual(chunk["report_sha256"], report_sha256)
        self.assertEqual(chunk["citation"]["report_sha256"], report_sha256)
        self.assertIn("rust", chunk["text"])
        self.assertEqual(result["lexical_index"]["document_count"], 1)
        self.assertFalse(result["rag_manifest"]["raw_image_reinspection"])

    def test_verified_legacy_image_descriptions_reject_wrong_extraction_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache_dir = root / "rag_storage" / "bridge_reports"
            report_path = root / "inspection.pdf"
            report_bytes = b"current-report-bytes"
            report_path.write_bytes(report_bytes)
            wrong_dir = root / "rag_storage" / "extracted_images" / ("f" * 16)
            wrong_dir.mkdir(parents=True)
            image_path = wrong_dir / "rust.png"
            image_path.write_bytes(b"unbound-image")
            cache_dir.mkdir(parents=True)
            (cache_dir / "34_TEST-1.json").write_text(
                json.dumps({
                    "chunks": [{
                        "kind": "image",
                        "source": "inspection.pdf",
                        "text": "Rust on a girder.",
                        "path": str(image_path.relative_to(root)),
                        "page": 7,
                    }],
                }),
                encoding="utf-8",
            )
            report_file = {
                "id": 5,
                "filename": "inspection.pdf",
                "filepath": str(report_path),
                "size_bytes": len(report_bytes),
            }
            with (
                patch.object(vision, "_bridge_cache_dir", return_value=cache_dir),
                patch.object(
                    vision,
                    "_existing_report_files",
                    return_value=[report_file],
                ),
                patch.object(
                    vision,
                    "_resolve_data_path",
                    side_effect=lambda value: str(root / value),
                ),
            ):
                result = vision._load_verified_legacy_image_description_index(
                    34,
                    "TEST-1",
                    workspace_id="workspace-a",
                    inventory_identity=_published_identity(),
                    selected_sources=["inspection.pdf"],
                )

        self.assertIsNone(result)

    async def test_normal_bridge_ask_reuses_indexed_description_without_inspection(self):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "indexed.png"
            image_path.write_bytes(b"previously-indexed-image")
            chunks = [
                {
                    "kind": "text",
                    "source": "inspection.pdf",
                    "text": "TEXT_REPORT_FACT: the deck rating is 6.",
                },
                {
                    "kind": "image",
                    "source": "inspection.pdf",
                    "text": "INDEXED_IMAGE_DESCRIPTION: a previously described crack.",
                    "caption": "Deck crack",
                    "path": str(image_path),
                    "ext": "png",
                    "page": 4,
                },
            ]
            captured_prompt = {}

            def generate(prompt, _model):
                captured_prompt["value"] = prompt
                return "text answer"

            def retrieve(_question, result, **kwargs):
                allowed = kwargs.get("allowed_kinds")
                return [
                    chunk for chunk in result["chunks"]
                    if allowed is None or chunk.get("kind") in allowed
                ]

            with (
                patch.object(
                    vision,
                    "ingest_bridge_reports",
                    return_value={"chunks": chunks, "embeddings": None, "page_images": []},
                ),
                patch.object(
                    vision,
                    "_retrieve_report_chunks",
                    side_effect=retrieve,
                ),
                patch.object(vision, "bridge_sql_context", return_value=("", "")),
                patch.object(vision, "get_db_context", return_value=("", {})),
                patch.object(vision, "get_cerebras_answer", side_effect=generate),
                patch.object(
                    rag_engine,
                    "_detect_defects_with_bboxes",
                    side_effect=AssertionError("text mode reached detector"),
                ) as detector,
            ):
                result = await vision.process_bridge_ask(
                    "What evidence describes the deck crack?",
                    state_code=34,
                    bridge_number="TEST-1",
                    selected_sources=["inspection.pdf"],
                    analysis_mode="text",
                )

        self.assertIn("TEXT_REPORT_FACT", captured_prompt["value"])
        self.assertIn("INDEXED_IMAGE_DESCRIPTION", captured_prompt["value"])
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(
            result["image_description_evidence"]["notice"],
            "Previously indexed image-description evidence. "
            "No new visual inspection was performed.",
        )
        self.assertTrue(
            result["image_description_evidence"]["included_in_answer_context"]
        )
        self.assertFalse(
            result["image_description_evidence"]["raw_images_sent_to_model"]
        )
        self.assertFalse(result["visual_processing_ran"])
        detector.assert_not_called()

    async def test_normal_ask_only_loads_an_existing_visual_description_index(self):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "cached.png"
            image_path.write_bytes(b"cached-image")
            text_result = {
                "chunks": [{
                    "kind": "text",
                    "source": "inspection.pdf",
                    "text": "The deck rating is 6.",
                }],
                "page_images": [],
            }
            visual_result = {
                "chunks": [{
                    "kind": "image",
                    "source": "inspection.pdf",
                    "text": "Previously indexed description of deck spalling.",
                    "path": str(image_path),
                    "ext": "png",
                    "page": 5,
                }],
                "page_images": [],
                "rag_manifest": {"identity_sha256": "c" * 64},
            }

            def retrieve(_question, result, **kwargs):
                allowed = kwargs.get("allowed_kinds")
                return [
                    chunk for chunk in result["chunks"]
                    if allowed is None or chunk.get("kind") in allowed
                ]

            with (
                patch.object(
                    vision,
                    "ingest_bridge_reports",
                    side_effect=[text_result, visual_result],
                ) as ingest,
                patch.object(
                    vision,
                    "_retrieve_report_chunks",
                    side_effect=retrieve,
                ),
                patch.object(
                    vision,
                    "get_cerebras_answer",
                    return_value="The indexed description reports deck spalling.",
                ),
                patch.object(
                    vision.llm_providers,
                    "generate",
                    side_effect=AssertionError("normal Ask called the visual model"),
                ) as visual_provider,
            ):
                result = await vision.process_bridge_ask(
                    "What does the report say about deck spalling?",
                    state_code=34,
                    bridge_number="TEST-1",
                    selected_sources=["inspection.pdf"],
                    analysis_mode="text",
                )

        self.assertEqual(ingest.call_count, 2)
        visual_lookup = ingest.call_args_list[1]
        self.assertFalse(visual_lookup.kwargs["build_if_missing"])
        self.assertEqual(visual_lookup.kwargs["analysis_mode"], "visual")
        self.assertTrue(visual_lookup.kwargs["profiles"].caption["enabled"])
        self.assertEqual(len(result["images"]), 1)
        self.assertTrue(
            result["image_description_evidence"]["included_in_answer_context"]
        )
        visual_provider.assert_not_called()

    async def test_normal_ask_falls_back_to_verified_legacy_descriptions(self):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "legacy.png"
            image_path.write_bytes(b"legacy-image")
            text_result = {
                "chunks": [{
                    "kind": "text",
                    "source": "inspection.pdf",
                    "text": "The inspection report discusses the superstructure.",
                }],
                "page_images": [],
            }
            legacy_result = {
                "chunks": [{
                    "kind": "image",
                    "source": "inspection.pdf",
                    "text": "Previously indexed description of rust on the girder.",
                    "path": str(image_path),
                    "ext": "png",
                    "page": 7,
                }],
                "page_images": [],
                "rag_manifest": {"identity_sha256": "d" * 64},
            }

            def retrieve(_question, result, **kwargs):
                allowed = kwargs.get("allowed_kinds")
                return [
                    chunk for chunk in result["chunks"]
                    if allowed is None or chunk.get("kind") in allowed
                ]

            with (
                patch.object(
                    vision,
                    "ingest_bridge_reports",
                    side_effect=[text_result, vision.ReportRagCacheMiss("missing")],
                ),
                patch.object(
                    vision,
                    "_load_verified_legacy_image_description_index",
                    return_value=legacy_result,
                ) as legacy_loader,
                patch.object(vision, "_retrieve_report_chunks", side_effect=retrieve),
                patch.object(
                    vision,
                    "get_cerebras_answer",
                    return_value="The indexed image description reports girder rust.",
                ),
                patch.object(
                    vision.llm_providers,
                    "generate",
                    side_effect=AssertionError("normal Ask called the visual model"),
                ) as visual_provider,
            ):
                result = await vision.process_bridge_ask(
                    "Is there any rust?",
                    state_code=34,
                    bridge_number="TEST-1",
                    selected_sources=["inspection.pdf"],
                    analysis_mode="text",
                )

        legacy_loader.assert_called_once()
        self.assertEqual(len(result["images"]), 1)
        self.assertTrue(
            result["image_description_evidence"]["included_in_answer_context"]
        )
        visual_provider.assert_not_called()

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

        registry = AiRequestRegistry()
        context = registry.register(
            request_id=str(uuid4()),
            owner_id="owner@example.com",
            workspace_id="workspace-a",
            conversation_id=None,
            mode="visual",
            operation="bridge_visual_investigation",
            state_code=34,
            bridge_number="TEST-1",
            data_year=2025,
            bridge_identity=_published_identity(),
        )
        opened = MagicMock()
        opened.__enter__.return_value.read.side_effect = [b"image-bytes", b""]
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "open", return_value=opened),
            patch.object(
                vision,
                "_load_verified_legacy_image_description_index",
                return_value={"chunks": chunks, "embeddings": None},
            ),
            patch.object(
                vision,
                "ingest_bridge_reports",
                side_effect=AssertionError("existing descriptions should be reused"),
            ) as ingest,
            patch.object(
                vision,
                "_retrieve_report_chunks",
                side_effect=lambda _question, result, **_kwargs: [
                    chunk for chunk in result["chunks"]
                    if chunk.get("kind") == "image"
                ],
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
                request_context=context,
            )

        detect.assert_called_once_with(
            str(image_path),
            "find cracks",
            raise_on_provider_error=True,
        )
        self.assertEqual(result["checked_images"], 1)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["detections"][0]["detections"], detections)
        self.assertEqual(result["visual_evidence"]["analysis_mode"], "visual")
        self.assertTrue(result["visual_evidence"]["visual_processing_ran"])
        self.assertEqual(
            result["visual_evidence"]["candidate_description_source"],
            "verified_prior_image_descriptions",
        )
        ingest.assert_not_called()

    async def test_visual_mode_enforces_image_cap_and_provenance(self):
        chunks = [
            {
                "kind": "image",
                "source": "inspection.pdf",
                "text": f"Photo {number}",
                "caption": f"Photo {number}",
                "path": f"image-{number}.png",
                "ext": "png",
                "page": number,
                "photo_number": number,
            }
            for number in range(1, 4)
        ]
        registry = AiRequestRegistry()
        context = registry.register(
            request_id=str(uuid4()), owner_id="owner@example.com",
            workspace_id="workspace-a", conversation_id=None, mode="visual",
            operation="bridge_visual_investigation", state_code=34,
            bridge_number="TEST-1",
            data_year=2025, bridge_identity=_published_identity(),
        )
        opened = MagicMock()
        opened.__enter__.return_value.read.side_effect = [b"image", b""]
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "open", return_value=opened),
            patch.object(
                vision, "ingest_bridge_reports",
                return_value={"chunks": chunks, "embeddings": None},
            ),
            patch.object(
                vision, "_retrieve_report_chunks",
                side_effect=lambda _q, result, **_kwargs: [
                    chunk for chunk in result["chunks"]
                    if chunk.get("kind") == "image"
                ],
            ),
            patch.object(rag_engine, "_detect_defects_with_bboxes", return_value=[]) as detector,
        ):
            result = await vision.process_bridge_visual_investigation(
                query="find cracks", state_code=34, bridge_number="TEST-1",
                request_context=context,
                limits=VisualAnalysisLimits(max_images=1),
            )

        detector.assert_called_once()
        self.assertEqual(result["checked_images"], 1)
        self.assertEqual(len(result["inspected_images"]), 1)
        self.assertEqual(result["inspected_images"][0]["page"], 1)
        usage = result["visual_evidence"]["usage"]
        self.assertEqual(usage["estimated_images"], 1)
        self.assertEqual(usage["actual_images_inspected"], 1)
        self.assertIsNone(usage["actual_cost_usd"])

    async def test_visual_cancellation_is_checked_after_each_detector_call(self):
        context = AiRequestRegistry().register(
            request_id=str(uuid4()), owner_id="owner@example.com",
            workspace_id="workspace-a", conversation_id=None, mode="visual",
            operation="bridge_visual_investigation", state_code=34,
            bridge_number="TEST-1",
            data_year=2025, bridge_identity=_published_identity(),
        )
        opened = MagicMock()
        opened.__enter__.return_value.read.side_effect = [b"image", b""]

        def cancel_during_detection(_path, _query, **_kwargs):
            context.cancellation_token.set()
            return []

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "open", return_value=opened),
            patch.object(
                vision, "ingest_bridge_reports",
                return_value={
                    "chunks": [{
                        "kind": "image", "source": "inspection.pdf",
                        "text": "deck", "caption": "deck",
                        "path": "image.png", "ext": "png", "page": 1,
                    }],
                    "embeddings": None,
                },
            ),
            patch.object(
                vision, "_retrieve_report_chunks",
                side_effect=lambda _q, result, **_kwargs: [
                    chunk for chunk in result["chunks"]
                    if chunk.get("kind") == "image"
                ],
            ),
            patch.object(
                rag_engine, "_detect_defects_with_bboxes",
                side_effect=cancel_during_detection,
            ),
        ):
            with self.assertRaises(AiRequestCancelled):
                await vision.process_bridge_visual_investigation(
                    query="find cracks", state_code=34,
                    bridge_number="TEST-1", request_context=context,
                )


if __name__ == "__main__":
    unittest.main()
