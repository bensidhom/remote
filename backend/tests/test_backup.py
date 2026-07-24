import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import backup


class BackupTests(unittest.TestCase):
    def test_backup_contains_all_persistent_groups_and_verifies_checksums(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            data = root / "data"
            output = root / "backups"
            data.mkdir()
            for database_name in ("snbi_bridges.db", "snbi_forecasts.db"):
                with closing(sqlite3.connect(data / database_name)) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
                    connection.execute("INSERT INTO sample VALUES ('ready')")
                    connection.commit()
            for directory in ("attachments", "rag_storage", "imports", "reference"):
                path = data / directory
                path.mkdir()
                (path / "payload.txt").write_text(directory, encoding="utf-8")
            (data / "api_usage.json").write_text("{}", encoding="utf-8")

            with patch.object(backup, "DATA_DIR", data):
                target = backup.create_backup(output)

            manifest = backup.verify_backup(target)
            self.assertEqual(manifest["format"], "bridgeguard-backup-v1")
            self.assertIn("snbi_bridges.db", manifest["files"])
            self.assertIn("snbi_forecasts.db", manifest["files"])
            self.assertIn("files.tar.gz", manifest["files"])

            with closing(sqlite3.connect(target / "snbi_bridges.db")) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM sample").fetchone()[0],
                    "ready",
                )

            archive_record = manifest["files"]["files.tar.gz"]
            self.assertGreater(archive_record["bytes"], 0)
            self.assertEqual(len(archive_record["sha256"]), 64)

            (target / "files.tar.gz").write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                backup.verify_backup(target)

    def test_verify_rejects_unknown_manifest_format(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            target = Path(temp)
            (target / "manifest.json").write_text(
                json.dumps({"format": "unknown", "files": {}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                backup.verify_backup(target)


if __name__ == "__main__":
    unittest.main()
