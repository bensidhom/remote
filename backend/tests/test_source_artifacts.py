from __future__ import annotations

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.services.conversion.source_artifacts import (
    ArtifactErrorCode,
    SourceArtifactError,
    SourceArtifactStore,
)


def _temporary_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and (path.name.endswith(".part") or path.name.endswith(".tmp"))
    ]


def _content_objects(root: Path) -> list[Path]:
    return [path for path in (root / "objects").rglob("*") if path.is_file()]


def _manifests(root: Path) -> list[Path]:
    return sorted((root / "manifests").glob("*.json"))


class RecordingStream(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.requested_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requested_sizes.append(size)
        return super().read(size)


def test_streams_content_with_hash_fidelity_and_path_free_metadata(test_workspace):
    root = test_workspace / "store"
    payload = (b"NBI-to-SNBI source bytes\x00\xff" * 19) + b"end"
    stream = RecordingStream(payload)
    store = SourceArtifactStore(root, max_bytes=4096, chunk_bytes=17)

    receipt = store.ingest(
        stream,
        original_filename="Bridge resume #7?.PDF",
    )

    UUID(receipt.job_id)
    UUID(receipt.artifact_id)
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.size_bytes == len(payload)
    assert receipt.original_filename == "Bridge resume _7_.PDF"
    assert receipt.status == "accepted"
    assert store.read_bytes(receipt.artifact_id) == payload
    assert stream.requested_sizes
    assert set(stream.requested_sizes) == {17}

    public_json = json.dumps(receipt.to_public_dict(), sort_keys=True)
    assert str(root) not in public_json
    assert "server_path" not in public_json
    assert "object_path" not in public_json

    manifests = _manifests(root)
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["sha256"] == receipt.sha256
    assert manifest["original_filename"] == receipt.original_filename
    assert str(root) not in json.dumps(manifest)
    objects = _content_objects(root)
    assert len(objects) == 1
    assert objects[0].name == receipt.sha256
    assert objects[0].read_bytes() == payload
    assert all(receipt.original_filename not in path.name for path in root.rglob("*"))
    assert _temporary_files(root) == []


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.txt",
        r"..\secret.txt",
        "/etc/passwd",
        r"C:\private\bridge.txt",
        "folder/report.pdf",
        ".",
        "..",
        "bad\x00name.pdf",
        "",
    ],
)
def test_rejects_traversal_and_invalid_original_names(
    test_workspace, filename
):
    root = test_workspace / "store"
    store = SourceArtifactStore(root)

    with pytest.raises(SourceArtifactError) as raised:
        store.ingest(b"source", original_filename=filename)

    assert raised.value.code == ArtifactErrorCode.INVALID_FILENAME.value
    assert str(root) not in json.dumps(raised.value.to_public_dict())
    assert _manifests(root) == []
    assert _content_objects(root) == []
    assert _temporary_files(root) == []


def test_interrupted_stream_returns_typed_io_failure_and_cleans_stage(
    test_workspace,
):
    root = test_workspace / "store"
    store = SourceArtifactStore(root, chunk_bytes=4)

    def interrupted():
        yield b"partial"
        raise RuntimeError(r"failure at C:\private\input.txt")

    with pytest.raises(SourceArtifactError) as raised:
        store.ingest(interrupted(), original_filename="input.txt")

    assert raised.value.code == ArtifactErrorCode.IO_FAILURE.value
    assert "private" not in str(raised.value)
    assert _manifests(root) == []
    assert _content_objects(root) == []
    assert _temporary_files(root) == []
    assert list((root / "staging").iterdir()) == []


def test_oversize_stream_is_rejected_before_extra_bytes_are_written(
    test_workspace,
):
    root = test_workspace / "store"
    store = SourceArtifactStore(root, max_bytes=8, chunk_bytes=4)

    with pytest.raises(SourceArtifactError) as raised:
        store.ingest(
            [b"1234", b"5678", b"9"],
            original_filename="inventory.txt",
        )

    assert raised.value.code == ArtifactErrorCode.UPLOAD_TOO_LARGE.value
    assert raised.value.details == {"max_bytes": 8}
    assert _manifests(root) == []
    assert _content_objects(root) == []
    assert _temporary_files(root) == []


def test_zero_byte_upload_is_rejected_and_leaves_no_temporary_file(
    test_workspace,
):
    root = test_workspace / "store"
    store = SourceArtifactStore(root)

    with pytest.raises(SourceArtifactError) as raised:
        store.ingest(io.BytesIO(), original_filename="empty.txt")

    assert raised.value.code == ArtifactErrorCode.EMPTY_UPLOAD.value
    assert _manifests(root) == []
    assert _content_objects(root) == []
    assert _temporary_files(root) == []


def test_same_filename_and_content_are_concurrency_safe_and_idempotent(
    test_workspace,
):
    root = test_workspace / "store"
    store = SourceArtifactStore(root, chunk_bytes=13)
    payload = b"same immutable bridge inventory" * 37

    def accept_one(_index: int):
        return store.ingest(
            io.BytesIO(payload),
            original_filename="same-name.txt",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(accept_one, range(16)))

    assert len({receipt.artifact_id for receipt in receipts}) == 16
    assert len({receipt.job_id for receipt in receipts}) == 16
    assert {receipt.sha256 for receipt in receipts} == {
        hashlib.sha256(payload).hexdigest()
    }
    assert len(_content_objects(root)) == 1
    assert len(_manifests(root)) == 16
    assert all(store.read_bytes(item.artifact_id) == payload for item in receipts)
    assert _temporary_files(root) == []


def test_distinct_filenames_and_content_are_concurrency_safe(test_workspace):
    root = test_workspace / "store"
    store = SourceArtifactStore(root, chunk_bytes=11)
    sources = [
        (f"bridge-{index}.txt", f"bridge payload {index}".encode() * 23)
        for index in range(12)
    ]

    def accept_one(item: tuple[str, bytes]):
        filename, payload = item
        return payload, store.ingest(payload, original_filename=filename)

    with ThreadPoolExecutor(max_workers=6) as executor:
        accepted = list(executor.map(accept_one, sources))

    assert len({receipt.artifact_id for _, receipt in accepted}) == len(sources)
    assert len({receipt.sha256 for _, receipt in accepted}) == len(sources)
    assert len(_content_objects(root)) == len(sources)
    assert len(_manifests(root)) == len(sources)
    for payload, receipt in accepted:
        assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
        assert store.read_bytes(receipt.artifact_id) == payload
    assert _temporary_files(root) == []


def test_reingest_does_not_overwrite_the_existing_content_object(test_workspace):
    root = test_workspace / "store"
    store = SourceArtifactStore(root)
    payload = b"immutable object"

    first = store.ingest(payload, original_filename="first.txt")
    object_path = _content_objects(root)[0]
    before = object_path.stat()
    second = store.ingest(payload, original_filename="second.txt")
    after = object_path.stat()

    assert first.artifact_id != second.artifact_id
    assert first.job_id != second.job_id
    assert first.sha256 == second.sha256
    assert len(_content_objects(root)) == 1
    assert len(_manifests(root)) == 2
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert object_path.read_bytes() == payload


def test_failed_staging_retention_requires_explicit_confined_uuids(
    test_workspace,
):
    root = test_workspace / "store"
    store = SourceArtifactStore(root)
    accepted = store.ingest(b"accepted", original_filename="accepted.txt")

    assert (
        store.discard_failed_staging(
            job_id=accepted.job_id,
            artifact_id=accepted.artifact_id,
        )
        is False
    )
    assert store.read_bytes(accepted.artifact_id) == b"accepted"

    job_id = str(uuid4())
    artifact_id = str(uuid4())
    staged = root / "staging" / job_id / f"{artifact_id}.part"
    staged.parent.mkdir()
    staged.write_bytes(b"failed partial source")
    assert store.discard_failed_staging(
        job_id=job_id,
        artifact_id=artifact_id,
    )
    assert not staged.exists()
    assert store.read_bytes(accepted.artifact_id) == b"accepted"

    with pytest.raises(SourceArtifactError) as raised:
        store.discard_failed_staging(
            job_id="../outside",
            artifact_id=artifact_id,
        )
    assert raised.value.code == ArtifactErrorCode.INVALID_ARTIFACT_ID.value


def test_symlinked_storage_root_is_rejected_when_supported(test_workspace):
    real_root = test_workspace / "real-store"
    real_root.mkdir()
    linked_root = test_workspace / "linked-store"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable for this test account")

    with pytest.raises(SourceArtifactError) as raised:
        SourceArtifactStore(linked_root)

    assert raised.value.code == ArtifactErrorCode.IO_FAILURE.value
