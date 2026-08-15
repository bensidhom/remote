"""Stage an FHWA standards artifact for explicit human review.

This maintainer-only command never changes the executable registry and never
writes directly into ``backend/app/data/fhwa``.  It downloads one registered
HTTPS resource into a private candidate directory, computes its SHA-256, and
prints metadata that a reviewer can use in a later, ordinary code review.

Example::

    python backend/scripts/update_fhwa_artifacts.py \
        --artifact snbi_march_2022

Network access is intentionally limited to the URL already present in the
local registry.  Runtime application code does not import or invoke this
script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import BinaryIO, Callable
from urllib.request import Request, urlopen
import zipfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.standards.fhwa_registry import (  # noqa: E402
    ArtifactState,
    DEFAULT_REGISTRY_PATH,
    load_rule_bundle_registry,
)


DEFAULT_CANDIDATE_ROOT = BACKEND_ROOT / ".fhwa-candidates"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class CandidateDownloadError(RuntimeError):
    """Raised when a candidate cannot be staged safely."""


_XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _verify_candidate_format(path: Path, media_type: str | None) -> None:
    """Reject a successful HTTP response whose bytes are not the registered type."""

    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    try:
        with path.open("rb") as source:
            signature = source.read(8)
    except OSError as exc:
        raise CandidateDownloadError("could not verify candidate media type") from exc

    if normalized == _XLSX_MEDIA_TYPE:
        if not signature.startswith(b"PK\x03\x04") or not zipfile.is_zipfile(path):
            raise CandidateDownloadError(
                "artifact bytes do not match the registered XLSX media type"
            )
        try:
            with zipfile.ZipFile(path) as archive:
                members = set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise CandidateDownloadError("artifact is not a readable XLSX package") from exc
        if "[Content_Types].xml" not in members or "xl/workbook.xml" not in members:
            raise CandidateDownloadError("artifact is not a complete XLSX workbook")
    elif normalized == "application/pdf":
        if not signature.startswith(b"%PDF-"):
            raise CandidateDownloadError(
                "artifact bytes do not match the registered PDF media type"
            )
    elif normalized == "application/json":
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CandidateDownloadError(
                "artifact bytes do not match the registered JSON media type"
            ) from exc
    elif normalized == "text/html":
        if not signature.lstrip().lower().startswith((b"<!doctype", b"<html")):
            raise CandidateDownloadError(
                "artifact bytes do not match the registered HTML media type"
            )


def _copy_bounded(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> tuple[str, int]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if chunk_size <= 0 or chunk_size > max_bytes:
        raise ValueError("chunk_size must be positive and no larger than max_bytes")

    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise CandidateDownloadError("artifact response returned non-binary data")
        total += len(chunk)
        if total > max_bytes:
            raise CandidateDownloadError(
                f"artifact exceeds the configured {max_bytes}-byte limit"
            )
        digest.update(chunk)
        destination.write(chunk)
    if total == 0:
        raise CandidateDownloadError("artifact response was empty")
    return digest.hexdigest(), total


def stage_candidate(
    artifact_id: str,
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    candidate_root: Path = DEFAULT_CANDIDATE_ROOT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = 30.0,
    opener: Callable[..., BinaryIO] = urlopen,
) -> dict[str, object]:
    """Download one registry URL to a non-authoritative candidate directory."""

    if not _SAFE_ID.fullmatch(artifact_id):
        raise CandidateDownloadError("invalid artifact identifier")
    registry = load_rule_bundle_registry(registry_path)
    artifact = registry.artifact(artifact_id)
    if artifact.state is ArtifactState.UNAVAILABLE or not artifact.source_url:
        raise CandidateDownloadError(f"{artifact_id} has no downloadable source URL")

    root = Path(candidate_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifact_directory = (root / artifact_id).resolve()
    if artifact_directory.parent != root:
        raise CandidateDownloadError("candidate path escaped its configured root")
    # Passing a POSIX-only restrictive ``mode`` to ``mkdir`` can create an
    # unusable ACL in some sandboxed Windows environments. Deployment-level
    # permissions belong on the configured candidate root; use ordinary
    # inherited permissions for this child directory on every platform.
    artifact_directory.mkdir(parents=False, exist_ok=True)

    request = Request(
        artifact.source_url,
        headers={"User-Agent": "BridgeGuard-FHWA-Artifact-Review/1.0"},
        method="GET",
    )
    temporary_path: Path | None = None
    try:
        response = opener(request, timeout=timeout_seconds)
        with response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    announced_size = int(content_length)
                except ValueError as exc:
                    raise CandidateDownloadError(
                        "artifact response has an invalid Content-Length"
                    ) from exc
                if announced_size > max_bytes:
                    raise CandidateDownloadError(
                        f"artifact announces {announced_size} bytes, above the limit"
                    )

            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix="candidate-",
                suffix=".part",
                dir=artifact_directory,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                sha256, size = _copy_bounded(
                    response, temporary, max_bytes=max_bytes
                )
                temporary.flush()
                os.fsync(temporary.fileno())

        assert temporary_path is not None
        _verify_candidate_format(temporary_path, artifact.media_type)

        final_path = artifact_directory / f"{sha256}.candidate"
        if final_path.exists():
            if hashlib.sha256(final_path.read_bytes()).hexdigest() != sha256:
                raise CandidateDownloadError("existing candidate failed hash validation")
            temporary_path.unlink(missing_ok=True)
        else:
            os.replace(temporary_path, final_path)
        temporary_path = None

        result = {
            "artifact_id": artifact.id,
            "source_url": artifact.source_url,
            "candidate_path": final_path.relative_to(root).as_posix(),
            "sha256": sha256,
            "size_bytes": size,
            "registered_state": artifact.state.value,
            "registered_sha256": artifact.sha256,
            "registry_modified": False,
            "review_required": True,
        }
        manifest_path = artifact_directory / f"{sha256}.json"
        manifest_temp = artifact_directory / f".{sha256}.{os.getpid()}.tmp"
        manifest_temp.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(manifest_temp, manifest_path)
        return result
    except CandidateDownloadError:
        raise
    except Exception as exc:
        raise CandidateDownloadError(f"could not stage {artifact_id}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, help="registered artifact id")
    parser.add_argument(
        "--registry", type=Path, default=DEFAULT_REGISTRY_PATH, help="local registry"
    )
    parser.add_argument(
        "--candidate-root",
        type=Path,
        default=DEFAULT_CANDIDATE_ROOT,
        help="non-authoritative review directory",
    )
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = stage_candidate(
            args.artifact,
            registry_path=args.registry,
            candidate_root=args.candidate_root,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout,
        )
    except CandidateDownloadError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": "staged", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
