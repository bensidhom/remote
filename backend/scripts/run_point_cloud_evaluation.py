"""Run the sealed point-cloud evaluation without network or database access."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from app.services.point_cloud.clearance import CLEARANCE_PROFILE_VERSION
from app.services.point_cloud.evaluation import (
    DEFAULT_MANIFEST,
    EvaluationVersions,
    evaluate_point_cloud_observations,
    load_evaluation_bundle,
    load_observations,
)
from app.services.point_cloud.fusion import FUSION_PROFILE_VERSION
from app.services.point_cloud.geometry import GEOMETRY_PROFILE_VERSION
from app.services.point_cloud.knowledge_pack import KNOWLEDGE_PACK_VERSION
from app.services.point_cloud.source_reader import E57_PARSER_VERSION
from app.services.point_cloud.vlm_protocol import VLM_PROTOCOL_VERSION


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--observations")
    parser.add_argument("--output")
    parser.add_argument("--code-revision", default="working-tree-uncommitted")
    parser.add_argument("--provider", default="unconfigured")
    parser.add_argument("--model", default="unconfigured")
    parser.add_argument("--prompt-version", default=VLM_PROTOCOL_VERSION)
    parser.add_argument("--evaluated-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle = load_evaluation_bundle(args.manifest)
    if not args.observations:
        payload = {
            "status": "observations_required",
            "manifest_id": bundle.manifest_id,
            "manifest_sha256": bundle.manifest_sha256,
            "corpus_id": bundle.corpus_id,
            "claim_level": bundle.claim_level,
            "case_count": len(bundle.cases),
            "limitations": list(bundle.limitations),
            "release_passed": False,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 2
    observations = load_observations(args.observations)
    versions = EvaluationVersions(
        code_revision=args.code_revision,
        parser_version=E57_PARSER_VERSION,
        geometry_version=GEOMETRY_PROFILE_VERSION,
        fusion_version=FUSION_PROFILE_VERSION,
        measurement_version=CLEARANCE_PROFILE_VERSION,
        prompt_version=args.prompt_version,
        provider=args.provider,
        model=args.model,
        context_builder_version=KNOWLEDGE_PACK_VERSION,
    )
    receipt = evaluate_point_cloud_observations(
        bundle,
        observations,
        versions,
        evaluated_at=args.evaluated_at,
    )
    raw = (receipt.canonical_json() + "\n").encode("utf-8")
    if args.output:
        target = Path(args.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    sys.stdout.buffer.write(raw)
    return 0 if receipt.release_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
