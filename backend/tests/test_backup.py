from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from app import backup
from app.routers.services.forecasting import init_forecast_db
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.operations.deployment_database import bootstrap_dedicated_database
from app.services.operations.jobs import initialize_job_store
from app.services.operations.provisioning import provision_dedicated_layout
from app.services.conversion.canonical_json import canonical_json_text, canonical_sha256
from app.services.point_cloud.run_store import (
    CreatePointCloudRunRequest,
    PointCloudSourceEvidence,
    ReviewPointCloudRunRequest,
    TransitionPointCloudRunRequest,
    create_point_cloud_run,
    review_point_cloud_run,
    transition_point_cloud_run,
)


def _environment(root: Path) -> dict[str, str]:
    return {
        "BRIDGEGUARD_ENV": "production",
        "BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer",
        "BRIDGEGUARD_WORKSPACE_ID": "customer-a-production",
        "BRIDGEGUARD_CUSTOMER_SLUG": "customer-a",
        "BRIDGEGUARD_WORKER_COUNT": "1",
        "BRIDGEGUARD_AUTH_MODE": "cloudflare",
        "CLOUDFLARE_ACCESS_TEAM_DOMAIN": "example.cloudflareaccess.com",
        "CLOUDFLARE_ACCESS_AUD": "audience",
        "BRIDGEGUARD_DATA_DIR": str(root),
        "RAG_WORKING_DIR": str(root / "rag_storage"),
        "SNBI_TEMP_DIR": str(root / "temp"),
        "BRIDGEGUARD_OWNER_EMAIL": "owner@example.com",
        "BRIDGEGUARD_PDF_ENGINE_DISPOSITION": "commercial_license",
        "BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID": "PDF-LICENSE-2026-001",
        "BRIDGEGUARD_AI_PROVIDER_ID": "cerebras",
        "BRIDGEGUARD_AI_MODEL_ID": "gemma-4-31b",
        "CEREBRAS_MODEL": "gemma-4-31b",
        "BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID": "AI-ORDER-2026-001",
        "BRIDGEGUARD_AI_DPA_RECEIPT_ID": "AI-DPA-2026-001",
        "BRIDGEGUARD_MAP_SERVICE_MODE": "arcgis_sdk_keyless_osm",
        "BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID": "MAP-REVIEW-2026-001",
        "BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID": "EDGE-ORDER-2026-001",
        "BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID": "OSS-NOTICES-2026-001",
        "BRIDGEGUARD_SBOM_RECEIPT_ID": "SBOM-RELEASE-2026-001",
    }


def _prepare_customer_data(root: Path) -> tuple[dict[str, str], Path]:
    provision_dedicated_layout(
        root,
        customer_slug="customer-a",
        workspace_id="customer-a-production",
    )
    environment = _environment(root)
    bootstrap_dedicated_database(root, environ=environment)
    init_forecast_db(str(root / "snbi_forecasts.db"))
    initialize_job_store(root / "operation_jobs.db")

    attachment = root / "attachments" / "reports" / "34_1" / "report.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"%PDF-1.7\ncurrent customer report\n")
    # Derived assets are backed up but are not independent attachment rows.
    # The durable row-to-source-file reconciliation must allow these products.
    derived = (
        root
        / "attachments"
        / "point_clouds"
        / "34_1"
        / "processed"
        / "analysis.json"
    )
    derived.parent.mkdir(parents=True)
    derived.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(attachment.read_bytes()).hexdigest()
    with closing(sqlite3.connect(root / "snbi_bridges.db")) as connection:
        connection.execute(
            "INSERT INTO bridge_attachments "
            "(state_code,bridge_number,attachment_type,filename,filepath,"
            "size_bytes,sha256,workspace_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                "34",
                "1",
                "inspection_report",
                "report.pdf",
                "attachments/reports/34_1/report.pdf",
                attachment.stat().st_size,
                digest,
                "customer-a-production",
            ),
        )
        connection.commit()
    return environment, attachment


def _create_current_backup(root: Path, output: Path) -> Path:
    environment, _attachment = _prepare_customer_data(root)
    with (
        patch.object(backup, "DATA_DIR", root),
        patch.dict(backup.os.environ, environment, clear=False),
    ):
        return backup.create_backup(output)


def _add_accepted_point_cloud_run(root: Path) -> tuple[str, Path]:
    database = root / "snbi_bridges.db"
    workspace = "customer-a-production"
    state = 34
    bridge = "1"
    year = 2026
    document = {
        "BL01": state,
        "BID01": bridge,
        "BID02": "Backup fixture bridge",
        "Features": [{"BF01": "H02", "BF02": "2"}],
    }
    canonical_hash = canonical_sha256(document)
    revision_id = str(uuid.uuid4())
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO snapshot_revisions "
            "(id,workspace_id,conversion_batch_id,source_record_id,state_code,bridge_number,data_year,revision_number,"
            "lifecycle_state,canonical_json,canonical_sha256,parent_revision_id,profile_id,rule_bundle_sha256,created_by,created_at) "
            "VALUES (?,?,NULL,NULL,?,?,?,1,'staged',?,?,NULL,'research_historical_import',?,?,?)",
            (
                revision_id,
                workspace,
                state,
                bridge,
                year,
                canonical_json_text(document),
                canonical_hash,
                "b" * 64,
                "fixture",
                "2026-08-14T12:00:00.000Z",
            ),
        )
        connection.execute(
            "INSERT INTO published_snapshots "
            "(id,workspace_id,state_code,bridge_number,data_year,snapshot_revision_id,published_by,published_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), workspace, state, bridge, year, revision_id,
                "fixture", "2026-08-14T12:01:00.000Z",
            ),
        )
        connection.commit()

    workspace_token = "ws-" + hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:24]
    point_root = root / "attachments" / "point_clouds" / workspace_token / "34_1"
    source = point_root / "source.e57"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"ASTM-E57 accepted backup fixture")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO bridge_attachments "
            "(state_code,bridge_number,attachment_type,filename,filepath,size_bytes,sha256,workspace_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                str(state), bridge, "point_cloud", "source.e57",
                source.relative_to(root).as_posix(), source.stat().st_size,
                source_sha, workspace,
            ),
        )
        connection.commit()

    generation = "pcv2-1-" + "a" * 32 + "-" + "b" * 12
    generation_root = point_root / "processed" / generation
    generation_root.mkdir(parents=True)
    npz = generation_root / "processed_cloud.npz"
    ply = generation_root / "processed_cloud.ply"
    knowledge = generation_root / "knowledge_pack.json"
    npz.write_bytes(b"immutable npz fixture")
    ply.write_bytes(b"ply\nimmutable fixture\n")
    knowledge.write_bytes(b'{"schema_version":"fixture-v1"}')
    knowledge_sha = hashlib.sha256(knowledge.read_bytes()).hexdigest()
    analysis = {
        "status": "success",
        "persistence": {
            "artifact_sha256": {
                "npz": hashlib.sha256(npz.read_bytes()).hexdigest(),
                "ply": hashlib.sha256(ply.read_bytes()).hexdigest(),
                "knowledge_pack": knowledge_sha,
            }
        },
    }
    analysis_path = generation_root / "analysis.json"
    analysis_path.write_bytes(
        json.dumps(analysis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    result_sha = hashlib.sha256(analysis_path.read_bytes()).hexdigest()

    run, created = create_point_cloud_run(
        str(database),
        CreatePointCloudRunRequest(
            workspace_id=workspace,
            actor_id="analyst@example.com",
            job_id=str(uuid.uuid4()),
            idempotency_key="backup-fixture-run",
            state_code=state,
            bridge_number=bridge,
            data_year=year,
            snapshot_revision_id=revision_id,
            canonical_sha256=canonical_hash,
            source=PointCloudSourceEvidence(
                "point_cloud", "attachment:backup-fixture", source_sha,
                source.stat().st_size, {"format": "e57"},
            ),
            analysis_profile_sha256="c" * 64,
            knowledge_pack_sha256=knowledge_sha,
            report_source_set_sha256=None,
        ),
    )
    assert created is True
    running = transition_point_cloud_run(
        str(database),
        TransitionPointCloudRunRequest(
            workspace, run.run_id, "queued", "running", "geometry",
            {"source_generation": generation, "completed": 1, "total": 2},
        ),
    )
    succeeded = transition_point_cloud_run(
        str(database),
        TransitionPointCloudRunRequest(
            workspace, run.run_id, "running", "succeeded", "complete",
            {"source_generation": generation, "completed": 2, "total": 2},
            result_sha,
        ),
    )
    reviewed = review_point_cloud_run(
        str(database),
        ReviewPointCloudRunRequest(
            workspace_id=workspace,
            run_id=succeeded.run_id,
            review_id=str(uuid.uuid4()),
            reviewer_id="analyst@example.com",
            decision="accept",
            reason="survey evidence reviewed for backup fixture",
            expected_etag=succeeded.etag,
            corrections={},
        ),
    )
    assert running.status == "running" and reviewed.published is True
    return run.run_id, ply


class TestBackup:
    def test_research_backup_needs_no_customer_marker_or_current_schema(
        self, test_workspace: Path
    ):
        data = test_workspace / "research-data"
        data.mkdir()
        with closing(sqlite3.connect(data / "snbi_bridges.db")) as connection:
            connection.execute(
                "CREATE TABLE bridges (state_code TEXT, bridge_number TEXT, data_year INTEGER)"
            )
            connection.execute("INSERT INTO bridges VALUES ('34','0101150',2024)")
            connection.commit()
        init_forecast_db(str(data / "snbi_forecasts.db"))
        report = data / "attachments" / "reports" / "sample.pdf"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"%PDF-1.7\nresearch fixture\n")
        environment = {
            "BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared",
            "BRIDGEGUARD_WORKSPACE_ID": "shared-research-workspace",
        }
        with (
            patch.object(backup, "DATA_DIR", data),
            patch.dict(backup.os.environ, environment, clear=False),
        ):
            target = backup.create_backup(test_workspace / "research-backups")

        manifest = backup.verify_backup(target, require_current=True)
        assert manifest["deployment"] == {
            "format": "bridgeguard-research-backup-identity-v1",
            "mode": "research_shared",
            "deployment_id": "unbound-research",
            "customer_slug": "",
            "workspace_id": "shared-research-workspace",
            "marker_sha256": "",
        }
        assert backup.DEPLOYMENT_MARKER_FILENAME not in manifest["files"]
        assert manifest["database_payloads"] == [
            "snbi_bridges.db",
            "snbi_forecasts.db",
        ]
        assert manifest["database_health"]["snbi_bridges.db"]["user_version"] == 0
        assert manifest["research_storage"]["file_count"] == 1
        assert backup.verify_restore_drill(
            target,
            workspace_root=test_workspace / "research-restore-drills",
            require_current=True,
        )["verified"] is True

    def test_backup_reconciles_accepted_point_cloud_run_and_rejects_tamper(
        self, test_workspace: Path
    ):
        data = test_workspace / "data"
        environment, _attachment = _prepare_customer_data(data)
        _run_id, ply = _add_accepted_point_cloud_run(data)
        with (
            patch.object(backup, "DATA_DIR", data),
            patch.dict(backup.os.environ, environment, clear=False),
        ):
            target = backup.create_backup(test_workspace / "backups")
        manifest = backup.verify_backup(target, require_current=True)
        assert manifest["point_cloud_runs"]["run_count"] == 1
        assert manifest["point_cloud_runs"]["published_count"] == 1
        assert manifest["point_cloud_runs"]["artifact_bytes"] > 0
        assert backup.verify_restore_drill(
            target,
            workspace_root=test_workspace / "restore-drills",
            require_current=True,
        )["verified"] is True

        ply.write_bytes(b"tampered immutable artifact")
        with (
            patch.object(backup, "DATA_DIR", data),
            patch.dict(backup.os.environ, environment, clear=False),
            pytest.raises(RuntimeError, match="artifact digest mismatch"),
        ):
            backup.create_backup(test_workspace / "tampered-backups")

    def test_backup_contains_bound_databases_and_reconciled_attachments(
        self, test_workspace: Path
    ):
        data = test_workspace / "data"
        output = test_workspace / "backups"
        target = _create_current_backup(data, output)

        manifest = backup.verify_backup(target, require_current=True)
        assert manifest["format"] == backup.CURRENT_BACKUP_FORMAT
        assert set(backup.BACKUP_DATABASES).issubset(manifest["files"])
        assert manifest["deployment"]["workspace_id"] == "customer-a-production"
        assert manifest["deployment"]["customer_slug"] == "customer-a"
        assert manifest["deployment"]["deployment_id"]
        assert len(manifest["deployment"]["marker_sha256"]) == 64
        assert manifest["attachments"] == {
            "count": 1,
            "catalog_sha256": manifest["attachments"]["catalog_sha256"],
            "total_bytes": len(b"%PDF-1.7\ncurrent customer report\n"),
        }
        assert manifest["point_cloud_runs"] == {
            "run_count": 0,
            "published_count": 0,
            "pending_retention_count": 0,
            "artifact_bytes": 0,
            "catalog_sha256": manifest["point_cloud_runs"]["catalog_sha256"],
        }

        for database_name in backup.BACKUP_DATABASES:
            assert manifest["database_health"][database_name]["quick_check"] == "ok"
        assert (
            manifest["database_health"]["snbi_bridges.db"]["user_version"]
            == LATEST_SCHEMA_VERSION
        )
        assert manifest["database_health"]["operation_jobs.db"]["user_version"] == 2

        drill = backup.verify_restore_drill(
            target,
            workspace_root=test_workspace / "restore-drills",
            require_current=True,
        )
        assert drill["verified"] is True
        assert drill["source_backup_format"] == backup.CURRENT_BACKUP_FORMAT
        assert drill["extracted_file_count"] >= 2

        (target / "files.tar.gz").write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="checksum"):
            backup.verify_backup(target, require_current=True)

    def test_current_backup_rejects_corrupt_attachment_before_success(
        self, test_workspace: Path
    ):
        data = test_workspace / "data"
        environment, attachment = _prepare_customer_data(data)
        attachment.write_bytes(b"changed after cataloging")
        with (
            patch.object(backup, "DATA_DIR", data),
            patch.dict(backup.os.environ, environment, clear=False),
            pytest.raises(RuntimeError, match="size/hash"),
        ):
            backup.create_backup(test_workspace / "backups")
        backup_root = test_workspace / "backups"
        assert not list(backup_root.glob("*.incomplete-*"))
        assert not backup_root.exists() or not [
            path for path in backup_root.iterdir() if path.is_dir()
        ]

    def test_restore_drill_rejects_missing_attachment_from_archive(
        self, test_workspace: Path
    ):
        target = _create_current_backup(
            test_workspace / "data", test_workspace / "backups"
        )
        archive_path = target / "files.tar.gz"
        replacement = target / "replacement.tar.gz"
        omitted = "attachments/reports/34_1/report.pdf"
        with (
            tarfile.open(archive_path, "r:gz") as source,
            tarfile.open(replacement, "w:gz") as destination,
        ):
            for member in source.getmembers():
                if member.name == omitted:
                    continue
                stream = source.extractfile(member) if member.isfile() else None
                destination.addfile(member, stream)
        replacement.replace(archive_path)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["files.tar.gz"] = backup._file_record(archive_path)
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(RuntimeError, match="attachment catalog path is missing"):
            backup.verify_restore_drill(
                target,
                workspace_root=test_workspace / "restore-drills",
                require_current=True,
            )

    def test_current_backup_rejects_wrong_marker_identity(self, test_workspace: Path):
        target = _create_current_backup(
            test_workspace / "data", test_workspace / "backups"
        )
        marker_path = target / backup.DEPLOYMENT_MARKER_FILENAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["deployment_id"] = "00000000-0000-4000-8000-000000000000"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"][backup.DEPLOYMENT_MARKER_FILENAME] = backup._file_record(
            marker_path
        )
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(RuntimeError, match="identity does not match"):
            backup.verify_backup(target, require_current=True)

    def test_restore_drill_rejects_missing_required_database(
        self, test_workspace: Path
    ):
        target = _create_current_backup(
            test_workspace / "data", test_workspace / "backups"
        )
        (target / "operation_jobs.db").unlink()
        with pytest.raises(RuntimeError, match="payload is missing"):
            backup.verify_restore_drill(
                target,
                workspace_root=test_workspace / "restore-drills",
                require_current=True,
            )

    def test_current_verification_rejects_operation_job_schema_drift(
        self, test_workspace: Path
    ):
        target = _create_current_backup(
            test_workspace / "data", test_workspace / "backups"
        )
        jobs = target / "operation_jobs.db"
        with closing(sqlite3.connect(jobs)) as connection:
            connection.execute("DROP TRIGGER trg_operation_jobs_payload_immutable")
            connection.commit()
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["operation_jobs.db"] = backup._file_record(jobs)
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(RuntimeError, match="indexes/triggers"):
            backup.verify_backup(target, require_current=True)

    def test_legacy_recent_backup_is_not_eligible_for_release_reuse(
        self, test_workspace: Path
    ):
        (test_workspace / "manifest.json").write_text(
            json.dumps({"format": "bridgeguard-backup-v3", "files": {}}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="not eligible for release reuse"):
            backup.verify_backup(test_workspace, require_current=True)

    def test_verify_rejects_unknown_manifest_format(self, test_workspace: Path):
        (test_workspace / "manifest.json").write_text(
            json.dumps({"format": "unknown", "files": {}}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="unsupported"):
            backup.verify_backup(test_workspace)

    def test_restore_drill_rejects_unsafe_archive_members(self, test_workspace: Path):
        target = _create_current_backup(
            test_workspace / "data", test_workspace / "backups"
        )
        with tarfile.open(target / "files.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["files.tar.gz"] = backup._file_record(
            target / "files.tar.gz"
        )
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(RuntimeError, match="unsafe path"):
            backup.verify_restore_drill(
                target,
                workspace_root=test_workspace / "restore-drills",
                require_current=True,
            )


def test_release_backup_quiesces_old_app_and_uses_candidate_image():
    root = Path(__file__).resolve().parents[2]
    script = (root / "deploy/oci/backup.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/deploy-oci.yml").read_text(encoding="utf-8")

    assert "backup skipped: no running app" not in script
    assert "backup failed: the existing app container is not running" in script
    assert "compose exec" not in script
    assert "compose run --rm --no-deps -T app" in script
    assert script.index("compose stop -t 30 cloudflared") < script.index(
        "compose stop -t 120 app"
    ) < script.index("candidate python -m app.backup")
    assert "--verify-current" in script
    assert "--restore-drill-current" in script
    assert 'docker start "$app_container"' in script

    build = workflow.index("compose build --pull app")
    backup_command = workflow.index("sh deploy/oci/backup.sh")
    start = workflow.index("compose up", backup_command)
    assert build < backup_command < start
    assert "compose up \\\n              -d --build" not in workflow
