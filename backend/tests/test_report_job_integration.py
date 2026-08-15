from __future__ import annotations

import ast
from pathlib import Path
import sqlite3
from unittest.mock import patch

from app import main as main_module


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "backend" / "app" / "main.py"


def _source() -> str:
    return MAIN.read_text(encoding="utf-8")


def test_report_indexing_uses_durable_queue_not_per_request_daemon_threads():
    source = _source()
    tree = ast.parse(source)
    function_sources = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    upload = function_sources["upload_bridge_report"]
    listing = function_sources["list_bridge_reports"]
    status = function_sources["bridge_reports_status"]
    assert "_queue_report_index(" in upload
    assert "threading.Thread" not in upload
    assert "_queue_report_index(" not in listing
    assert "_queue_report_index(" not in status
    assert "threading.Thread" not in listing + status


def test_report_upload_is_streamed_opaque_hashed_and_workspace_scoped():
    source = _source()
    assert "_stream_upload_to_private_path(" in source
    assert 'filepath = rpt_dir / f"{uuid4().hex}{ext}"' in source
    assert "size_bytes, source_sha256" in source
    assert "workspace_id    TEXT" in source
    assert "UPDATE bridge_attachments SET workspace_id=? WHERE workspace_id IS NULL" in source
    assert "workspace_id=? AND id=?" in source
    assert "'.doc'" not in source[source.index("REPORT_EXTS ="):source.index("MAX_RPT_BYTES")]
    assert "'.xls'" not in source[source.index("REPORT_EXTS ="):source.index("MAX_RPT_BYTES")]


def test_ai_registry_is_wired_to_shared_process_quota():
    context = (ROOT / "backend" / "app" / "services" / "ai" / "context.py").read_text(
        encoding="utf-8"
    )
    assert "quota_registry=DEFAULT_OPERATION_QUOTAS" in context
    assert "self._quota_registry.release(entry.quota_lease)" in context


def test_startup_enqueue_is_metadata_only_uncapped_by_one_system_owner(tmp_path):
    database = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE bridge_attachments ("
            "id INTEGER PRIMARY KEY,workspace_id TEXT,state_code TEXT,bridge_number TEXT,"
            "attachment_type TEXT,sha256 TEXT,uploaded_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO bridge_attachments VALUES (?,?,?,?,?,?,?)",
            [
                (
                    index,
                    "workspace-a",
                    "34",
                    f"BR-{index:03d}",
                    "inspection_report",
                    f"{index:064x}"[-64:],
                    "2026-08-13T12:00:00Z",
                )
                for index in range(1, 76)
            ],
        )
        connection.commit()
    finally:
        connection.close()

    queued: list[dict[str, object]] = []

    def capture(state_code, bridge_number, **kwargs):
        queued.append({
            "state_code": state_code,
            "bridge_number": bridge_number,
            **kwargs,
        })
        return object()

    with (
        patch.object(main_module, "USER_DB", database),
        patch.object(main_module, "SHARED_WORKSPACE_ID", "workspace-a"),
        patch.object(main_module, "_queue_report_index", side_effect=capture),
        patch.dict("os.environ", {"BRIDGEGUARD_REINDEX_STALE_REPORTS": "1"}),
    ):
        main_module._enqueue_stale_report_indexes()

    assert len(queued) == 75
    assert len({item["owner_id"] for item in queued}) == 75
    assert all(str(item["owner_id"]).startswith("system+") for item in queued)
    assert all("unresolved" not in str(item["idempotency_key"]) for item in queued)
    # No file path or file read is involved; the digest comes only from stored
    # attachment receipts and bounded metadata.
    source = _source()
    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_enqueue_stale_report_indexes"
    )
    function_source = ast.get_source_segment(source, function) or ""
    assert "bridge_reports_index_status" not in function_source
    assert "read_bytes" not in function_source


def test_worker_periodically_refills_the_bounded_report_queue():
    source = _source()
    tree = ast.parse(source)
    worker = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_start_report_job_worker"
    )
    worker_source = ast.get_source_segment(source, worker) or ""
    assert "_enqueue_stale_report_indexes()" in worker_source
    assert "time.monotonic()" in worker_source
    assert "next_reconcile = now + 30.0" in worker_source


def test_normal_bridge_ask_never_builds_a_missing_report_index():
    source = (
        ROOT / "backend" / "app" / "routers" / "services" / "vision.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    ask = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_bridge_ask"
    )
    ask_source = ast.get_source_segment(source, ask) or ""
    assert "build_if_missing=False" in ask_source
