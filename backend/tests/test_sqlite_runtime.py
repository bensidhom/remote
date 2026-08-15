import unittest
from unittest.mock import patch

from app.routers.services.sqlite_runtime import (
    inspect_sqlite_wal_runtime,
    require_safe_sqlite_wal_runtime,
    upstream_wal_fix_present,
)


class SQLiteRuntimeTests(unittest.TestCase):
    def test_upstream_fixed_release_and_backports_are_allowlisted(self):
        for version in ((3, 51, 3), (3, 52, 0), (3, 50, 7), (3, 50, 8), (3, 44, 6)):
            with self.subTest(version=version):
                self.assertTrue(upstream_wal_fix_present(version))

    def test_affected_and_unrelated_branches_are_rejected(self):
        for version in ((3, 51, 2), (3, 50, 6), (3, 49, 9), (3, 45, 9), (3, 44, 5)):
            with self.subTest(version=version):
                self.assertFalse(upstream_wal_fix_present(version))

    def test_affected_runtime_fails_when_wal_is_enabled(self):
        result = inspect_sqlite_wal_runtime("WAL", version=(3, 50, 4), environ={})
        self.assertFalse(result.approved)
        self.assertTrue(result.wal_enabled)
        self.assertEqual(result.fix_source, "none")

    def test_non_wal_database_is_not_blocked_by_wal_specific_gate(self):
        result = inspect_sqlite_wal_runtime("delete", version=(3, 50, 4), environ={})
        self.assertTrue(result.approved)
        self.assertFalse(result.wal_enabled)

    def test_vendor_backport_requires_an_auditable_reference(self):
        missing_reference = inspect_sqlite_wal_runtime(
            "wal",
            version=(3, 40, 1),
            environ={"BRIDGEGUARD_SQLITE_WAL_FIX_BACKPORT": "true"},
        )
        self.assertFalse(missing_reference.approved)

        approved = inspect_sqlite_wal_runtime(
            "wal",
            version=(3, 40, 1),
            environ={
                "BRIDGEGUARD_SQLITE_WAL_FIX_BACKPORT": "true",
                "BRIDGEGUARD_SQLITE_WAL_FIX_REFERENCE": "DEBIAN-DSA-EXAMPLE",
            },
        )
        self.assertTrue(approved.approved)
        self.assertEqual(approved.fix_source, "vendor_backport")

    def test_public_diagnostics_do_not_contain_database_paths(self):
        diagnostics = inspect_sqlite_wal_runtime(
            "wal", version="3.51.3", environ={}
        ).public_dict()
        self.assertEqual(diagnostics["runtime_version"], "3.51.3")
        self.assertNotIn("path", diagnostics)

    def test_runtime_guard_raises_before_an_unsafe_wal_database_can_open(self):
        with patch(
            "app.routers.services.sqlite_runtime.sqlite3.sqlite_version_info",
            (3, 50, 4),
        ), patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Unsafe SQLite WAL runtime"):
                require_safe_sqlite_wal_runtime("wal")


if __name__ == "__main__":
    unittest.main()
