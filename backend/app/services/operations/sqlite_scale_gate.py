"""Disposable SQLite concurrency gate for a BridgeGuard release.

The gate intentionally cannot accept an application database path.  Every run
creates a uniquely named database below a temporary directory and destroys it
after collecting a content-free receipt.  It is therefore safe to execute from
deployment verification while the real customer database is mounted.

The exercise covers the production SQLite assumptions that ordinary unit tests
cannot establish independently: two writers, at least twenty simultaneous
snapshot readers, bounded lock contention, request-owned interruption, WAL
checkpoint health, and an online-backup restore whose logical contents match.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

from app.routers.services.sqlite_runtime import require_safe_sqlite_wal_runtime


MIN_WRITERS = 2
MIN_READERS = 20
DEFAULT_WRITER_ITERATIONS = 12
DEFAULT_BUSY_TIMEOUT_MS = 100
DEFAULT_GATE_TIMEOUT_SECONDS = 30.0
_WORKSPACE_ID = "sqlite-release-gate"
_SCRATCH_FILENAMES = (
    "disposable-scale-gate.db",
    "concurrent-backup.db",
    "final-backup.db",
)


class SQLiteScaleGateError(RuntimeError):
    """Raised when a release-critical SQLite invariant is not demonstrated."""


@dataclass(frozen=True, slots=True)
class SQLiteScaleGateReceipt:
    format: str
    sqlite_runtime_version: str
    runtime_fix_source: str
    writers: int
    readers: int
    writer_iterations: int
    committed_writes: int
    final_data_generation: int
    reader_snapshots_verified: int
    reader_refreshes_verified: int
    writer_busy_retries: int
    deterministic_busy_wait_ms: float
    maximum_observed_busy_wait_ms: float
    cancellation_latency_ms: float
    passive_checkpoint_busy: int
    passive_checkpoint_log_frames: int
    passive_checkpointed_frames: int
    truncate_checkpoint_busy: int
    wal_bytes_before_checkpoint: int
    wal_bytes_after_checkpoint: int
    concurrent_backup_generation: int
    concurrent_backup_rows: int
    restored_generation: int
    restored_rows: int
    restore_quick_check: str
    restore_foreign_key_issues: int
    logical_sha256: str
    elapsed_ms: float

    def public_dict(self) -> dict[str, object]:
        """Return a stable receipt containing no paths, records, or secrets."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class _DatabaseSnapshot:
    generation: int
    rows: int
    logical_sha256: str
    quick_check: str
    foreign_key_issues: int


@contextmanager
def _disposable_directory(scratch_root: Path | None):
    """Create and remove only a UUID-named directory owned by this probe.

    ``TemporaryDirectory`` applies a Windows ACL that is incompatible with the
    restricted desktop test runner.  Creating the UUID child directly also
    lets cleanup enumerate the exact SQLite artifacts instead of using a broad
    recursive delete.
    """

    parent = (
        Path(tempfile.gettempdir()).resolve()
        if scratch_root is None
        else Path(scratch_root).resolve()
    )
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("scratch_root must be an existing regular directory")
    target = parent / f"bridgeguard-sqlite-scale-{uuid.uuid4()}"
    target.mkdir()
    try:
        yield target
    finally:
        for filename in _SCRATCH_FILENAMES:
            for suffix in ("-wal", "-shm", "-journal", ""):
                candidate = target / f"{filename}{suffix}"
                try:
                    candidate.unlink(missing_ok=True)
                except PermissionError:
                    # Surface a cleanup failure through rmdir without ever
                    # widening deletion to files this process did not create.
                    pass
        target.rmdir()


def _validate_configuration(
    *,
    writer_count: int,
    reader_count: int,
    writer_iterations: int,
    busy_timeout_ms: int,
    gate_timeout_seconds: float,
) -> None:
    if isinstance(writer_count, bool) or writer_count < MIN_WRITERS:
        raise ValueError(f"writer_count must be at least {MIN_WRITERS}")
    if isinstance(reader_count, bool) or reader_count < MIN_READERS:
        raise ValueError(f"reader_count must be at least {MIN_READERS}")
    if isinstance(writer_iterations, bool) or not 1 <= writer_iterations <= 1_000:
        raise ValueError("writer_iterations must be between 1 and 1000")
    if isinstance(busy_timeout_ms, bool) or not 10 <= busy_timeout_ms <= 1_000:
        raise ValueError("busy_timeout_ms must be between 10 and 1000")
    if not 5.0 <= float(gate_timeout_seconds) <= 120.0:
        raise ValueError("gate_timeout_seconds must be between 5 and 120")


def _connect(
    database: Path,
    *,
    readonly: bool = False,
    busy_timeout_ms: int,
) -> sqlite3.Connection:
    if readonly:
        target = f"file:{database.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(
            target,
            uri=True,
            isolation_level=None,
            timeout=busy_timeout_ms / 1_000,
        )
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(
            str(database),
            isolation_level=None,
            timeout=busy_timeout_ms / 1_000,
        )
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return connection


def _initialize(database: Path, *, busy_timeout_ms: int) -> None:
    with closing(
        _connect(database, readonly=False, busy_timeout_ms=busy_timeout_ms)
    ) as connection:
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if journal_mode.lower() != "wal":
            raise SQLiteScaleGateError("disposable database did not enter WAL mode")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE workspace_state (
                workspace_id TEXT PRIMARY KEY
                    REFERENCES workspaces(id) ON DELETE CASCADE,
                data_generation INTEGER NOT NULL CHECK(data_generation >= 0)
            );
            CREATE TABLE scale_events (
                id INTEGER PRIMARY KEY,
                workspace_id TEXT NOT NULL
                    REFERENCES workspaces(id) ON DELETE CASCADE,
                writer_id INTEGER NOT NULL,
                writer_sequence INTEGER NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(workspace_id, writer_id, writer_sequence)
            );
            COMMIT;
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO workspaces(id) VALUES (?)", (_WORKSPACE_ID,))
        connection.execute(
            "INSERT INTO workspace_state(workspace_id, data_generation) VALUES (?, 0)",
            (_WORKSPACE_ID,),
        )
        connection.commit()


def _deterministic_busy_probe(database: Path, *, busy_timeout_ms: int) -> float:
    """Hold a writer reservation and prove a contender stops at its bound."""

    holder = _connect(database, readonly=False, busy_timeout_ms=busy_timeout_ms)
    contender = _connect(database, readonly=False, busy_timeout_ms=busy_timeout_ms)
    try:
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            contender.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            elapsed_ms = (time.monotonic() - started) * 1_000
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise SQLiteScaleGateError(
                    "writer contention returned an unexpected SQLite error"
                ) from exc
        else:
            contender.rollback()
            raise SQLiteScaleGateError("contending writer bypassed an active reservation")
        # Allow scheduler/timer granularity, but reject an effectively unbounded
        # busy wait.  The lower bound catches a connection that ignored policy.
        if elapsed_ms < busy_timeout_ms * 0.5:
            raise SQLiteScaleGateError("SQLite busy timeout returned prematurely")
        if elapsed_ms > busy_timeout_ms + 750:
            raise SQLiteScaleGateError("SQLite busy timeout exceeded its release bound")
        return elapsed_ms
    finally:
        try:
            holder.rollback()
        finally:
            holder.close()
            contender.close()


def _writer(
    database: Path,
    *,
    writer_id: int,
    iterations: int,
    busy_timeout_ms: int,
    operation_timeout_seconds: float,
    first_write: threading.Event,
    backup_started: threading.Event,
) -> tuple[int, tuple[float, ...]]:
    connection = _connect(database, readonly=False, busy_timeout_ms=busy_timeout_ms)
    connection.execute("PRAGMA synchronous=FULL")
    busy_waits: list[float] = []
    committed = 0
    try:
        for sequence in range(iterations):
            deadline = time.monotonic() + operation_timeout_seconds
            while True:
                started = time.monotonic()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO scale_events "
                        "(workspace_id, writer_id, writer_sequence, payload) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            _WORKSPACE_ID,
                            writer_id,
                            sequence,
                            hashlib.sha256(
                                f"{writer_id}:{sequence}".encode("ascii")
                            ).hexdigest()
                            * 8,
                        ),
                    )
                    connection.execute(
                        "UPDATE workspace_state SET data_generation=data_generation+1 "
                        "WHERE workspace_id=?",
                        (_WORKSPACE_ID,),
                    )
                    connection.commit()
                    committed += 1
                    if committed == 1:
                        first_write.set()
                        # Keep at least one writer task active until the online
                        # backup has opened against the live WAL database.
                        if writer_id == 0 and not backup_started.wait(
                            min(2.0, operation_timeout_seconds)
                        ):
                            raise SQLiteScaleGateError(
                                "online backup did not start while writers were active"
                            )
                    break
                except sqlite3.OperationalError as exc:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    busy_waits.append((time.monotonic() - started) * 1_000)
                    if time.monotonic() >= deadline:
                        raise SQLiteScaleGateError(
                            f"writer {writer_id} exceeded its bounded retry deadline"
                        ) from exc
                    time.sleep(0.002)
        return committed, tuple(busy_waits)
    finally:
        connection.close()


def _reader(
    database: Path,
    *,
    busy_timeout_ms: int,
    readers_ready: threading.Barrier,
    writers_done: threading.Event,
    expected_generation: int,
    timeout_seconds: float,
) -> tuple[bool, bool]:
    connection = _connect(database, readonly=True, busy_timeout_ms=busy_timeout_ms)
    try:
        connection.execute("BEGIN")
        initial_generation = int(
            connection.execute(
                "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
                (_WORKSPACE_ID,),
            ).fetchone()[0]
        )
        initial_rows = int(connection.execute("SELECT COUNT(*) FROM scale_events").fetchone()[0])
        readers_ready.wait(timeout=timeout_seconds)
        if not writers_done.wait(timeout_seconds):
            raise SQLiteScaleGateError("reader timed out waiting for concurrent writers")
        snapshot_generation = int(
            connection.execute(
                "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
                (_WORKSPACE_ID,),
            ).fetchone()[0]
        )
        snapshot_rows = int(connection.execute("SELECT COUNT(*) FROM scale_events").fetchone()[0])
        snapshot_ok = (
            initial_generation == 0
            and initial_rows == 0
            and snapshot_generation == initial_generation
            and snapshot_rows == initial_rows
        )
        connection.commit()
        refreshed_generation = int(
            connection.execute(
                "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
                (_WORKSPACE_ID,),
            ).fetchone()[0]
        )
        refreshed_rows = int(connection.execute("SELECT COUNT(*) FROM scale_events").fetchone()[0])
        refresh_ok = (
            refreshed_generation == expected_generation
            and refreshed_rows == expected_generation
        )
        return snapshot_ok, refresh_ok
    finally:
        connection.close()


def _logical_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    generation = int(
        connection.execute(
            "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
            (_WORKSPACE_ID,),
        ).fetchone()[0]
    )
    digest.update(f"generation:{generation}\n".encode("ascii"))
    for row in connection.execute(
        "SELECT writer_id, writer_sequence, payload FROM scale_events "
        "ORDER BY writer_id, writer_sequence"
    ):
        digest.update(json.dumps(list(row), separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _inspect_database(database: Path, *, busy_timeout_ms: int) -> _DatabaseSnapshot:
    connection = _connect(database, readonly=True, busy_timeout_ms=busy_timeout_ms)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_issues = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        generation = int(
            connection.execute(
                "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
                (_WORKSPACE_ID,),
            ).fetchone()[0]
        )
        rows = int(connection.execute("SELECT COUNT(*) FROM scale_events").fetchone()[0])
        return _DatabaseSnapshot(
            generation=generation,
            rows=rows,
            logical_sha256=_logical_digest(connection),
            quick_check=quick_check,
            foreign_key_issues=foreign_key_issues,
        )
    finally:
        connection.close()


def _online_backup(
    source: Path,
    destination: Path,
    *,
    busy_timeout_ms: int,
    timeout_seconds: float,
    started: threading.Event | None = None,
) -> _DatabaseSnapshot:
    source_connection = _connect(source, readonly=True, busy_timeout_ms=busy_timeout_ms)
    destination_connection = _connect(
        destination, readonly=False, busy_timeout_ms=busy_timeout_ms
    )
    deadline = time.monotonic() + timeout_seconds

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise SQLiteScaleGateError("online backup exceeded its time bound")

    try:
        if started is not None:
            started.set()
        source_connection.backup(
            destination_connection,
            pages=16,
            progress=progress,
            sleep=0.005,
        )
    finally:
        destination_connection.close()
        source_connection.close()
    return _inspect_database(destination, busy_timeout_ms=busy_timeout_ms)


def _cancellation_probe(database: Path, *, busy_timeout_ms: int) -> float:
    cancellation = threading.Event()
    connection = _connect(database, readonly=True, busy_timeout_ms=busy_timeout_ms)
    connection.set_progress_handler(lambda: 1 if cancellation.is_set() else 0, 100)
    cancellation.set()
    started = time.monotonic()
    try:
        connection.execute(
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 100000000"
            ") SELECT sum(value) FROM counter"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        elapsed_ms = (time.monotonic() - started) * 1_000
        if "interrupt" not in str(exc).lower():
            raise SQLiteScaleGateError(
                "cancellation probe returned an unexpected SQLite error"
            ) from exc
    else:
        raise SQLiteScaleGateError("SQLite request cancellation did not interrupt work")
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
    if elapsed_ms > 1_000:
        raise SQLiteScaleGateError("SQLite request cancellation exceeded one second")
    return elapsed_ms


def _future_result(future: Future, *, timeout_seconds: float):
    try:
        return future.result(timeout=timeout_seconds)
    except TimeoutError as exc:
        raise SQLiteScaleGateError("SQLite scale exercise exceeded its time bound") from exc


def run_sqlite_scale_gate(
    *,
    writer_count: int = MIN_WRITERS,
    reader_count: int = MIN_READERS,
    writer_iterations: int = DEFAULT_WRITER_ITERATIONS,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    gate_timeout_seconds: float = DEFAULT_GATE_TIMEOUT_SECONDS,
    scratch_root: Path | None = None,
) -> SQLiteScaleGateReceipt:
    """Run the release gate against a newly created disposable database.

    There is deliberately no ``database`` parameter.  ``scratch_root`` only
    controls where the probe creates its UUID-named child and is useful for
    operators who need the same filesystem characteristics as the deployment
    temp volume.
    """

    _validate_configuration(
        writer_count=writer_count,
        reader_count=reader_count,
        writer_iterations=writer_iterations,
        busy_timeout_ms=busy_timeout_ms,
        gate_timeout_seconds=gate_timeout_seconds,
    )
    # Fail before creating a test artifact when the running process is not the
    # pinned/approved WAL runtime.
    runtime = require_safe_sqlite_wal_runtime("wal")
    started_at = time.monotonic()
    with _disposable_directory(scratch_root) as temporary_root:
        database = temporary_root / "disposable-scale-gate.db"
        concurrent_backup = temporary_root / "concurrent-backup.db"
        final_backup = temporary_root / "final-backup.db"
        _initialize(database, busy_timeout_ms=busy_timeout_ms)
        deterministic_busy_wait = _deterministic_busy_probe(
            database, busy_timeout_ms=busy_timeout_ms
        )

        expected_generation = writer_count * writer_iterations
        readers_ready = threading.Barrier(reader_count + 1)
        writers_done = threading.Event()
        first_write = threading.Event()
        backup_started = threading.Event()
        reader_futures: list[Future[tuple[bool, bool]]] = []
        writer_futures: list[Future[tuple[int, tuple[float, ...]]]] = []
        backup_future: Future[_DatabaseSnapshot] | None = None
        with ThreadPoolExecutor(
            max_workers=reader_count + writer_count + 1,
            thread_name_prefix="sqlite-release-gate",
        ) as executor:
            for _ in range(reader_count):
                reader_futures.append(
                    executor.submit(
                        _reader,
                        database,
                        busy_timeout_ms=busy_timeout_ms,
                        readers_ready=readers_ready,
                        writers_done=writers_done,
                        expected_generation=expected_generation,
                        timeout_seconds=gate_timeout_seconds,
                    )
                )
            try:
                readers_ready.wait(timeout=gate_timeout_seconds)
                for writer_id in range(writer_count):
                    writer_futures.append(
                        executor.submit(
                            _writer,
                            database,
                            writer_id=writer_id,
                            iterations=writer_iterations,
                            busy_timeout_ms=busy_timeout_ms,
                            operation_timeout_seconds=min(5.0, gate_timeout_seconds / 2),
                            first_write=first_write,
                            backup_started=backup_started,
                        )
                    )

                def backup_after_first_write() -> _DatabaseSnapshot:
                    if not first_write.wait(gate_timeout_seconds):
                        raise SQLiteScaleGateError(
                            "online backup timed out waiting for a committed write"
                        )
                    return _online_backup(
                        database,
                        concurrent_backup,
                        busy_timeout_ms=busy_timeout_ms,
                        timeout_seconds=gate_timeout_seconds,
                        started=backup_started,
                    )

                backup_future = executor.submit(backup_after_first_write)
                writer_results = [
                    _future_result(future, timeout_seconds=gate_timeout_seconds)
                    for future in writer_futures
                ]
            finally:
                # Readers must always leave their transactions, including when
                # one writer fails, so the executor and WAL can close cleanly.
                writers_done.set()
                first_write.set()

            reader_results = [
                _future_result(future, timeout_seconds=gate_timeout_seconds)
                for future in reader_futures
            ]
            if backup_future is None:
                raise SQLiteScaleGateError("online backup task was not created")
            concurrent_snapshot = _future_result(
                backup_future, timeout_seconds=gate_timeout_seconds
            )

        committed_writes = sum(item[0] for item in writer_results)
        busy_waits = [wait for _committed, waits in writer_results for wait in waits]
        if committed_writes != expected_generation:
            raise SQLiteScaleGateError("writers did not commit every expected transaction")
        if not all(snapshot_ok for snapshot_ok, _refresh_ok in reader_results):
            raise SQLiteScaleGateError("a reader observed writes inside its open snapshot")
        if not all(refresh_ok for _snapshot_ok, refresh_ok in reader_results):
            raise SQLiteScaleGateError("a reader did not refresh to the new data_generation")
        if (
            concurrent_snapshot.quick_check != "ok"
            or concurrent_snapshot.foreign_key_issues
            or concurrent_snapshot.generation != concurrent_snapshot.rows
            or not 1 <= concurrent_snapshot.generation <= expected_generation
        ):
            raise SQLiteScaleGateError("concurrent online backup was not self-consistent")

        wal_path = Path(f"{database}-wal")
        wal_bytes_before = wal_path.stat().st_size if wal_path.exists() else 0
        checkpoint_connection = _connect(
            database, readonly=False, busy_timeout_ms=busy_timeout_ms
        )
        try:
            passive = checkpoint_connection.execute(
                "PRAGMA wal_checkpoint(PASSIVE)"
            ).fetchone()
            truncate = checkpoint_connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        finally:
            checkpoint_connection.close()
        if passive is None or len(passive) < 3 or truncate is None or len(truncate) < 3:
            raise SQLiteScaleGateError("SQLite checkpoint diagnostics were unavailable")
        passive_busy, passive_log, passive_checkpointed = map(int, passive[:3])
        truncate_busy = int(truncate[0])
        if passive_busy or passive_checkpointed != passive_log or truncate_busy:
            raise SQLiteScaleGateError("WAL checkpoint did not fully drain without readers")
        wal_bytes_after = wal_path.stat().st_size if wal_path.exists() else 0
        if wal_bytes_after != 0:
            raise SQLiteScaleGateError("TRUNCATE checkpoint did not reduce the WAL to zero")

        source_snapshot = _inspect_database(
            database, busy_timeout_ms=busy_timeout_ms
        )
        restored_snapshot = _online_backup(
            database,
            final_backup,
            busy_timeout_ms=busy_timeout_ms,
            timeout_seconds=gate_timeout_seconds,
        )
        if (
            source_snapshot.quick_check != "ok"
            or source_snapshot.foreign_key_issues
            or restored_snapshot.quick_check != "ok"
            or restored_snapshot.foreign_key_issues
            or restored_snapshot != source_snapshot
            or source_snapshot.generation != expected_generation
            or source_snapshot.rows != expected_generation
        ):
            raise SQLiteScaleGateError("online-backup restore did not match the source")

        cancellation_latency = _cancellation_probe(
            database, busy_timeout_ms=busy_timeout_ms
        )
        elapsed_ms = (time.monotonic() - started_at) * 1_000
        if elapsed_ms > gate_timeout_seconds * 1_000:
            raise SQLiteScaleGateError("SQLite release gate exceeded its total time bound")
        maximum_busy = max([deterministic_busy_wait, *busy_waits])
        return SQLiteScaleGateReceipt(
            format="bridgeguard-sqlite-scale-gate-v1",
            sqlite_runtime_version=runtime.runtime_version,
            runtime_fix_source=runtime.fix_source,
            writers=writer_count,
            readers=reader_count,
            writer_iterations=writer_iterations,
            committed_writes=committed_writes,
            final_data_generation=source_snapshot.generation,
            reader_snapshots_verified=sum(1 for snapshot, _refresh in reader_results if snapshot),
            reader_refreshes_verified=sum(1 for _snapshot, refresh in reader_results if refresh),
            writer_busy_retries=len(busy_waits),
            deterministic_busy_wait_ms=round(deterministic_busy_wait, 3),
            maximum_observed_busy_wait_ms=round(maximum_busy, 3),
            cancellation_latency_ms=round(cancellation_latency, 3),
            passive_checkpoint_busy=passive_busy,
            passive_checkpoint_log_frames=passive_log,
            passive_checkpointed_frames=passive_checkpointed,
            truncate_checkpoint_busy=truncate_busy,
            wal_bytes_before_checkpoint=wal_bytes_before,
            wal_bytes_after_checkpoint=wal_bytes_after,
            concurrent_backup_generation=concurrent_snapshot.generation,
            concurrent_backup_rows=concurrent_snapshot.rows,
            restored_generation=restored_snapshot.generation,
            restored_rows=restored_snapshot.rows,
            restore_quick_check=restored_snapshot.quick_check,
            restore_foreign_key_issues=restored_snapshot.foreign_key_issues,
            logical_sha256=restored_snapshot.logical_sha256,
            elapsed_ms=round(elapsed_ms, 3),
        )


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_GATE_TIMEOUT_SECONDS",
    "DEFAULT_WRITER_ITERATIONS",
    "MIN_READERS",
    "MIN_WRITERS",
    "SQLiteScaleGateError",
    "SQLiteScaleGateReceipt",
    "run_sqlite_scale_gate",
]
