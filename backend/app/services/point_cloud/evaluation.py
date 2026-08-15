"""Sealed point-cloud evaluation, metric, and release-receipt boundary.

The bundled corpus deliberately contains only synthetic safety contracts.  It
can prove that mandatory abstentions and protected-region retention were
checked, but it cannot establish measurement accuracy.  A release receipt is
therefore fail-closed until registered survey ground truth, untouched physical
bridge splits, domain-approved thresholds, and an approval receipt are added.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from app.services.conversion.canonical_json import canonical_json_text, canonical_sha256


EVALUATION_VERSION = "bridgeguard-point-cloud-evaluation-v1"
DEFAULT_EVALUATION_ROOT = (
    Path(__file__).resolve().parents[2] / "data" / "point_cloud" / "evaluation"
)
DEFAULT_MANIFEST = DEFAULT_EVALUATION_ROOT / "manifest_v1.json"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_CASES = 10_000
_SHA_LENGTH = 64


class PointCloudEvaluationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PointCloudEvaluationError(
                "evaluation_json_duplicate_key", "Evaluation JSON contains a duplicate key."
            )
        result[key] = value
    return result


def _read_json(path: Path, *, maximum: int = MAX_JSON_BYTES) -> tuple[bytes, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PointCloudEvaluationError(
            "evaluation_artifact_unavailable", "An evaluation artifact is unavailable."
        ) from exc
    if not raw or len(raw) > maximum:
        raise PointCloudEvaluationError(
            "evaluation_artifact_size_invalid", "An evaluation artifact has an invalid size."
        )
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                PointCloudEvaluationError(
                    "evaluation_json_nonfinite", "Evaluation JSON contains a non-finite value."
                )
            ),
        )
    except PointCloudEvaluationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PointCloudEvaluationError(
            "evaluation_json_invalid", "An evaluation artifact is not valid JSON."
        ) from exc
    return raw, value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _text(value: Any, field: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PointCloudEvaluationError(
            "evaluation_field_invalid", f"Evaluation field {field} is invalid."
        )
    return value.strip()


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise PointCloudEvaluationError("evaluation_metric_invalid", f"{field} is invalid.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PointCloudEvaluationError("evaluation_metric_invalid", f"{field} is invalid.") from exc
    if not math.isfinite(result):
        raise PointCloudEvaluationError("evaluation_metric_invalid", f"{field} is invalid.")
    return result


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    physical_structure_id: str
    split: str
    bridge_type: str
    material: str
    under_feature: str
    ground_truth_kind: str
    require_no_numeric_output: bool
    protected_restriction_ids: tuple[str, ...]
    reason: str
    clearance_ground_truth_m: float | None = None


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    policy_id: str
    false_numeric_max: int
    protected_retention_min: float
    domain_accuracy_thresholds: Mapping[str, Any]
    required_empirical_splits: tuple[str, ...]
    required_ground_truth_kind: str
    approval: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "domain_accuracy_thresholds",
            MappingProxyType(dict(self.domain_accuracy_thresholds)),
        )
        object.__setattr__(self, "approval", MappingProxyType(dict(self.approval)))


@dataclass(frozen=True, slots=True)
class EvaluationBundle:
    manifest_id: str
    manifest_sha256: str
    corpus_id: str
    claim_level: str
    cases: tuple[EvaluationCase, ...]
    limitations: tuple[str, ...]
    policy: EvaluationPolicy
    artifact_receipts: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class EvaluationVersions:
    code_revision: str
    parser_version: str
    geometry_version: str
    fusion_version: str
    measurement_version: str
    prompt_version: str
    provider: str
    model: str
    context_builder_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code_revision": _text(self.code_revision, "code_revision", maximum=128),
            "parser_version": _text(self.parser_version, "parser_version"),
            "geometry_version": _text(self.geometry_version, "geometry_version"),
            "fusion_version": _text(self.fusion_version, "fusion_version"),
            "measurement_version": _text(self.measurement_version, "measurement_version"),
            "prompt_version": _text(self.prompt_version, "prompt_version"),
            "provider": _text(self.provider, "provider"),
            "model": _text(self.model, "model"),
            "context_builder_version": _text(
                self.context_builder_version, "context_builder_version"
            ),
        }


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    case_id: str
    numeric_output: bool
    retained_restriction_ids: tuple[str, ...]
    measurable: bool | None = None
    measured_clearance_m: float | None = None
    feature_binding_correct: bool | None = None
    latency_ms: float | None = None
    peak_memory_bytes: int | None = None
    provider_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvaluationObservation":
        allowed = {
            "case_id", "numeric_output", "retained_restriction_ids", "measurable",
            "measured_clearance_m", "feature_binding_correct", "latency_ms",
            "peak_memory_bytes", "provider_calls", "input_tokens", "output_tokens",
        }
        if set(value) - allowed:
            raise PointCloudEvaluationError(
                "evaluation_observation_unknown_field",
                "An evaluation observation contains an unknown field.",
            )
        numeric_output = value.get("numeric_output")
        retained = value.get("retained_restriction_ids", [])
        if type(numeric_output) is not bool or not isinstance(retained, list):
            raise PointCloudEvaluationError(
                "evaluation_observation_invalid", "An evaluation observation is invalid."
            )
        retained_ids = tuple(_text(item, "retained_restriction_id") for item in retained)
        if len(retained_ids) != len(set(retained_ids)):
            raise PointCloudEvaluationError(
                "evaluation_observation_invalid", "Restriction IDs are duplicated."
            )
        measured = value.get("measured_clearance_m")
        latency = value.get("latency_ms")
        measurable = value.get("measurable")
        binding = value.get("feature_binding_correct")
        if measurable is not None and type(measurable) is not bool:
            raise PointCloudEvaluationError("evaluation_observation_invalid", "measurable is invalid.")
        if binding is not None and type(binding) is not bool:
            raise PointCloudEvaluationError("evaluation_observation_invalid", "feature binding is invalid.")
        integer_fields: dict[str, int | None] = {}
        for field in ("peak_memory_bytes", "provider_calls", "input_tokens", "output_tokens"):
            raw = value.get(field)
            if raw is not None and (type(raw) is not int or raw < 0):
                raise PointCloudEvaluationError("evaluation_observation_invalid", f"{field} is invalid.")
            integer_fields[field] = raw
        return cls(
            case_id=_text(value.get("case_id"), "case_id"),
            numeric_output=numeric_output,
            retained_restriction_ids=retained_ids,
            measurable=measurable,
            measured_clearance_m=(None if measured is None else _finite(measured, "measured_clearance_m")),
            feature_binding_correct=binding,
            latency_ms=(None if latency is None else _finite(latency, "latency_ms")),
            **integer_fields,
        )


@dataclass(frozen=True, slots=True)
class EvaluationReceipt:
    receipt_id: str
    evaluated_at: str
    evaluation_version: str
    manifest_id: str
    manifest_sha256: str
    policy_id: str
    versions: Mapping[str, str]
    observation_sha256: str
    metrics: Mapping[str, Any]
    thresholds: Mapping[str, Any]
    blockers: tuple[str, ...]
    safety_contract_passed: bool
    empirical_accuracy_passed: bool
    release_passed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))

    def to_dict(self, *, include_receipt_id: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "evaluated_at": self.evaluated_at,
            "evaluation_version": self.evaluation_version,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "policy_id": self.policy_id,
            "versions": dict(self.versions),
            "observation_sha256": self.observation_sha256,
            "metrics": dict(self.metrics),
            "thresholds": dict(self.thresholds),
            "blockers": list(self.blockers),
            "safety_contract_passed": self.safety_contract_passed,
            "empirical_accuracy_passed": self.empirical_accuracy_passed,
            "release_passed": self.release_passed,
            "official_validation": False,
            "submission_ready": False,
        }
        if include_receipt_id:
            result["receipt_id"] = self.receipt_id
        return result

    def canonical_json(self) -> str:
        return canonical_json_text(self.to_dict())


def active_evaluation_versions() -> EvaluationVersions:
    """Bind a receipt check to every runtime version that can change output."""

    from app.services.point_cloud.clearance import CLEARANCE_PROFILE_VERSION
    from app.services.point_cloud.fusion import FUSION_PROFILE_VERSION
    from app.services.point_cloud.geometry import GEOMETRY_PROFILE_VERSION
    from app.services.point_cloud.knowledge_pack import KNOWLEDGE_PACK_VERSION
    from app.services.point_cloud.source_reader import E57_PARSER_VERSION
    from app.services.point_cloud.vlm_protocol import VLM_PROTOCOL_VERSION

    return EvaluationVersions(
        code_revision=os.getenv("BRIDGEGUARD_APP_REVISION", "working-tree-uncommitted"),
        parser_version=E57_PARSER_VERSION,
        geometry_version=GEOMETRY_PROFILE_VERSION,
        fusion_version=FUSION_PROFILE_VERSION,
        measurement_version=CLEARANCE_PROFILE_VERSION,
        prompt_version=VLM_PROTOCOL_VERSION,
        provider="cerebras",
        model=(
            os.getenv("CEREBRAS_VISION_MODEL", "").strip()
            or os.getenv("CEREBRAS_MODEL", "").strip()
            or "gemma-4-31b"
        ),
        context_builder_version=KNOWLEDGE_PACK_VERSION,
    )


def _parse_case(value: Any) -> EvaluationCase:
    if not isinstance(value, Mapping):
        raise PointCloudEvaluationError("evaluation_case_invalid", "An evaluation case is invalid.")
    protected = value.get("protected_restriction_ids")
    no_numeric = value.get("require_no_numeric_output")
    if not isinstance(protected, list) or type(no_numeric) is not bool:
        raise PointCloudEvaluationError("evaluation_case_invalid", "An evaluation case is invalid.")
    protected_ids = tuple(_text(item, "protected_restriction_id") for item in protected)
    if len(protected_ids) != len(set(protected_ids)):
        raise PointCloudEvaluationError("evaluation_case_invalid", "Protected IDs are duplicated.")
    ground_truth = value.get("clearance_ground_truth_m")
    return EvaluationCase(
        case_id=_text(value.get("case_id"), "case_id"),
        physical_structure_id=_text(value.get("physical_structure_id"), "physical_structure_id"),
        split=_text(value.get("split"), "split"),
        bridge_type=_text(value.get("bridge_type"), "bridge_type"),
        material=_text(value.get("material"), "material"),
        under_feature=_text(value.get("under_feature"), "under_feature"),
        ground_truth_kind=_text(value.get("ground_truth_kind"), "ground_truth_kind"),
        require_no_numeric_output=no_numeric,
        protected_restriction_ids=protected_ids,
        reason=_text(value.get("reason"), "reason", maximum=1000),
        clearance_ground_truth_m=(None if ground_truth is None else _finite(ground_truth, "clearance_ground_truth_m")),
    )


def load_evaluation_bundle(manifest_path: str | Path = DEFAULT_MANIFEST) -> EvaluationBundle:
    manifest_file = Path(manifest_path).resolve()
    manifest_raw, manifest = _read_json(manifest_file, maximum=256 * 1024)
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != "bridgeguard-point-cloud-evaluation-manifest-v1":
        raise PointCloudEvaluationError("evaluation_manifest_invalid", "Evaluation manifest is invalid.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise PointCloudEvaluationError("evaluation_manifest_invalid", "Evaluation manifest is incomplete.")
    root = manifest_file.parent
    loaded: dict[str, Any] = {}
    receipts: list[Mapping[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise PointCloudEvaluationError("evaluation_manifest_invalid", "Evaluation artifact receipt is invalid.")
        filename = _text(artifact.get("filename"), "filename")
        if Path(filename).name != filename:
            raise PointCloudEvaluationError("evaluation_manifest_invalid", "Evaluation artifact path is invalid.")
        expected_sha = _text(artifact.get("sha256"), "sha256", maximum=_SHA_LENGTH)
        expected_size = artifact.get("byte_size")
        if len(expected_sha) != _SHA_LENGTH or type(expected_size) is not int or expected_size < 1:
            raise PointCloudEvaluationError("evaluation_manifest_invalid", "Evaluation artifact receipt is invalid.")
        raw, value = _read_json(root / filename)
        if len(raw) != expected_size or _sha256(raw) != expected_sha:
            raise PointCloudEvaluationError("evaluation_artifact_hash_mismatch", "Evaluation artifact integrity failed.")
        artifact_id = _text(artifact.get("artifact_id"), "artifact_id")
        loaded[artifact_id] = value
        receipts.append(MappingProxyType(dict(artifact)))
    corpus = loaded.get("safety_contract_v1")
    policy_value = loaded.get("release_policy_v1")
    if not isinstance(corpus, Mapping) or not isinstance(policy_value, Mapping):
        raise PointCloudEvaluationError("evaluation_manifest_invalid", "Evaluation roles are missing.")
    raw_cases = corpus.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > MAX_CASES:
        raise PointCloudEvaluationError("evaluation_corpus_invalid", "Evaluation corpus is invalid.")
    cases = tuple(_parse_case(value) for value in raw_cases)
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise PointCloudEvaluationError("evaluation_corpus_invalid", "Evaluation case IDs are duplicated.")
    structure_splits: dict[str, str] = {}
    for case in cases:
        prior = structure_splits.setdefault(case.physical_structure_id, case.split)
        if prior != case.split:
            raise PointCloudEvaluationError(
                "evaluation_split_leakage", "One physical structure appears in multiple splits."
            )
    safety = policy_value.get("safety_thresholds")
    domain = policy_value.get("domain_accuracy_thresholds")
    approval = policy_value.get("approval")
    splits = policy_value.get("required_empirical_splits")
    if not all(isinstance(item, Mapping) for item in (safety, domain, approval)) or not isinstance(splits, list):
        raise PointCloudEvaluationError("evaluation_policy_invalid", "Evaluation policy is invalid.")
    false_max = safety.get("false_numeric_mandatory_abstention_max")
    retention_min = safety.get("protected_restriction_retention_min")
    if type(false_max) is not int or false_max < 0:
        raise PointCloudEvaluationError("evaluation_policy_invalid", "Safety threshold is invalid.")
    retention_value = _finite(retention_min, "protected_restriction_retention_min")
    if not 0.0 <= retention_value <= 1.0:
        raise PointCloudEvaluationError("evaluation_policy_invalid", "Safety threshold is invalid.")
    limitations = corpus.get("limitations")
    if not isinstance(limitations, list):
        raise PointCloudEvaluationError("evaluation_corpus_invalid", "Evaluation limitations are missing.")
    policy = EvaluationPolicy(
        policy_id=_text(policy_value.get("policy_id"), "policy_id"),
        false_numeric_max=false_max,
        protected_retention_min=retention_value,
        domain_accuracy_thresholds=domain,
        required_empirical_splits=tuple(_text(value, "required_split") for value in splits),
        required_ground_truth_kind=_text(policy_value.get("required_ground_truth_kind"), "required_ground_truth_kind"),
        approval=approval,
    )
    return EvaluationBundle(
        manifest_id=_text(manifest.get("manifest_id"), "manifest_id"),
        manifest_sha256=_sha256(manifest_raw),
        corpus_id=_text(corpus.get("corpus_id"), "corpus_id"),
        claim_level=_text(corpus.get("claim_level"), "claim_level"),
        cases=cases,
        limitations=tuple(_text(value, "limitation", maximum=1000) for value in limitations),
        policy=policy,
        artifact_receipts=tuple(receipts),
    )


def load_observations(path: str | Path) -> tuple[EvaluationObservation, ...]:
    _raw, value = _read_json(Path(path).resolve())
    if not isinstance(value, Mapping) or value.get("schema_version") != "bridgeguard-point-cloud-observations-v1":
        raise PointCloudEvaluationError("evaluation_observations_invalid", "Observation file is invalid.")
    observations = value.get("observations")
    if not isinstance(observations, list) or len(observations) > MAX_CASES:
        raise PointCloudEvaluationError("evaluation_observations_invalid", "Observation file is invalid.")
    parsed = tuple(EvaluationObservation.from_mapping(item) for item in observations if isinstance(item, Mapping))
    if len(parsed) != len(observations) or len({item.case_id for item in parsed}) != len(parsed):
        raise PointCloudEvaluationError("evaluation_observations_invalid", "Observations are missing or duplicated.")
    return parsed


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def evaluate_point_cloud_observations(
    bundle: EvaluationBundle,
    observations: Sequence[EvaluationObservation],
    versions: EvaluationVersions,
    *,
    evaluated_at: str | None = None,
) -> EvaluationReceipt:
    by_id = {item.case_id: item for item in observations}
    expected_ids = {case.case_id for case in bundle.cases}
    if set(by_id) != expected_ids:
        raise PointCloudEvaluationError(
            "evaluation_observation_coverage_mismatch",
            "Observations must cover every sealed case exactly once.",
        )
    false_numeric = 0
    protected_total = 0
    protected_retained = 0
    errors: list[float] = []
    binding_values: list[bool] = []
    measurable_ground_truth_cases = 0
    false_measurable = 0
    latencies: list[float] = []
    provider_calls = 0
    input_tokens = 0
    output_tokens = 0
    for case in bundle.cases:
        observation = by_id[case.case_id]
        if case.require_no_numeric_output and observation.numeric_output:
            false_numeric += 1
        if case.require_no_numeric_output and observation.measurable is True:
            false_measurable += 1
        retained = set(observation.retained_restriction_ids)
        for restriction in case.protected_restriction_ids:
            protected_total += 1
            protected_retained += int(restriction in retained)
        if case.clearance_ground_truth_m is not None:
            measurable_ground_truth_cases += 1
            if observation.measured_clearance_m is not None:
                errors.append(observation.measured_clearance_m - case.clearance_ground_truth_m)
        if observation.feature_binding_correct is not None:
            binding_values.append(observation.feature_binding_correct)
        if observation.latency_ms is not None:
            latencies.append(observation.latency_ms)
        provider_calls += observation.provider_calls or 0
        input_tokens += observation.input_tokens or 0
        output_tokens += observation.output_tokens or 0
    absolute_errors = [abs(value) for value in errors]
    mandatory_abstention_count = sum(
        case.require_no_numeric_output for case in bundle.cases
    )
    protected_rate = 1.0 if protected_total == 0 else protected_retained / protected_total
    metrics: dict[str, Any] = {
        "case_count": len(bundle.cases),
        "mandatory_abstention_count": mandatory_abstention_count,
        "false_numeric_mandatory_abstention_count": false_numeric,
        "false_measurable_count": false_measurable,
        "false_measurable_rate": (
            0.0
            if mandatory_abstention_count == 0
            else false_measurable / mandatory_abstention_count
        ),
        "protected_restriction_count": protected_total,
        "protected_restriction_retained_count": protected_retained,
        "protected_restriction_retention": protected_rate,
        "empirical_clearance_case_count": measurable_ground_truth_cases,
        "clearance_observation_count": len(errors),
        "clearance_signed_bias_m": (None if not errors else sum(errors) / len(errors)),
        "clearance_mae_m": (None if not absolute_errors else sum(absolute_errors) / len(absolute_errors)),
        "clearance_rmse_m": (None if not errors else math.sqrt(sum(value * value for value in errors) / len(errors))),
        "clearance_p95_absolute_error_m": _p95(absolute_errors),
        "feature_binding_observation_count": len(binding_values),
        "feature_binding_accuracy": (None if not binding_values else sum(binding_values) / len(binding_values)),
        # Component-class confusion observations are intentionally not inferred
        # from a VLM label. A future empirical corpus must provide a reviewed
        # class-confusion receipt before this metric can become numeric.
        "per_class_f1_min": None,
        "latency_observation_count": len(latencies),
        "latency_p95_ms": _p95(latencies),
        "provider_calls": provider_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    policy = bundle.policy
    safety_passed = (
        false_numeric <= policy.false_numeric_max
        and protected_rate >= policy.protected_retention_min
    )
    blockers: list[str] = []
    splits = {case.split for case in bundle.cases}
    if not set(policy.required_empirical_splits).issubset(splits):
        blockers.append("empirical_physical_bridge_splits_missing")
    if not any(case.ground_truth_kind == policy.required_ground_truth_kind for case in bundle.cases):
        blockers.append("registered_survey_ground_truth_missing")
    required_thresholds = (
        "clearance_mae_max_m",
        "clearance_p95_absolute_error_max_m",
        "feature_binding_accuracy_min",
        "false_measurable_rate_max",
        "per_class_f1_min",
    )
    thresholds = policy.domain_accuracy_thresholds
    if any(thresholds.get(key) is None for key in required_thresholds):
        blockers.append("domain_accuracy_thresholds_unapproved")
    if policy.approval.get("status") != "approved" or not policy.approval.get("evidence_receipt"):
        blockers.append("domain_owner_approval_missing")
    if measurable_ground_truth_cases == 0 or len(errors) != measurable_ground_truth_cases:
        blockers.append("empirical_clearance_observations_missing")
    if all(thresholds.get(key) is not None for key in required_thresholds):
        try:
            mae_max = _finite(thresholds["clearance_mae_max_m"], "clearance_mae_max_m")
            p95_max = _finite(
                thresholds["clearance_p95_absolute_error_max_m"],
                "clearance_p95_absolute_error_max_m",
            )
            binding_min = _finite(
                thresholds["feature_binding_accuracy_min"],
                "feature_binding_accuracy_min",
            )
            false_measurable_max = _finite(
                thresholds["false_measurable_rate_max"],
                "false_measurable_rate_max",
            )
            class_f1_min = _finite(thresholds["per_class_f1_min"], "per_class_f1_min")
        except PointCloudEvaluationError:
            blockers.append("domain_accuracy_thresholds_invalid")
        else:
            if min(mae_max, p95_max) < 0 or not all(
                0.0 <= value <= 1.0
                for value in (binding_min, false_measurable_max, class_f1_min)
            ):
                blockers.append("domain_accuracy_thresholds_invalid")
            if metrics["clearance_mae_m"] is None or metrics["clearance_p95_absolute_error_m"] is None:
                blockers.append("empirical_clearance_observations_missing")
            elif (
                metrics["clearance_mae_m"] > mae_max
                or metrics["clearance_p95_absolute_error_m"] > p95_max
            ):
                blockers.append("clearance_accuracy_threshold_failed")
            if metrics["feature_binding_accuracy"] is None:
                blockers.append("feature_binding_observations_missing")
            elif metrics["feature_binding_accuracy"] < binding_min:
                blockers.append("feature_binding_threshold_failed")
            if metrics["false_measurable_rate"] > false_measurable_max:
                blockers.append("false_measurable_threshold_failed")
            if metrics["per_class_f1_min"] is None:
                blockers.append("component_class_f1_observations_missing")
            elif metrics["per_class_f1_min"] < class_f1_min:
                blockers.append("component_class_f1_threshold_failed")
    empirical_passed = not blockers
    if not safety_passed:
        blockers.append("mandatory_safety_threshold_failed")
    observation_payload = [
        {
            "case_id": item.case_id,
            "numeric_output": item.numeric_output,
            "retained_restriction_ids": list(item.retained_restriction_ids),
            "measurable": item.measurable,
            "measured_clearance_m": item.measured_clearance_m,
            "feature_binding_correct": item.feature_binding_correct,
            "latency_ms": item.latency_ms,
            "peak_memory_bytes": item.peak_memory_bytes,
            "provider_calls": item.provider_calls,
            "input_tokens": item.input_tokens,
            "output_tokens": item.output_tokens,
        }
        for item in sorted(observations, key=lambda value: value.case_id)
    ]
    now = evaluated_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    threshold_payload = {
        "false_numeric_mandatory_abstention_max": policy.false_numeric_max,
        "protected_restriction_retention_min": policy.protected_retention_min,
        "domain_accuracy_thresholds": dict(policy.domain_accuracy_thresholds),
        "approval_status": policy.approval.get("status"),
    }
    base = {
        "evaluated_at": now,
        "evaluation_version": EVALUATION_VERSION,
        "manifest_id": bundle.manifest_id,
        "manifest_sha256": bundle.manifest_sha256,
        "policy_id": policy.policy_id,
        "versions": versions.to_dict(),
        "observation_sha256": canonical_sha256(observation_payload),
        "metrics": metrics,
        "thresholds": threshold_payload,
        "blockers": sorted(set(blockers)),
        "safety_contract_passed": safety_passed,
        "empirical_accuracy_passed": empirical_passed,
        "release_passed": safety_passed and empirical_passed,
        "official_validation": False,
        "submission_ready": False,
    }
    return EvaluationReceipt(
        receipt_id=canonical_sha256(base),
        evaluated_at=now,
        evaluation_version=EVALUATION_VERSION,
        manifest_id=bundle.manifest_id,
        manifest_sha256=bundle.manifest_sha256,
        policy_id=policy.policy_id,
        versions=versions.to_dict(),
        observation_sha256=base["observation_sha256"],
        metrics=metrics,
        thresholds=threshold_payload,
        blockers=tuple(base["blockers"]),
        safety_contract_passed=safety_passed,
        empirical_accuracy_passed=empirical_passed,
        release_passed=base["release_passed"],
    )


def receipt_matches_versions(
    receipt: EvaluationReceipt, bundle: EvaluationBundle, versions: EvaluationVersions
) -> bool:
    return bool(
        receipt.manifest_sha256 == bundle.manifest_sha256
        and dict(receipt.versions) == versions.to_dict()
        and receipt.evaluation_version == EVALUATION_VERSION
    )


def load_evaluation_receipt(path: str | Path) -> EvaluationReceipt:
    _raw, value = _read_json(Path(path).resolve(), maximum=2 * 1024 * 1024)
    if not isinstance(value, Mapping):
        raise PointCloudEvaluationError("evaluation_receipt_invalid", "Evaluation receipt is invalid.")
    try:
        expected_keys = {
            "receipt_id", "evaluated_at", "evaluation_version", "manifest_id",
            "manifest_sha256", "policy_id", "versions", "observation_sha256",
            "metrics", "thresholds", "blockers", "safety_contract_passed",
            "empirical_accuracy_passed", "release_passed", "official_validation",
            "submission_ready",
        }
        if set(value) != expected_keys:
            raise ValueError
        if value.get("official_validation") is not False or value.get("submission_ready") is not False:
            raise ValueError
        versions = value["versions"]
        metrics = value["metrics"]
        thresholds = value["thresholds"]
        blockers = value["blockers"]
        if not isinstance(versions, Mapping) or not isinstance(metrics, Mapping) or not isinstance(thresholds, Mapping) or not isinstance(blockers, list):
            raise ValueError
        receipt = EvaluationReceipt(
            receipt_id=_text(value.get("receipt_id"), "receipt_id", maximum=64),
            evaluated_at=_text(value.get("evaluated_at"), "evaluated_at"),
            evaluation_version=_text(value.get("evaluation_version"), "evaluation_version"),
            manifest_id=_text(value.get("manifest_id"), "manifest_id"),
            manifest_sha256=_text(value.get("manifest_sha256"), "manifest_sha256", maximum=64),
            policy_id=_text(value.get("policy_id"), "policy_id"),
            versions={str(key): _text(item, f"versions.{key}") for key, item in versions.items()},
            observation_sha256=_text(value.get("observation_sha256"), "observation_sha256", maximum=64),
            metrics=dict(metrics),
            thresholds=dict(thresholds),
            blockers=tuple(_text(item, "blocker") for item in blockers),
            safety_contract_passed=value["safety_contract_passed"],
            empirical_accuracy_passed=value["empirical_accuracy_passed"],
            release_passed=value["release_passed"],
        )
        if any(
            type(item) is not bool
            for item in (
                receipt.safety_contract_passed,
                receipt.empirical_accuracy_passed,
                receipt.release_passed,
            )
        ):
            raise ValueError
        if (
            not all(
                re.fullmatch(r"[0-9a-f]{64}", digest)
                for digest in (
                    receipt.receipt_id,
                    receipt.manifest_sha256,
                    receipt.observation_sha256,
                )
            )
            or canonical_sha256(receipt.to_dict(include_receipt_id=False)) != receipt.receipt_id
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, PointCloudEvaluationError) as exc:
        if isinstance(exc, PointCloudEvaluationError):
            raise
        raise PointCloudEvaluationError(
            "evaluation_receipt_invalid", "Evaluation receipt is invalid."
        ) from exc
    return receipt


def require_current_release_receipt(
    path: str | Path,
    *,
    bundle: EvaluationBundle | None = None,
    versions: EvaluationVersions | None = None,
) -> EvaluationReceipt:
    active_bundle = bundle or load_evaluation_bundle()
    active_versions = versions or active_evaluation_versions()
    receipt = load_evaluation_receipt(path)
    if not receipt_matches_versions(receipt, active_bundle, active_versions):
        raise PointCloudEvaluationError(
            "point_cloud_evaluation_receipt_stale",
            "The point-cloud evaluation receipt does not match the active runtime.",
        )
    if not receipt.release_passed or receipt.blockers:
        raise PointCloudEvaluationError(
            "point_cloud_evaluation_release_blocked",
            "The point-cloud scientific release gate has not passed.",
        )
    return receipt


__all__ = [
    "DEFAULT_MANIFEST",
    "EVALUATION_VERSION",
    "EvaluationBundle",
    "EvaluationCase",
    "EvaluationObservation",
    "EvaluationPolicy",
    "EvaluationReceipt",
    "EvaluationVersions",
    "PointCloudEvaluationError",
    "active_evaluation_versions",
    "evaluate_point_cloud_observations",
    "load_evaluation_bundle",
    "load_observations",
    "load_evaluation_receipt",
    "require_current_release_receipt",
    "receipt_matches_versions",
]
