from __future__ import annotations

import asyncio
from types import MappingProxyType
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.routers.services import vision
from app.services.ai.context import AiRequestRegistry
from app.services.bridge_identity import BridgeSnapshotIdentity
from app.services.conversion.revision_store import SnapshotRevisionRecord


WORKSPACE = "workspace-a"


def _identity() -> BridgeSnapshotIdentity:
    return BridgeSnapshotIdentity(
        workspace_id=WORKSPACE,
        state_code=34,
        bridge_number="BR-001",
        data_year=2024,
        snapshot_revision_id="revision-2024",
        revision_number=2,
        canonical_sha256="a" * 64,
        profile_id="profile-1",
        rule_bundle_sha256="b" * 64,
        published_at="2026-08-13T12:00:00Z",
        data_generation=8,
        schema_generation=7,
    )


def _context():
    identity = _identity()
    return AiRequestRegistry().register(
        request_id=str(uuid4()),
        owner_id="owner@example.com",
        workspace_id=WORKSPACE,
        conversation_id=None,
        mode="text",
        operation="bridge_ask",
        state_code=34,
        bridge_number="BR-001",
        data_year=2024,
        bridge_identity=identity,
    )


def _revision() -> SnapshotRevisionRecord:
    document = {
        "BL01": "34",
        "BID01": "BR-001",
        "BID02": "Pinned 2024 bridge",
        "BC01": "6",
        "Features": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Elements": [],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "Works": [],
    }
    return SnapshotRevisionRecord(
        workspace_id=WORKSPACE,
        revision_id="revision-2024",
        state_code=34,
        bridge_number="BR-001",
        data_year=2024,
        revision_number=2,
        lifecycle_state="staged",
        canonical_json="{}",
        canonical_document=MappingProxyType(document),
        content_hash="a" * 64,
        parent_revision_id=None,
        profile_id="profile-1",
        created_by="owner@example.com",
        created_at="2026-08-13T10:00:00Z",
        source_records=(),
        lineage=({
            "target_pointer": "/BC01",
            "source_record_id": "record-1",
            "source_pointer": "/DECK_COND_058",
            "rule_id": "direct_bc01",
            "status": "populated",
        },),
        validation={"status": "warning", "issues": []},
    )


def test_published_context_contains_exact_document_all_field_index_and_lineage():
    context = _context()
    with (
        patch.object(vision, "USER_DB", "ignored.sqlite3"),
        patch(
            "app.services.conversion.revision_store.get_snapshot_revision",
            return_value=_revision(),
        ),
        patch(
            "app.services.conversion.canonical_json.canonical_sha256",
            return_value="a" * 64,
        ),
    ):
        encoded, metadata = vision.published_bridge_revision_context(
            "What is BC01 and where did it come from?", context
        )

    assert '"data_year":2024' in encoded
    assert '"BID02":"Pinned 2024 bridge"' in encoded
    assert encoded.count('"queryable_in_current_catalog"') == 154
    assert '"target_pointer":"/BC01"' in encoded
    assert metadata["transition_lineage_count"] == 1
    assert "BC01" in metadata["selected_detailed_tags"]


def test_bridge_ask_never_calls_legacy_prompt_scoped_sql_or_projection():
    context = _context()
    captured: dict[str, str] = {}

    def answer(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return "The pinned value is 6."

    with (
        patch.object(vision, "ingest_bridge_reports", return_value={
            "chunks": [], "embeddings": None, "page_images": [], "rag_manifest": None,
        }),
        patch.object(
            vision,
            "published_bridge_revision_context",
            return_value=(
                '{"published_identity":{"data_year":2024},"canonical_snbi_document":{"BC01":"6"}}',
                {"snbi_dictionary_version": "v1"},
            ),
        ) as exact_context,
        patch.object(
            vision,
            "bridge_sql_context",
            side_effect=AssertionError("prompt-scoped SQL must not run"),
        ),
        patch.object(
            vision,
            "get_db_context",
            side_effect=AssertionError("legacy projection must not run"),
        ),
        patch.object(vision, "get_cerebras_answer", side_effect=answer),
    ):
        result = asyncio.run(
            vision.process_bridge_ask(
                "What is the deck rating?",
                state_code=34,
                bridge_number="BR-001",
                selected_sources=["Database"],
                request_context=context,
            )
        )

    assert result["answer"] == "The pinned value is 6."
    assert '\\"data_year\\":2024' in captured["prompt"]
    exact_context.assert_called_once()


def test_bridge_database_evidence_requires_owned_published_identity():
    with pytest.raises(RuntimeError, match="published revision"):
        asyncio.run(
            vision.process_bridge_ask(
                "What is the deck rating?",
                state_code=34,
                bridge_number="BR-001",
                selected_sources=["Database"],
                request_context=None,
            )
        )
