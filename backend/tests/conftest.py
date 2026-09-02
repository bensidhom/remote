"""Shared safeguards and temporary workspaces for backend tests.

The repository may contain a real bridge database and uploaded artifacts under
``backend/data``. Tests must never read or mutate those files. Environment
overrides are installed here, before pytest imports any test module (and thus
before application path constants are initialized).

Python's ``tempfile`` directories can inherit unusable ACLs in some sandboxed
Windows environments. The fixture below deliberately uses ordinary directory
creation under a single ignored test-artifact root instead.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DATA_ROOT = (REPO_ROOT / "backend" / "data").resolve()
TEST_RUN_ROOT = (
    REPO_ROOT
    / "backend"
    / ".test-artifacts"
    / f"run-{os.getpid()}-{uuid4().hex[:8]}"
)
TEST_DATA_ROOT = TEST_RUN_ROOT / "data"

# Use regular mkdir rather than tempfile/pytest's tmp_path factory. On Windows,
# the latter may apply restrictive ACLs that make its own directory inaccessible
# in sandboxed development environments.
TEST_DATA_ROOT.mkdir(parents=True, exist_ok=False)
(TEST_DATA_ROOT / "rag_storage").mkdir()
(TEST_DATA_ROOT / "temp").mkdir()

os.environ.update(
    {
        "BRIDGEGUARD_DATA_DIR": str(TEST_DATA_ROOT),
        "RAG_WORKING_DIR": str(TEST_DATA_ROOT / "rag_storage"),
        "SNBI_TEMP_DIR": str(TEST_DATA_ROOT / "temp"),
        "BRIDGEGUARD_ENV": "test",
        "CEREBRAS_API_KEY": "",
        "TEI_EMBED_API_KEY": "",
        "TZ": "UTC",
    }
)


def _sqlite_path(database: object) -> Path | None:
    """Return a filesystem path for SQLite file targets, if one is present."""

    try:
        raw = os.fspath(database)  # type: ignore[arg-type]
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if raw == ":memory:":
        return None
    if raw.startswith("file:"):
        raw = unquote(raw[5:].split("?", 1)[0])
        if raw == ":memory:":
            return None
    return Path(raw).resolve()


@pytest.fixture(autouse=True)
def forbid_production_database_access(monkeypatch: pytest.MonkeyPatch):
    """Fail immediately if a test tries to open the production SQLite tree."""

    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        path = _sqlite_path(database)
        if path is not None and (
            path == PRODUCTION_DATA_ROOT
            or PRODUCTION_DATA_ROOT in path.parents
        ):
            raise AssertionError(
                f"tests may not access the production data tree: {path}"
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)


@pytest.fixture
def test_workspace() -> Path:
    """Create a writable, per-test artifact directory outside source tests."""

    # Keep the path short enough for immutable-artifact temp filenames on
    # Windows installations where long-path support is not enabled.
    path = TEST_RUN_ROOT / "cases" / uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path)


@pytest.fixture
def tmp_path() -> Path:
    """Provide pytest-compatible temporary paths under the isolated run root.

    Pytest's built-in ``tmp_path`` factory uses the account-wide Windows temp
    directory before a test can redirect it.  That directory can be left with
    an unreadable ACL by another sandboxed process.  Keeping the familiar
    fixture name while allocating beneath ``.test-artifacts`` makes every test
    deterministic and preserves the production-data guard above.
    """

    path = TEST_RUN_ROOT / "tmp-path" / uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path)


@pytest.fixture(scope="session", autouse=True)
def verify_application_path_isolation():
    """Prove application path constants resolved to the isolated test root."""

    from app.routers.services import datapaths

    configured = (
        Path(datapaths.DATA_DIR),
        Path(datapaths.USER_DB),
        Path(datapaths.FORECAST_DB),
        Path(datapaths.ATTACHMENTS_DIR),
        Path(datapaths.SAVE_DIR),
        Path(datapaths.RAG_WORKING_DIR),
        Path(datapaths.TEMP_DIR),
    )
    for path in configured:
        resolved = path.resolve()
        assert resolved == TEST_RUN_ROOT or TEST_RUN_ROOT in resolved.parents

    yield


def pytest_sessionfinish(session, exitstatus):
    """Remove the run root even for ``--collect-only`` and failed sessions."""

    expected_parent = (REPO_ROOT / "backend" / ".test-artifacts").resolve()
    resolved = TEST_RUN_ROOT.resolve()
    if resolved.parent != expected_parent or not resolved.name.startswith("run-"):
        raise RuntimeError(f"refusing to remove unexpected test path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
