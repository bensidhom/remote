from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from app.routers.services import llm_providers, rag_engine, vision
from app.routers.services.report_rag_cache import (
    ReportRagCacheStore,
    ReportRagProfiles,
)
from app.services.ai.context import (
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


def _profiles(*, max_images: int = 4) -> ReportRagProfiles:
    return ReportRagProfiles(
        parser={
            "profile_id": "parser-test-v1",
            "chunk_size": 10,
            "chunk_overlap": 2,
        },
        caption={
            "profile_id": "caption-test-v1",
            "enabled": True,
            "model": "caption-test",
            "prompt_sha256": "c" * 64,
            "max_images": max_images,
        },
        embedding=llm_providers.embedding_profile(),
        retrieval={
            "profile_id": "retrieval-test-v1",
            "tokenizer": "unicode-alphanumeric-lower-stopwords-v1",
            "bm25_k1": 1.2,
            "bm25_b": 0.75,
            "fusion": "reciprocal-rank-fusion-v1",
            "rrf_k": 60,
            "lexical_weight": 1.0,
            "dense_weight": 1.0,
            "minimum_bm25_score": 0.15,
            "minimum_dense_cosine": 0.25,
            "minimum_fused_score": 1.0 / 61.0,
            "maximum_candidates_per_channel": 40,
        },
    )


def _context():
    return AiRequestRegistry().register(
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


def _vectors(chunks: list[dict]) -> np.ndarray:
    dimension = int(llm_providers.embedding_profile()["dimension"])
    return np.zeros((len(chunks), dimension), dtype=np.float32)


def test_deselected_report_is_filtered_before_parse_or_caption_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(vision, "DATA_DIR", tmp_path)
    selected_report = tmp_path / "selected.txt"
    secret_report = tmp_path / "deselected.txt"
    selected_report.write_text("selected report text", encoding="utf-8")
    secret_report.write_text("DO_NOT_SEND_THIS_REPORT", encoding="utf-8")
    selected_image = tmp_path / "selected.png"
    secret_image = tmp_path / "deselected.png"
    selected_image.write_bytes(b"selected-image-bytes")
    secret_image.write_bytes(b"secret-image-bytes")
    files = [
        {
            "id": 1,
            "filename": "selected.txt",
            "filepath": str(selected_report),
            "size_bytes": selected_report.stat().st_size,
        },
        {
            "id": 2,
            "filename": "deselected.txt",
            "filepath": str(secret_report),
            "size_bytes": secret_report.stat().st_size,
        },
    ]
    parsed_paths: list[str] = []

    def extract(filepath, _sha, _cancellation):
        parsed_paths.append(str(filepath))
        image = selected_image if Path(filepath) == selected_report else secret_image
        return (
            [{"page": 1, "text": Path(filepath).read_text(encoding="utf-8")}],
            [{"path": str(image), "page": 1, "image_id": image.stem}],
            [],
        )

    store = ReportRagCacheStore(tmp_path / "cache")
    budget = vision._VisualRequestBudget(
        VisualAnalysisLimits(max_images=4), input_cost_per_million=0.15
    )
    with (
        patch.object(vision, "_existing_report_files", return_value=files),
        patch.object(vision, "_report_rag_store", return_value=store),
        patch.object(vision, "_extract_file_assets", side_effect=extract),
        patch.object(
            rag_engine,
            "_resize_image_b64",
            side_effect=lambda path: f"ENC:{Path(path).name}",
        ),
        patch.object(llm_providers, "generate", return_value="selected deck photo") as provider,
        patch.object(rag_engine, "_compute_embeddings", side_effect=_vectors),
    ):
        result = vision.ingest_bridge_reports(
            34,
            "TEST-1",
            workspace_id="workspace-a",
            data_year=2025,
            inventory_identity=_published_identity(),
            profiles=_profiles(),
            analysis_mode="visual",
            selected_sources=["selected.txt"],
            visual_budget=budget,
        )

    assert parsed_paths == [str(selected_report)]
    assert provider.call_count == 1
    assert provider.call_args.kwargs["images_b64"] == ["ENC:selected.png"]
    assert [source["report_id"] for source in result["rag_manifest"]["sources"]] == ["1"]
    assert {chunk["source"] for chunk in result["chunks"]} == {"selected.txt"}
    assert budget.usage()["caption_image_call_attempts"] == 1


def test_caption_cap_round_robins_selected_reports_without_source_starvation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(vision, "DATA_DIR", tmp_path)
    report_a = tmp_path / "a.txt"
    report_b = tmp_path / "b.txt"
    report_a.write_text("a", encoding="utf-8")
    report_b.write_text("b", encoding="utf-8")
    files = [
        {"id": 1, "filename": "a.txt", "filepath": str(report_a)},
        {"id": 2, "filename": "b.txt", "filepath": str(report_b)},
    ]
    identity = vision._report_rag_identity(
        files, _published_identity(), profiles=_profiles(max_images=2)
    )
    image_paths: dict[str, Path] = {}
    images: list[dict] = []
    for report_id, source, page in (
        ("1", "a.txt", 1),
        ("1", "a.txt", 2),
        ("1", "a.txt", 3),
        ("2", "b.txt", 1),
    ):
        key = f"{report_id}-{page}"
        path = tmp_path / f"{key}.png"
        path.write_bytes(key.encode("ascii"))
        image_paths[key] = path
        images.append({
            "path": str(path),
            "report_id": report_id,
            "page": page,
            "image_id": key,
            "chunk_id": (key.replace("-", "") * 64)[:64],
            "source": source,
            "report_sha256": identity.sources[int(report_id) - 1].sha256,
            "citation": {"image_id": key},
        })

    store = ReportRagCacheStore(tmp_path / "caption-cache")
    with (
        patch.object(
            rag_engine,
            "_resize_image_b64",
            side_effect=lambda path: f"ENC:{Path(path).stem}",
        ),
        patch.object(llm_providers, "generate", return_value="bridge photo") as provider,
    ):
        _, payload = vision._build_caption_report_stage(
            identity=identity,
            parsed_payload={"images": images},
            store=store,
            report_progress=False,
            cancellation=None,
        )

    sent = [call.kwargs["images_b64"][0] for call in provider.call_args_list]
    assert sent == ["ENC:1-1", "ENC:2-1"]
    assert [chunk["source"] for chunk in payload["image_chunks"]] == ["a.txt", "b.txt"]
    assert payload["skipped_image_count"] == 2


def test_caption_and_detector_calls_share_one_total_image_cap(tmp_path: Path):
    paths = []
    for index in range(1, 6):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(f"image-{index}".encode("ascii"))
        paths.append(path)

    chunks = [
        {
            "kind": "image",
            "source": "inspection.pdf",
            "text": "deck crack",
            "caption": f"Photo {index}",
            "path": str(path),
            "page": index,
        }
        for index, path in enumerate(paths[2:], start=3)
    ]

    def ingest(*_args, visual_budget, **_kwargs):
        for path in paths[:2]:
            receipt, denial = visual_budget.reserve(
                stage="caption", path=path, source="inspection.pdf"
            )
            assert denial is None and receipt is not None
            visual_budget.finish(receipt, outcome="helper_returned")
        return {"chunks": chunks, "embeddings": None, "rag_manifest": {}}

    with (
        patch.object(vision, "ingest_bridge_reports", side_effect=ingest),
        patch.object(
            vision,
            "_retrieve_report_chunks",
            side_effect=lambda _q, result, **_kwargs: result["chunks"],
        ),
        patch.object(rag_engine, "_detect_defects_with_bboxes", return_value=[]) as detector,
    ):
        result = asyncio.run(vision.process_bridge_visual_investigation(
            query="find deck cracks",
            state_code=34,
            bridge_number="TEST-1",
            selected_sources=["inspection.pdf"],
            request_context=_context(),
            limits=VisualAnalysisLimits(max_images=4),
        ))

    usage = result["visual_evidence"]["usage"]
    assert detector.call_count == 2
    assert usage["model_image_call_attempts"] == 4
    assert usage["caption_image_call_attempts"] == 2
    assert usage["detector_image_call_attempts"] == 2
    assert usage["estimated_images"] == 4


def test_caption_usage_remains_truthful_when_retrieval_uses_visual_fallback(tmp_path: Path):
    caption_path = tmp_path / "captioned.png"
    caption_path.write_bytes(b"captioned-image")

    def ingest(*_args, visual_budget, **_kwargs):
        receipt, denial = visual_budget.reserve(
            stage="caption",
            path=caption_path,
            source="selected.pdf",
            page=7,
        )
        assert denial is None and receipt is not None
        visual_budget.finish(receipt, outcome="helper_returned")
        return {
            "chunks": [{
                "kind": "image",
                "source": "selected.pdf",
                "text": "unrelated image",
                "path": str(caption_path),
                "page": 7,
            }],
            "embeddings": None,
            "rag_manifest": {"identity_sha256": "d" * 64},
        }

    with (
        patch.object(vision, "ingest_bridge_reports", side_effect=ingest),
        patch.object(vision, "_retrieve_report_chunks", return_value=[]),
        patch.object(
            rag_engine,
            "_detect_defects_with_bboxes",
            return_value=[],
        ) as detector,
    ):
        result = asyncio.run(vision.process_bridge_visual_investigation(
            query="find corrosion",
            state_code=34,
            bridge_number="TEST-1",
            selected_sources=["selected.pdf"],
            request_context=_context(),
        ))

    usage = result["visual_evidence"]["usage"]
    assert result["status"] == "success"
    assert result["visual_evidence"]["candidate_retrieval"]["fallback_used"] is True
    assert result["visual_evidence"]["visual_processing_ran"] is True
    assert usage["model_image_call_attempts"] == 2
    assert usage["caption_image_call_attempts"] == 1
    assert usage["detector_image_call_attempts"] == 1
    assert usage["provider_call_receipts"][0]["source"] == "selected.pdf"
    detector.assert_called_once()
