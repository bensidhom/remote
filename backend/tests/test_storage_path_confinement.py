from __future__ import annotations

from pathlib import Path

import pytest

from app.services.operations.storage_paths import (
    StoragePathError,
    portable_confined_path,
    resolve_confined_path,
)


def test_confined_paths_accept_portable_and_legacy_absolute_paths(tmp_path: Path):
    root = tmp_path / "customer"
    target = root / "attachments" / "report.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"pdf")

    assert resolve_confined_path(root, "attachments/report.pdf", require_exists=True) == target.resolve()
    assert resolve_confined_path(root, target, require_exists=True) == target.resolve()
    assert portable_confined_path(root, target, require_exists=True) == "attachments/report.pdf"


@pytest.mark.parametrize(
    "stored",
    ("../secret", "attachments/../../secret"),
)
def test_confined_paths_reject_traversal(tmp_path: Path, stored: str):
    root = tmp_path / "customer"
    root.mkdir()
    with pytest.raises(StoragePathError, match="traversal"):
        resolve_confined_path(root, stored)


def test_confined_paths_reject_absolute_escape(tmp_path: Path):
    root = tmp_path / "customer"
    outside = tmp_path / "outside" / "secret.bin"
    root.mkdir()
    outside.parent.mkdir()
    outside.write_bytes(b"secret")
    with pytest.raises(StoragePathError, match="outside"):
        resolve_confined_path(root, outside, require_exists=True)


def test_confined_paths_reject_link_swap(tmp_path: Path):
    root = tmp_path / "customer"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    linked = root / "attachments"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(StoragePathError, match="link"):
        resolve_confined_path(root, "attachments/secret.bin", require_exists=True)


def test_application_resolvers_share_the_same_fail_closed_boundary(tmp_path: Path):
    from app import main
    from app.routers import point_cloud_api
    from app.routers.services import vision

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do-not-read-or-delete")

    with pytest.raises(ValueError, match="outside"):
        main._resolve_attachment_path(str(outside))
    with pytest.raises(ValueError, match="outside"):
        point_cloud_api._resolve_data_path(str(outside))
    with pytest.raises(ValueError, match="outside"):
        vision._resolve_data_path(str(outside))
    assert outside.read_bytes() == b"do-not-read-or-delete"
