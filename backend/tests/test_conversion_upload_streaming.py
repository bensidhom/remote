import asyncio
import hashlib

import pytest
from fastapi import HTTPException

from app.main import _stream_upload_to_private_path


class _AsyncUpload:
    def __init__(self, chunks, error_after=None):
        self._chunks = list(chunks)
        self._calls = 0
        self._error_after = error_after

    async def read(self, _size):
        if self._error_after is not None and self._calls >= self._error_after:
            raise OSError("interrupted upload")
        self._calls += 1
        return self._chunks.pop(0) if self._chunks else b""


def test_streamed_upload_writes_exact_bytes_and_sha256(test_workspace):
    destination = test_workspace / "private-upload.nbi"
    size, digest = asyncio.run(
        _stream_upload_to_private_path(
            _AsyncUpload([b"abc", b"def"]),
            destination,
            limit=32,
            chunk_size=8,
        )
    )

    assert size == 6
    assert digest == hashlib.sha256(b"abcdef").hexdigest()
    assert destination.read_bytes() == b"abcdef"


def test_streamed_upload_removes_partial_file_when_oversize(test_workspace):
    destination = test_workspace / "oversize.nbi"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _stream_upload_to_private_path(
                _AsyncUpload([b"1234", b"56"]),
                destination,
                limit=5,
                chunk_size=4,
            )
        )
    assert exc.value.status_code == 413
    assert not destination.exists()


def test_streamed_upload_removes_partial_file_when_reader_fails(test_workspace):
    destination = test_workspace / "interrupted.nbi"
    with pytest.raises(OSError, match="interrupted"):
        asyncio.run(
            _stream_upload_to_private_path(
                _AsyncUpload([b"1234"], error_after=1),
                destination,
                limit=16,
                chunk_size=4,
            )
        )
    assert not destination.exists()


def test_streamed_upload_rejects_empty_file_without_residue(test_workspace):
    destination = test_workspace / "empty.nbi"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _stream_upload_to_private_path(
                _AsyncUpload([]), destination, limit=16, chunk_size=4
            )
        )
    assert exc.value.status_code == 400
    assert not destination.exists()

