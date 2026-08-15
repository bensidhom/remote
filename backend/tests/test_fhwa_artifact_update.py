import io
import json
from pathlib import Path

import pytest

from scripts.update_fhwa_artifacts import (
    CandidateDownloadError,
    _copy_bounded,
    stage_candidate,
)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, content_length: str | None = None):
        super().__init__(body)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _xlsx_bytes() -> bytes:
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return output.getvalue()


def _registry(
    path: Path,
    *,
    media_type: str = "application/octet-stream",
) -> Path:
    payload = {
        "format_version": 1,
        "registry_id": "test_registry",
        "observed_on": "2026-08-13",
        "artifacts": [
            {
                "id": "test_artifact",
                "title": "Test artifact",
                "role": "test",
                "authority": "FHWA",
                "legal_status": "operational",
                "state": "remote_unverified",
                "source_url": "https://example.invalid/artifact.bin",
                "published_on": None,
                "published_label": None,
                "updated_on": None,
                "media_type": media_type,
                "local_path": None,
                "sha256": None,
                "notes": [],
            }
        ],
        "bundles": [
            {
                "id": "test_bundle",
                "title": "Test bundle",
                "profile_kind": "research_import",
                "legal_status": "internal_non_submission",
                "compliance_use": "research_only_not_for_submission",
                "implementation_status": "registry_only",
                "enabled": True,
                "effective_submission_years": [],
                "artifact_ids": ["test_artifact"],
                "inherits": [],
                "disabled_reason": None,
                "notes": [],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_copy_bounded_rejects_oversize_without_silent_truncation():
    destination = io.BytesIO()
    with pytest.raises(CandidateDownloadError, match="exceeds"):
        _copy_bounded(io.BytesIO(b"12345"), destination, max_bytes=4, chunk_size=2)


def test_candidate_is_hash_named_and_registry_is_not_modified(test_workspace):
    registry_path = _registry(test_workspace / "registry.json")
    original = registry_path.read_bytes()

    result = stage_candidate(
        "test_artifact",
        registry_path=registry_path,
        candidate_root=test_workspace / "candidates",
        opener=lambda *_args, **_kwargs: _Response(b"official bytes", "14"),
    )

    candidate = test_workspace / "candidates" / str(result["candidate_path"])
    assert candidate.read_bytes() == b"official bytes"
    assert candidate.name == f"{result['sha256']}.candidate"
    assert result["registry_modified"] is False
    assert registry_path.read_bytes() == original


def test_announced_oversize_leaves_no_partial_candidate(test_workspace):
    registry_path = _registry(test_workspace / "registry.json")
    candidate_root = test_workspace / "candidates"
    with pytest.raises(CandidateDownloadError, match="above the limit"):
        stage_candidate(
            "test_artifact",
            registry_path=registry_path,
            candidate_root=candidate_root,
            max_bytes=5,
            opener=lambda *_args, **_kwargs: _Response(b"123456", "6"),
        )
    assert not list(candidate_root.rglob("*.part"))


def test_registered_xlsx_rejects_html_error_page_and_leaves_no_candidate(test_workspace):
    registry_path = _registry(
        test_workspace / "registry.json",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    candidate_root = test_workspace / "candidates"
    with pytest.raises(CandidateDownloadError, match="registered XLSX"):
        stage_candidate(
            "test_artifact",
            registry_path=registry_path,
            candidate_root=candidate_root,
            opener=lambda *_args, **_kwargs: _Response(b"<!DOCTYPE html><p>error</p>"),
        )
    assert not list(candidate_root.rglob("*.candidate"))


def test_registered_xlsx_accepts_complete_office_package(test_workspace):
    registry_path = _registry(
        test_workspace / "registry.json",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    body = _xlsx_bytes()
    result = stage_candidate(
        "test_artifact",
        registry_path=registry_path,
        candidate_root=test_workspace / "candidates",
        opener=lambda *_args, **_kwargs: _Response(body, str(len(body))),
    )
    assert result["size_bytes"] == len(body)
