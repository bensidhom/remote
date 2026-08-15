from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.routers.services.database import ensure_schema
from app.services.conversion.revision_store import ensure_workspace
from app.services.point_cloud.rollout import (
    PointCloudRolloutError,
    inventory_legacy_processed_pointers,
    quarantine_legacy_processed_pointers,
)


WORKSPACE = "customer-a-workspace"


def _pointer_path(root: Path) -> Path:
    workspace_token = "ws-" + hashlib.sha256(WORKSPACE.encode("utf-8")).hexdigest()[:24]
    path = (
        root
        / "attachments"
        / "point_clouds"
        / workspace_token
        / "34-opaque-bridge"
        / "processed"
        / "current.json"
    )
    path.parent.mkdir(parents=True)
    return path


@pytest.fixture
def rollout_fixture(test_workspace: Path):
    root = test_workspace / "data"
    root.mkdir()
    database = root / "snbi_bridges.db"
    ensure_schema(str(database))
    ensure_workspace(str(database), WORKSPACE, "Customer A")
    return root, database


def test_inventory_is_read_only_and_classifies_unreviewed_pointer(rollout_fixture):
    root, database = rollout_fixture
    pointer = _pointer_path(root)
    payload = {
        "format": "bridgeguard-point-cloud-pointer-v2",
        "workspace_id": WORKSPACE,
        "source_generation": "pcv2-7-" + "a" * 32 + "-" + "b" * 12,
        "manifest_sha256": "c" * 64,
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    pointer.write_bytes(raw)

    first = inventory_legacy_processed_pointers(root, database, WORKSPACE)
    second = inventory_legacy_processed_pointers(root, database, WORKSPACE)
    assert first.inventory_sha256 == second.inventory_sha256
    assert first.item_count == 1
    assert first.classification_counts == {"legacy_unreviewed": 1}
    assert first.items[0].reason == "no_durable_run_or_review_receipt"
    assert pointer.read_bytes() == raw


def test_quarantine_requires_exact_preview_and_is_recoverable(rollout_fixture):
    root, database = rollout_fixture
    pointer = _pointer_path(root)
    pointer.write_text(
        json.dumps(
            {
                "format": "bridgeguard-point-cloud-pointer-v2",
                "workspace_id": WORKSPACE,
                "source_generation": "pcv2-7-" + "a" * 32 + "-" + "b" * 12,
                "manifest_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    inventory = inventory_legacy_processed_pointers(root, database, WORKSPACE)
    with pytest.raises(PointCloudRolloutError) as caught:
        quarantine_legacy_processed_pointers(
            root,
            database,
            WORKSPACE,
            expected_inventory_sha256="0" * 64,
        )
    assert caught.value.code == "point_cloud_rollout_inventory_changed"
    assert pointer.exists()

    receipt = quarantine_legacy_processed_pointers(
        root,
        database,
        WORKSPACE,
        expected_inventory_sha256=inventory.inventory_sha256,
    )
    assert receipt.moved_count == 1 and receipt.retained_count == 0
    assert not pointer.exists()
    destination = root / Path(*receipt.moves[0]["to"].split("/"))
    assert destination.is_file()
    receipt_path = (
        root
        / "reference"
        / "point_cloud_rollout"
        / f"legacy-quarantine-{receipt.receipt_sha256}.json"
    )
    assert receipt_path.is_file()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["moved_count"] == 1


def test_invalid_pointer_is_reported_not_silently_loaded(rollout_fixture):
    root, database = rollout_fixture
    pointer = _pointer_path(root)
    pointer.write_text('{"format":"wrong","format":"duplicate"}', encoding="utf-8")
    inventory = inventory_legacy_processed_pointers(root, database, WORKSPACE)
    assert inventory.classification_counts == {"invalid_pointer": 1}
    assert inventory.items[0].manifest_sha256 is None


def test_workspace_namespace_does_not_inventory_another_customer(rollout_fixture):
    root, database = rollout_fixture
    _pointer_path(root).write_text("{}", encoding="utf-8")
    ensure_workspace(str(database), "customer-b-workspace", "Customer B")
    inventory = inventory_legacy_processed_pointers(
        root, database, "customer-b-workspace"
    )
    assert inventory.item_count == 0
