"""Immutable, content-addressed storage for conversion source uploads.

This module intentionally has no FastAPI dependency. A router can stream an
upload into :class:`SourceArtifactStore` without allowing a client-controlled
filename to become a server path. Accepted bytes are deduplicated by SHA-256;
every call still receives fresh job and artifact UUIDs for provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator
from uuid import UUID, uuid4


DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_STORE_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 256 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MAX_INVENTORY_FILES = 1_000_000
MAX_SWEEP_DELETIONS = 1_000
MANIFEST_VERSION = "bridgeguard-source-artifact-v1"
SWEEP_RECEIPT_VERSION = "bridgeguard-source-artifact-sweep-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._() -]+")
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


class ArtifactErrorCode(str, Enum):
    UPLOAD_TOO_LARGE = "upload_too_large"
    EMPTY_UPLOAD = "empty_upload"
    IO_FAILURE = "io_failure"
    INVALID_FILENAME = "invalid_filename"
    INVALID_ARTIFACT_ID = "invalid_artifact_id"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    STORE_QUOTA_EXCEEDED = "artifact_store_quota_exceeded"


class SourceArtifactError(Exception):
    """A public, typed failure that never includes a filesystem path."""

    def __init__(
        self,
        code: ArtifactErrorCode,
        message: str,
        *,
        details: dict[str, int | str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code.value
        self.details = dict(details or {})

    def to_public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": str(self),
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class UploadTooLargeError(SourceArtifactError):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            ArtifactErrorCode.UPLOAD_TOO_LARGE,
            "The upload exceeds the configured byte limit.",
            details={"max_bytes": max_bytes},
        )


class EmptyUploadError(SourceArtifactError):
    def __init__(self) -> None:
        super().__init__(
            ArtifactErrorCode.EMPTY_UPLOAD,
            "The upload contains no bytes.",
        )


class ArtifactIOError(SourceArtifactError):
    def __init__(self) -> None:
        super().__init__(
            ArtifactErrorCode.IO_FAILURE,
            "The upload could not be stored.",
        )


class ArtifactStoreQuotaError(SourceArtifactError):
    def __init__(
        self, *, max_store_bytes: int, used_bytes: int, requested_bytes: int
    ) -> None:
        super().__init__(
            ArtifactErrorCode.STORE_QUOTA_EXCEEDED,
            "The immutable artifact store has reached its configured byte quota.",
            details={
                "max_store_bytes": max_store_bytes,
                "used_bytes": used_bytes,
                "requested_bytes": requested_bytes,
            },
        )


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    """Path-free metadata safe to return through an API."""

    artifact_id: str
    job_id: str
    sha256: str
    size_bytes: int
    original_filename: str
    created_at: str
    status: str = "accepted"

    def to_public_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArtifactSweepReport:
    """Path-free, bounded receipt for one accepted-artifact sweep."""

    created_at: str
    cutoff: str
    scanned_manifests: int
    protected_manifests: int
    young_manifests: int
    corrupt_manifests: int
    deleted_manifest_count: int
    deleted_object_count: int
    reclaimed_bytes: int
    retained_object_count: int
    deletion_limit_reached: bool
    deleted_artifact_ids: tuple[str, ...]
    deleted_object_sha256s: tuple[str, ...]
    receipt_sha256: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "format": SWEEP_RECEIPT_VERSION,
            **asdict(self),
            "deleted_artifact_ids": list(self.deleted_artifact_ids),
            "deleted_object_sha256s": list(self.deleted_object_sha256s),
        }


class SourceArtifactStore:
    """Store immutable upload bytes and path-free provenance manifests."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_store_bytes: int | None = None,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if (
            not isinstance(chunk_bytes, int)
            or isinstance(chunk_bytes, bool)
            or not 1 <= chunk_bytes <= MAX_CHUNK_BYTES
        ):
            raise ValueError(
                f"chunk_bytes must be between 1 and {MAX_CHUNK_BYTES}"
            )
        if max_store_bytes is None:
            configured_quota = os.getenv(
                "BRIDGEGUARD_SOURCE_ARTIFACT_STORE_MAX_BYTES",
                str(DEFAULT_MAX_STORE_BYTES),
            )
            try:
                max_store_bytes = int(configured_quota)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "BRIDGEGUARD_SOURCE_ARTIFACT_STORE_MAX_BYTES must be a positive integer"
                ) from exc
        if (
            not isinstance(max_store_bytes, int)
            or isinstance(max_store_bytes, bool)
            or max_store_bytes < 1
        ):
            raise ValueError("max_store_bytes must be a positive integer")

        requested_root = Path(root).absolute()
        try:
            self._reject_existing_symlink_chain(requested_root)
            requested_root.mkdir(parents=True, exist_ok=True)
            if requested_root.is_symlink() or not requested_root.is_dir():
                raise OSError("unsafe artifact root")
            self._root = requested_root.resolve(strict=True)
            self._objects = self._root / "objects"
            self._manifests = self._root / "manifests"
            self._staging = self._root / "staging"
            self._gc = self._root / "gc"
            self._gc_receipts = self._gc / "receipts"
            for directory in (
                self._objects,
                self._manifests,
                self._staging,
                self._gc,
                self._gc_receipts,
            ):
                directory.mkdir(exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():
                    raise OSError("unsafe managed directory")
                self._make_private(directory)
            self._make_private(self._root)
        except OSError as exc:
            raise ArtifactIOError() from exc

        self.max_bytes = max_bytes
        self.max_store_bytes = max_store_bytes
        self.chunk_bytes = chunk_bytes
        lock_key = os.path.normcase(str(self._root))
        with _STORE_LOCKS_GUARD:
            self._store_lock = _STORE_LOCKS.setdefault(lock_key, threading.RLock())

    @staticmethod
    def _make_private(path: Path) -> None:
        # chmod(0700) can create unusable ACLs in sandboxed Windows sessions.
        # Windows access control is inherited from the configured private root.
        if os.name != "nt":
            path.chmod(0o700)

    @staticmethod
    def _reject_existing_symlink_chain(path: Path) -> None:
        candidate = path
        while True:
            if candidate.is_symlink():
                raise OSError("symlinked artifact path")
            if candidate.parent == candidate:
                return
            candidate = candidate.parent

    @staticmethod
    def _canonical_uuid(value: str, *, field: str) -> str:
        try:
            parsed = UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise SourceArtifactError(
                ArtifactErrorCode.INVALID_ARTIFACT_ID,
                f"{field} must be a UUID.",
            ) from exc
        return str(parsed)

    @staticmethod
    def sanitize_original_filename(filename: str | None) -> str:
        """Validate traversal syntax and return metadata-safe ASCII text."""

        raw = unicodedata.normalize("NFKC", str(filename or "")).strip()
        if (
            not raw
            or "\x00" in raw
            or "/" in raw
            or "\\" in raw
            or raw in {".", ".."}
            or _WINDOWS_DRIVE_RE.match(raw)
        ):
            raise SourceArtifactError(
                ArtifactErrorCode.INVALID_FILENAME,
                "The original filename is invalid.",
            )
        safe = _SAFE_FILENAME_RE.sub("_", raw)
        safe = re.sub(r"\s+", " ", safe).strip(" .")[:180]
        if not safe or safe in {".", ".."}:
            raise SourceArtifactError(
                ArtifactErrorCode.INVALID_FILENAME,
                "The original filename is invalid.",
            )
        return safe

    def ingest(
        self,
        source: BinaryIO | Iterable[bytes] | bytes | bytearray | memoryview,
        *,
        original_filename: str,
    ) -> ArtifactReceipt:
        """Stream one source into immutable storage and return path-free metadata."""

        safe_filename = self.sanitize_original_filename(original_filename)
        job_id = str(uuid4())
        artifact_id = str(uuid4())
        job_dir = self._confined(self._staging, job_id)
        stage_path = self._confined(job_dir, f"{artifact_id}.part")

        try:
            job_dir.mkdir(parents=False, exist_ok=False)
            self._make_private(job_dir)
            digest = hashlib.sha256()
            size_bytes = 0
            with stage_path.open("xb", buffering=0) as destination:
                for chunk in self._iter_bounded_chunks(source):
                    if size_bytes + len(chunk) > self.max_bytes:
                        raise UploadTooLargeError(self.max_bytes)
                    self._write_all(destination, chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                if size_bytes == 0:
                    raise EmptyUploadError()
                destination.flush()
                os.fsync(destination.fileno())

            sha256 = digest.hexdigest()
            object_path = self._object_path(sha256)
            receipt = ArtifactReceipt(
                artifact_id=artifact_id,
                job_id=job_id,
                sha256=sha256,
                size_bytes=size_bytes,
                original_filename=safe_filename,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            with self._store_lock:
                self._ensure_capacity(
                    object_path=object_path,
                    object_size=size_bytes,
                    manifest_size=len(self._manifest_bytes(receipt)),
                )
                self._finalize_content(stage_path, object_path, sha256, size_bytes)
                self._write_manifest(receipt)
            return receipt
        except SourceArtifactError:
            raise
        except Exception as exc:
            raise ArtifactIOError() from exc
        finally:
            self._unlink_temporary(stage_path)
            try:
                job_dir.rmdir()
            except (FileNotFoundError, OSError):
                pass

    def get(self, artifact_id: str) -> ArtifactReceipt:
        """Return public metadata for one accepted artifact."""

        canonical_id = self._canonical_uuid(artifact_id, field="artifact_id")
        manifest_path = self._confined(
            self._manifests, f"{canonical_id}.json"
        )
        try:
            if manifest_path.is_symlink():
                raise OSError("symlinked manifest")
            raw = manifest_path.read_bytes()
        except FileNotFoundError as exc:
            raise SourceArtifactError(
                ArtifactErrorCode.ARTIFACT_NOT_FOUND,
                "The source artifact was not found.",
            ) from exc
        except OSError as exc:
            raise ArtifactIOError() from exc
        if len(raw) > 16 * 1024:
            raise ArtifactIOError()
        try:
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("format") != MANIFEST_VERSION:
                raise ValueError("manifest version")
            receipt = ArtifactReceipt(
                artifact_id=str(payload["artifact_id"]),
                job_id=str(payload["job_id"]),
                sha256=str(payload["sha256"]),
                size_bytes=int(payload["size_bytes"]),
                original_filename=str(payload["original_filename"]),
                created_at=str(payload["created_at"]),
                status=str(payload["status"]),
            )
            if (
                receipt.artifact_id != canonical_id
                or receipt.status != "accepted"
                or not _SHA256_RE.fullmatch(receipt.sha256)
                or receipt.size_bytes < 1
            ):
                raise ValueError("invalid manifest")
            self._canonical_uuid(receipt.job_id, field="job_id")
            return receipt
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise ArtifactIOError() from exc

    def iter_bytes(
        self, artifact_id: str, *, chunk_bytes: int | None = None
    ) -> Iterator[bytes]:
        """Yield verified content without returning an internal filesystem path."""

        receipt = self.get(artifact_id)
        read_size = self.chunk_bytes if chunk_bytes is None else chunk_bytes
        if (
            not isinstance(read_size, int)
            or isinstance(read_size, bool)
            or not 1 <= read_size <= MAX_CHUNK_BYTES
        ):
            raise ValueError(
                f"chunk_bytes must be between 1 and {MAX_CHUNK_BYTES}"
            )
        object_path = self._object_path(receipt.sha256)
        digest = hashlib.sha256()
        total = 0
        try:
            if object_path.is_symlink():
                raise OSError("symlinked object")
            with object_path.open("rb") as source:
                mode = os.fstat(source.fileno()).st_mode
                if not stat.S_ISREG(mode):
                    raise OSError("object is not a regular file")
                while True:
                    chunk = source.read(read_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    digest.update(chunk)
                    yield chunk
        except OSError as exc:
            raise ArtifactIOError() from exc
        if total != receipt.size_bytes or digest.hexdigest() != receipt.sha256:
            raise ArtifactIOError()

    def read_bytes(self, artifact_id: str) -> bytes:
        """Read one already-bounded artifact without exposing its object path."""

        return b"".join(self.iter_bytes(artifact_id))

    def discard_failed_staging(self, *, job_id: str, artifact_id: str) -> bool:
        """Delete only one explicit staged ``.part`` file.

        Accepted manifests and content objects are deliberately outside this
        method's reach. PR1 does not provide an accepted-artifact purge API.
        """

        canonical_job = self._canonical_uuid(job_id, field="job_id")
        canonical_artifact = self._canonical_uuid(
            artifact_id, field="artifact_id"
        )
        job_dir = self._confined(self._staging, canonical_job)
        stage_path = self._confined(
            job_dir, f"{canonical_artifact}.part"
        )
        try:
            if stage_path.is_symlink():
                raise OSError("symlinked staging file")
            if not stage_path.exists():
                return False
            if not stage_path.is_file():
                raise OSError("staging target is not a regular file")
            stage_path.unlink()
            try:
                job_dir.rmdir()
            except OSError:
                pass
            return True
        except OSError as exc:
            raise ArtifactIOError() from exc

    def persistent_usage_bytes(self) -> int:
        """Return exact managed persistent bytes, excluding temporary staging."""

        with self._store_lock:
            return self._persistent_usage()

    def sweep_unreferenced(
        self,
        *,
        protected_artifact_ids: Iterable[str],
        older_than: datetime,
        now: datetime | None = None,
        max_deletions: int = 100,
    ) -> ArtifactSweepReport:
        """Delete old, unreferenced manifests and last-reference objects.

        ``protected_artifact_ids`` must be derived from durable catalogs before
        calling this method.  A corrupt manifest disables object deletion for
        the whole sweep because its digest reference cannot be proven.  Clean
        unreferenced manifests may still be removed, leaving conservative
        orphan objects for a later healthy sweep.
        """

        if older_than.tzinfo is None:
            raise ValueError("older_than must be timezone-aware")
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = older_than.astimezone(timezone.utc)
        if (
            not isinstance(max_deletions, int)
            or isinstance(max_deletions, bool)
            or not 1 <= max_deletions <= MAX_SWEEP_DELETIONS
        ):
            raise ValueError(
                f"max_deletions must be between 1 and {MAX_SWEEP_DELETIONS}"
            )
        protected = {
            self._canonical_uuid(value, field="protected_artifact_id")
            for value in protected_artifact_ids
        }

        with self._store_lock:
            try:
                manifest_paths = sorted(self._manifests.glob("*.json"))
            except OSError as exc:
                raise ArtifactIOError() from exc
            if len(manifest_paths) > MAX_INVENTORY_FILES:
                raise ArtifactIOError()

            valid: list[tuple[Path, ArtifactReceipt, datetime, int]] = []
            corrupt = 0
            digest_references: dict[str, int] = {}
            protected_count = 0
            young_count = 0
            candidates: list[tuple[Path, ArtifactReceipt, int]] = []
            for manifest_path in manifest_paths:
                try:
                    if manifest_path.is_symlink() or not manifest_path.is_file():
                        raise OSError("unsafe manifest")
                    artifact_id = manifest_path.stem
                    receipt = self.get(artifact_id)
                    created_at = datetime.fromisoformat(
                        receipt.created_at.replace("Z", "+00:00")
                    )
                    if created_at.tzinfo is None:
                        raise ValueError("manifest timestamp must be timezone-aware")
                    manifest_size = manifest_path.stat().st_size
                except (OSError, ValueError, SourceArtifactError):
                    corrupt += 1
                    continue
                created_at = created_at.astimezone(timezone.utc)
                valid.append((manifest_path, receipt, created_at, manifest_size))
                digest_references[receipt.sha256] = (
                    digest_references.get(receipt.sha256, 0) + 1
                )
                if receipt.artifact_id in protected:
                    protected_count += 1
                elif created_at > cutoff:
                    young_count += 1
                else:
                    candidates.append((manifest_path, receipt, manifest_size))

            selected = candidates[:max_deletions]
            deleted_ids: list[str] = []
            deleted_object_digests: list[str] = []
            deleted_objects = 0
            reclaimed = 0
            for manifest_path, receipt, manifest_size in selected:
                try:
                    if manifest_path.is_symlink() or not manifest_path.is_file():
                        raise OSError("unsafe manifest")
                    manifest_path.unlink()
                    reclaimed += manifest_size
                    deleted_ids.append(receipt.artifact_id)
                    digest_references[receipt.sha256] -= 1
                    if digest_references[receipt.sha256] != 0 or corrupt:
                        continue
                    object_path = self._object_path(receipt.sha256)
                    if object_path.is_symlink() or not object_path.is_file():
                        raise OSError("unsafe content object")
                    object_size = object_path.stat().st_size
                    object_path.unlink()
                    reclaimed += object_size
                    deleted_objects += 1
                    deleted_object_digests.append(receipt.sha256)
                    self._remove_empty_object_shards(object_path)
                except OSError as exc:
                    raise ArtifactIOError() from exc

            orphan_candidates: list[tuple[Path, int]] = []
            if not corrupt:
                for object_path in self._inventory_files(self._objects):
                    try:
                        digest = object_path.name
                        if (
                            not _SHA256_RE.fullmatch(digest)
                            or object_path != self._object_path(digest)
                            or object_path.is_symlink()
                        ):
                            corrupt += 1
                            orphan_candidates = []
                            break
                        if digest_references.get(digest, 0):
                            continue
                        modified_at = datetime.fromtimestamp(
                            object_path.stat().st_mtime, tz=timezone.utc
                        )
                        if modified_at <= cutoff:
                            orphan_candidates.append(
                                (object_path, object_path.stat().st_size)
                            )
                    except OSError as exc:
                        raise ArtifactIOError() from exc
            remaining = max_deletions - len(deleted_ids)
            for object_path, object_size in orphan_candidates[:remaining]:
                try:
                    if object_path.is_symlink() or not object_path.is_file():
                        raise OSError("unsafe orphan object")
                    object_path.unlink()
                    reclaimed += object_size
                    deleted_objects += 1
                    deleted_object_digests.append(object_path.name)
                    self._remove_empty_object_shards(object_path)
                except OSError as exc:
                    raise ArtifactIOError() from exc

            retained_objects = len(self._inventory_files(self._objects))
            report = ArtifactSweepReport(
                created_at=instant.isoformat(),
                cutoff=cutoff.isoformat(),
                scanned_manifests=len(manifest_paths),
                protected_manifests=protected_count,
                young_manifests=young_count,
                corrupt_manifests=corrupt,
                deleted_manifest_count=len(deleted_ids),
                deleted_object_count=deleted_objects,
                reclaimed_bytes=reclaimed,
                retained_object_count=retained_objects,
                deletion_limit_reached=(
                    len(candidates) > len(selected)
                    or len(orphan_candidates) > remaining
                ),
                deleted_artifact_ids=tuple(deleted_ids),
                deleted_object_sha256s=tuple(deleted_object_digests),
            )
            if deleted_ids or deleted_objects:
                digest = self._write_sweep_receipt(report)
                report = ArtifactSweepReport(
                    **{
                        **asdict(report),
                        "deleted_artifact_ids": report.deleted_artifact_ids,
                        "deleted_object_sha256s": report.deleted_object_sha256s,
                        "receipt_sha256": digest,
                    }
                )
            return report

    @staticmethod
    def _manifest_bytes(receipt: ArtifactReceipt) -> bytes:
        payload = {"format": MANIFEST_VERSION, **receipt.to_public_dict()}
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def _inventory_files(self, base: Path) -> list[Path]:
        try:
            paths: list[Path] = []
            for path in base.rglob("*"):
                if path.is_symlink():
                    raise OSError("symlinked managed path")
                if path.is_file():
                    paths.append(path)
                    if len(paths) > MAX_INVENTORY_FILES:
                        raise OSError("artifact inventory limit exceeded")
                elif path.exists() and not path.is_dir():
                    raise OSError("unsafe managed path")
            return paths
        except OSError as exc:
            raise ArtifactIOError() from exc

    def _persistent_usage(self) -> int:
        total = 0
        count = 0
        for managed in (self._objects, self._manifests, self._gc_receipts):
            for path in self._inventory_files(managed):
                count += 1
                if count > MAX_INVENTORY_FILES:
                    raise ArtifactIOError()
                try:
                    total += path.stat().st_size
                except OSError as exc:
                    raise ArtifactIOError() from exc
        return total

    def _ensure_capacity(
        self, *, object_path: Path, object_size: int, manifest_size: int
    ) -> None:
        used = self._persistent_usage()
        additional = manifest_size
        try:
            object_exists = object_path.exists()
            if object_exists and (object_path.is_symlink() or not object_path.is_file()):
                raise OSError("unsafe content object")
        except OSError as exc:
            raise ArtifactIOError() from exc
        if not object_exists:
            additional += object_size
        if used + additional > self.max_store_bytes:
            raise ArtifactStoreQuotaError(
                max_store_bytes=self.max_store_bytes,
                used_bytes=used,
                requested_bytes=additional,
            )

    def _remove_empty_object_shards(self, object_path: Path) -> None:
        for directory in (object_path.parent, object_path.parent.parent):
            if directory == self._objects:
                return
            try:
                directory.relative_to(self._objects)
                if directory.is_symlink():
                    raise OSError("symlinked object shard")
                directory.rmdir()
            except OSError:
                return

    def _write_sweep_receipt(self, report: ArtifactSweepReport) -> str:
        body = report.to_public_dict()
        body.pop("receipt_sha256", None)
        encoded_body = json.dumps(
            body, ensure_ascii=True, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded_body).hexdigest()
        payload = {**body, "receipt_sha256": digest}
        encoded = (
            json.dumps(payload, ensure_ascii=True, allow_nan=False,
                       sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        destination = self._confined(self._gc_receipts, f"{digest}.json")
        temporary = self._confined(
            self._gc_receipts, f".{digest}.{uuid4()}.tmp"
        )
        try:
            with temporary.open("xb", buffering=0) as stream:
                self._write_all(stream, memoryview(encoded))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                if destination.is_symlink() or destination.read_bytes() != encoded:
                    raise OSError("sweep receipt collision")
            if os.name != "nt":
                destination.chmod(0o444)
            self._fsync_directory(self._gc_receipts)
            return digest
        except OSError as exc:
            raise ArtifactIOError() from exc
        finally:
            self._unlink_temporary(temporary)

    def _iter_bounded_chunks(
        self,
        source: BinaryIO | Iterable[bytes] | bytes | bytearray | memoryview,
    ) -> Iterator[memoryview]:
        if isinstance(source, str):
            raise ArtifactIOError()
        if isinstance(source, (bytes, bytearray, memoryview)):
            iterator: Iterable[object] = (source,)
        elif callable(getattr(source, "read", None)):
            iterator = self._read_file_chunks(source)  # type: ignore[arg-type]
        else:
            try:
                iterator = iter(source)  # type: ignore[arg-type]
            except TypeError as exc:
                raise ArtifactIOError() from exc

        try:
            for raw_chunk in iterator:
                if raw_chunk is None:
                    raise ArtifactIOError()
                try:
                    view = memoryview(raw_chunk)
                    if not view.contiguous:
                        view = memoryview(bytes(view))
                    view = view.cast("B")
                except (TypeError, ValueError) as exc:
                    raise ArtifactIOError() from exc
                for offset in range(0, len(view), self.chunk_bytes):
                    part = view[offset : offset + self.chunk_bytes]
                    if part:
                        yield part
        except SourceArtifactError:
            raise
        except Exception as exc:
            raise ArtifactIOError() from exc

    def _read_file_chunks(self, source: BinaryIO) -> Iterator[object]:
        while True:
            chunk = source.read(self.chunk_bytes)
            if chunk in (b"", ""):
                return
            if chunk is None:
                raise ArtifactIOError()
            yield chunk

    @staticmethod
    def _write_all(destination: BinaryIO, chunk: memoryview) -> None:
        remaining = chunk
        while remaining:
            written = destination.write(remaining)
            if written is None or written <= 0:
                raise OSError("short artifact write")
            remaining = remaining[written:]

    def _object_path(self, sha256: str) -> Path:
        if not _SHA256_RE.fullmatch(sha256):
            raise ArtifactIOError()
        return self._confined(
            self._objects, sha256[:2], sha256[2:4], sha256
        )

    def _finalize_content(
        self,
        stage_path: Path,
        object_path: Path,
        sha256: str,
        size_bytes: int,
    ) -> None:
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if object_path.parent.is_symlink():
            raise ArtifactIOError()
        try:
            os.link(stage_path, object_path, follow_symlinks=False)
            if os.name != "nt":
                object_path.chmod(0o444)
            self._fsync_directory(object_path.parent)
        except FileExistsError:
            self._verify_existing_object(object_path, sha256, size_bytes)
        except OSError as exc:
            raise ArtifactIOError() from exc

    def _verify_existing_object(
        self, object_path: Path, sha256: str, size_bytes: int
    ) -> None:
        try:
            if object_path.is_symlink() or not object_path.is_file():
                raise OSError("unsafe existing object")
            if object_path.stat().st_size != size_bytes:
                raise OSError("content-address collision")
            digest = hashlib.sha256()
            with object_path.open("rb") as existing:
                for chunk in iter(lambda: existing.read(self.chunk_bytes), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sha256:
                raise OSError("content-address collision")
        except OSError as exc:
            raise ArtifactIOError() from exc

    def _write_manifest(self, receipt: ArtifactReceipt) -> None:
        manifest_path = self._confined(
            self._manifests, f"{receipt.artifact_id}.json"
        )
        manifest_temp = self._confined(
            self._manifests,
            f".{receipt.artifact_id}.{uuid4()}.tmp",
        )
        try:
            encoded = self._manifest_bytes(receipt)
            with manifest_temp.open("xb", buffering=0) as destination:
                self._write_all(destination, memoryview(encoded))
                destination.flush()
                os.fsync(destination.fileno())
            os.link(manifest_temp, manifest_path, follow_symlinks=False)
            if os.name != "nt":
                manifest_path.chmod(0o444)
            self._fsync_directory(self._manifests)
        except FileExistsError as exc:
            raise ArtifactIOError() from exc
        except OSError as exc:
            raise ArtifactIOError() from exc
        finally:
            self._unlink_temporary(manifest_temp)

    def _confined(self, base: Path, *parts: str) -> Path:
        candidate = base.joinpath(*parts)
        root = self._root.absolute()
        lexical_base = base.absolute()
        lexical_candidate = candidate.absolute()
        if (
            lexical_base != root
            and root not in lexical_base.parents
        ) or (
            lexical_candidate != lexical_base
            and lexical_base not in lexical_candidate.parents
        ):
            raise SourceArtifactError(
                ArtifactErrorCode.INVALID_ARTIFACT_ID,
                "The artifact identity is invalid.",
            )
        current = root
        try:
            relative_parts = lexical_candidate.relative_to(root).parts
        except ValueError as exc:
            raise SourceArtifactError(
                ArtifactErrorCode.INVALID_ARTIFACT_ID,
                "The artifact identity is invalid.",
            ) from exc
        for part in relative_parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactIOError()
        return candidate

    @staticmethod
    def _unlink_temporary(path: Path) -> None:
        try:
            if path.is_symlink():
                raise OSError("refusing symlinked temporary file")
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ArtifactIOError() from exc

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
