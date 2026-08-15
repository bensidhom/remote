import json
import sqlite3
from contextlib import closing
from pathlib import Path

from scripts import migrate_local_artifacts as migration


def _create_attachment_database(path: Path, rows=()):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            CREATE TABLE bridge_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_code TEXT,
                bridge_number TEXT,
                attachment_type TEXT,
                filename TEXT,
                filepath TEXT,
                size_bytes INTEGER,
                uploaded_at TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO bridge_attachments
                (state_code, bridge_number, attachment_type, filename,
                 filepath, size_bytes, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()


class TestLocalArtifactMigration:
    def test_old_nomic_vectors_are_removed_but_parsed_content_is_kept(
        self, test_workspace
    ):
        destination = test_workspace
        cache = (
            destination
            / "rag_storage"
            / "bridge_reports"
            / "old-revision.json"
        )
        cache.parent.mkdir(parents=True)
        parsed_chunks = [{"text": "deck finding"}]
        parsed_images = [{"path": "page_images/page-1.png"}]
        cache.write_text(
            json.dumps(
                {
                    "chunks": parsed_chunks,
                    "images": parsed_images,
                    "embeddings": [[0.1] * 768],
                    "image_embeddings": [[0.2] * 768],
                    "embedding_profile": {
                        **migration.TARGET_EMBEDDING_PROFILE,
                        "revision": (
                            "e9b6763023c676ca8431644204f50c2b100d9aab"
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )

        invalidated = migration.invalidate_legacy_report_vectors(destination)
        migrated_cache = json.loads(cache.read_text(encoding="utf-8"))

        assert invalidated["caches"] == 1
        assert invalidated["chunks"] == 1
        assert invalidated["vectors_removed"] == 1
        assert migrated_cache["chunks"] == parsed_chunks
        assert migrated_cache["images"] == parsed_images
        assert migrated_cache["embeddings"] is None
        assert migrated_cache["image_embeddings"] is None
        assert migrated_cache["embedding_profile"] is None

    def test_copy_invalidate_and_merge_accepts_windows_attachment_paths(
        self, test_workspace
    ):
        source = test_workspace / "source"
        destination = test_workspace / "destination"
        report = source / "attachments" / "reports" / "34_1" / "report.pdf"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"portable report")
        cache = source / "rag_storage" / "bridge_reports" / "34_1.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(
            json.dumps(
                {
                    "chunks": [{"text": "deck finding"}],
                    "embeddings": [[0.1, 0.2]],
                    "embedding_profile": {"provider": "ollama"},
                }
            ),
            encoding="utf-8",
        )
        destination.mkdir()
        _create_attachment_database(
            source / "snbi_bridges.db",
            [
                (
                    "34",
                    "1",
                    "inspection_report",
                    "report.pdf",
                    r"C:\test\local\backend\data\attachments\reports\34_1\report.pdf",
                    report.stat().st_size,
                    "2026-01-01T00:00:00Z",
                )
            ],
        )
        _create_attachment_database(destination / "snbi_bridges.db")

        copied = migration.copy_portable_artifacts(source, destination)
        invalidated = migration.invalidate_legacy_report_vectors(destination)
        merged = migration.merge_attachment_rows(source, destination)

        assert copied["files_copied"] == 2
        assert invalidated["vectors_removed"] == 1
        assert Path(merged["database_backup"]).is_file()
        with closing(sqlite3.connect(destination / "snbi_bridges.db")) as connection:
            row = connection.execute(
                "SELECT filepath FROM bridge_attachments"
            ).fetchone()
        assert row[0] == "attachments/reports/34_1/report.pdf"
        migrated_cache = json.loads(
            (destination / "rag_storage" / "bridge_reports" / "34_1.json").read_text(
                encoding="utf-8"
            )
        )
        assert migrated_cache["embeddings"] is None
        assert migrated_cache["embedding_profile"] is None

    def test_same_size_different_content_is_reported_as_conflict(
        self, test_workspace
    ):
        source = test_workspace / "source"
        destination = test_workspace / "destination"
        source_file = source / "attachments" / "reports" / "same.bin"
        destination_file = destination / "attachments" / "reports" / "same.bin"
        source_file.parent.mkdir(parents=True)
        destination_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"source")
        destination_file.write_bytes(b"target")

        result = migration.copy_portable_artifacts(source, destination)

        assert result["files_copied"] == 0
        assert result["already_present"] == 0
        assert result["conflicts"] == [str(destination_file)]
