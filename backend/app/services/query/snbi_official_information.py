"""Offline, source-attributed SNBI field information for Bridgette.

Four primary-source artifacts used by the bundled enrichment are locally
vendored and SHA-256 verified.  The remaining Q&A page is a remote observation.
Source-byte identity is independent from the derived summaries and rule
implementation: this module exposes interpretation aids only and cannot enable
an official validation profile, execute FHWA rules, certify a transition, or
prove acceptance by FHWA/NBTIS.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterable, Mapping


INFORMATION_VERSION = "bridgeguard-snbi-informational-enrichment-v1"
INFORMATION_AUTHORITY = "informational_source_attributed_not_compliance_verified"
REMOTE_OBSERVATION_STATE = "remote_unverified_observation"
VENDORED_VERIFIED_SOURCE_STATE = "vendored_verified_source_bytes"
VALIDATION_INFORMATION_STATUS = "informational_verified_source_bytes_not_executed"
MAX_PROMPT_FIELDS = 18
MAX_PROMPT_VALIDATION_RULES_PER_FIELD = 12
MAX_PROMPT_CODE_MEANINGS_PER_FIELD = 80

_APP_ROOT = Path(__file__).resolve().parents[2]
_INFORMATION_PATH = (
    _APP_ROOT / "data" / "fhwa" / "derived" / "snbi_field_information_v1.json"
)
_REGISTRY_PATH = _APP_ROOT / "data" / "fhwa" / "registry.json"
_TAG_RE = re.compile(r"^B[A-Z]{1,3}\d{2}$")


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _string(value, field_name=field_name)


def _strings(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return tuple(_string(item, field_name=field_name) for item in value)


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


@dataclass(frozen=True, slots=True)
class InformationSource:
    source_id: str
    title: str
    authority: str
    artifact_state: str
    legal_status: str
    source_url: str
    observed_on: str
    observed_sha256: str | None
    verification_boundary: str

    def __post_init__(self) -> None:
        if self.artifact_state not in {
            REMOTE_OBSERVATION_STATE,
            VENDORED_VERIFIED_SOURCE_STATE,
        }:
            raise ValueError("unsupported informational source trust state")
        if self.authority != "FHWA" or not self.source_url.startswith("https://www.fhwa.dot.gov/"):
            raise ValueError("informational sources must be official FHWA primary-source URLs")
        if self.observed_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.observed_sha256
        ):
            raise ValueError("observed_sha256 must be a lowercase SHA-256 digest")
        if (
            self.artifact_state == VENDORED_VERIFIED_SOURCE_STATE
            and self.observed_sha256 is None
        ):
            raise ValueError("vendored verified sources require a SHA-256 digest")

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "InformationSource":
        return cls(
            source_id=_string(raw.get("source_id"), field_name="source_id"),
            title=_string(raw.get("title"), field_name="source title"),
            authority=_string(raw.get("authority"), field_name="source authority"),
            artifact_state=_string(raw.get("artifact_state"), field_name="artifact_state"),
            legal_status=_string(raw.get("legal_status"), field_name="legal_status"),
            source_url=_string(raw.get("source_url"), field_name="source_url"),
            observed_on=_string(raw.get("observed_on"), field_name="observed_on"),
            observed_sha256=_optional_string(
                raw.get("observed_sha256"), field_name="observed_sha256"
            ),
            verification_boundary=_string(
                raw.get("verification_boundary"), field_name="verification_boundary"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "authority": self.authority,
            "artifact_state": self.artifact_state,
            "legal_status": self.legal_status,
            "source_url": self.source_url,
            "observed_on": self.observed_on,
            "observed_sha256": self.observed_sha256,
            "verification_boundary": self.verification_boundary,
        }


@dataclass(frozen=True, slots=True)
class TransitionInformation:
    record_kind: str
    category: str
    legacy_item_id: str | None
    legacy_item_name: str | None
    legacy_format: str | None
    rule_summary: str | None
    additional_note: str | None
    source_id: str
    source_locator: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "TransitionInformation":
        record_kind = _string(raw.get("record_kind"), field_name="record_kind")
        category = _string(raw.get("category"), field_name="transition category")
        if record_kind not in {"over", "under"}:
            raise ValueError(f"unsupported transition record kind: {record_kind}")
        if category not in {"yes", "partial", "no", "calculate", "unavailable"}:
            raise ValueError(f"unsupported transition category: {category}")
        return cls(
            record_kind=record_kind,
            category=category,
            legacy_item_id=_optional_string(raw.get("legacy_item_id"), field_name="legacy_item_id"),
            legacy_item_name=_optional_string(raw.get("legacy_item_name"), field_name="legacy_item_name"),
            legacy_format=_optional_string(raw.get("legacy_format"), field_name="legacy_format"),
            rule_summary=_optional_string(raw.get("rule_summary"), field_name="rule_summary"),
            additional_note=_optional_string(raw.get("additional_note"), field_name="additional_note"),
            source_id=_string(raw.get("source_id"), field_name="transition source_id"),
            source_locator=_string(raw.get("source_locator"), field_name="transition source_locator"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "record_kind": self.record_kind,
            "category": self.category,
            "legacy_item_id": self.legacy_item_id,
            "legacy_item_name": self.legacy_item_name,
            "legacy_format": self.legacy_format,
            "rule_summary": self.rule_summary,
            "additional_note": self.additional_note,
            "source_id": self.source_id,
            "source_locator": self.source_locator,
        }


@dataclass(frozen=True, slots=True)
class ValidationInformation:
    severity: str | None
    rule_id: str | None
    condition_summary: str | None
    explanation_summary: str | None
    allowed_or_expected_values: str | None
    source_id: str
    source_locator: str
    authority_status: str
    relationship: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ValidationInformation":
        authority_status = _string(
            raw.get("authority_status"), field_name="validation authority_status"
        )
        if authority_status != VALIDATION_INFORMATION_STATUS:
            raise ValueError(
                "validation information must never claim rule execution or official validation"
            )
        relationship = _string(raw.get("relationship"), field_name="validation relationship")
        if relationship not in {"direct_target", "cross_field_reference"}:
            raise ValueError(f"unsupported validation relationship: {relationship}")
        return cls(
            severity=_optional_string(raw.get("severity"), field_name="validation severity"),
            rule_id=_optional_string(raw.get("rule_id"), field_name="validation rule_id"),
            condition_summary=_optional_string(
                raw.get("condition_summary"), field_name="validation condition_summary"
            ),
            explanation_summary=_optional_string(
                raw.get("explanation_summary"), field_name="validation explanation_summary"
            ),
            allowed_or_expected_values=_optional_string(
                raw.get("allowed_or_expected_values"),
                field_name="validation allowed_or_expected_values",
            ),
            source_id=_string(raw.get("source_id"), field_name="validation source_id"),
            source_locator=_string(
                raw.get("source_locator"), field_name="validation source_locator"
            ),
            authority_status=authority_status,
            relationship=relationship,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "rule_id": self.rule_id,
            "condition_summary": self.condition_summary,
            "explanation_summary": self.explanation_summary,
            "allowed_or_expected_values": self.allowed_or_expected_values,
            "source_id": self.source_id,
            "source_locator": self.source_locator,
            "authority_status": self.authority_status,
            "relationship": self.relationship,
        }


@dataclass(frozen=True, slots=True)
class SnbiFieldInformation:
    tag: str
    title: str
    format: str
    frequency: str
    frequency_meaning: str
    publication_page: int
    specification_locator: str
    unit: str | None
    reporting_notes: tuple[str, ...]
    interpretive_notes: tuple[str, ...]
    code_meanings: tuple[tuple[str, str], ...]
    code_meaning_source_ids: tuple[str, ...]
    code_profile_cautions_json: tuple[str, ...]
    transitions: tuple[TransitionInformation, ...]
    validation_notes: tuple[ValidationInformation, ...]
    applicability_cautions_json: tuple[str, ...]
    temporal_caution_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    knowledge_status: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SnbiFieldInformation":
        tag = _string(raw.get("tag"), field_name="tag").upper()
        if not _TAG_RE.fullmatch(tag):
            raise ValueError(f"invalid SNBI tag: {tag}")
        code_map = _mapping(raw.get("code_meanings"), field_name="code_meanings")
        publication_page = raw.get("publication_page")
        if isinstance(publication_page, bool) or not isinstance(publication_page, int):
            raise ValueError("publication_page must be an integer")
        code_profile_cautions = raw.get("code_profile_cautions")
        applicability_cautions = raw.get("applicability_cautions")
        transitions = raw.get("transition")
        validation_notes = raw.get("validation_notes")
        if not all(
            isinstance(value, list)
            for value in (
                code_profile_cautions,
                applicability_cautions,
                transitions,
                validation_notes,
            )
        ):
            raise ValueError(f"{tag} nested fact collections must be lists")
        return cls(
            tag=tag,
            title=_string(raw.get("title"), field_name=f"{tag} title"),
            format=_string(raw.get("format"), field_name=f"{tag} format"),
            frequency=_string(raw.get("frequency"), field_name=f"{tag} frequency"),
            frequency_meaning=_string(
                raw.get("frequency_meaning"), field_name=f"{tag} frequency_meaning"
            ),
            publication_page=publication_page,
            specification_locator=_string(
                raw.get("specification_locator"), field_name=f"{tag} specification_locator"
            ),
            unit=_optional_string(raw.get("unit"), field_name=f"{tag} unit"),
            reporting_notes=_strings(
                raw.get("reporting_notes"), field_name=f"{tag} reporting_notes"
            ),
            interpretive_notes=_strings(
                raw.get("interpretive_notes"), field_name=f"{tag} interpretive_notes"
            ),
            code_meanings=tuple(
                sorted(
                    (
                        _string(code, field_name=f"{tag} code"),
                        _string(description, field_name=f"{tag} code meaning"),
                    )
                    for code, description in code_map.items()
                )
            ),
            code_meaning_source_ids=_strings(
                raw.get("code_meaning_source_ids"),
                field_name=f"{tag} code_meaning_source_ids",
            ),
            code_profile_cautions_json=tuple(
                _stable_json(_mapping(item, field_name=f"{tag} code_profile_caution"))
                for item in code_profile_cautions
            ),
            transitions=tuple(
                TransitionInformation.from_dict(
                    _mapping(item, field_name=f"{tag} transition")
                )
                for item in transitions
            ),
            validation_notes=tuple(
                ValidationInformation.from_dict(
                    _mapping(item, field_name=f"{tag} validation_note")
                )
                for item in validation_notes
            ),
            applicability_cautions_json=tuple(
                _stable_json(_mapping(item, field_name=f"{tag} applicability_caution"))
                for item in applicability_cautions
            ),
            temporal_caution_ids=_strings(
                raw.get("temporal_caution_ids"), field_name=f"{tag} temporal_caution_ids"
            ),
            source_ids=_strings(raw.get("source_ids"), field_name=f"{tag} source_ids"),
            knowledge_status=_string(
                raw.get("knowledge_status"), field_name=f"{tag} knowledge_status"
            ),
        )

    @property
    def code_meanings_by_value(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self.code_meanings))

    def to_prompt_dict(self, *, max_validation_rules: int) -> dict[str, object]:
        validation = self.validation_notes[:max_validation_rules]
        codes = self.code_meanings[:MAX_PROMPT_CODE_MEANINGS_PER_FIELD]
        return {
            "tag": self.tag,
            "title": self.title,
            "format": self.format,
            "frequency": self.frequency,
            "frequency_meaning": self.frequency_meaning,
            "specification_locator": self.specification_locator,
            "unit": self.unit,
            "reporting_notes": list(self.reporting_notes),
            "interpretive_notes": list(self.interpretive_notes),
            "code_meanings": dict(codes),
            "code_meaning_count": len(self.code_meanings),
            "code_meanings_truncated": len(codes) < len(self.code_meanings),
            "code_meaning_source_ids": list(self.code_meaning_source_ids),
            "code_profile_cautions": [
                json.loads(value) for value in self.code_profile_cautions_json
            ],
            "transitions": [item.to_dict() for item in self.transitions],
            "validation_notes": [item.to_dict() for item in validation],
            "validation_note_count": len(self.validation_notes),
            "validation_notes_truncated": len(validation) < len(self.validation_notes),
            "applicability_cautions": [
                json.loads(value) for value in self.applicability_cautions_json
            ],
            "temporal_caution_ids": list(self.temporal_caution_ids),
            "source_ids": list(self.source_ids),
            "knowledge_status": self.knowledge_status,
        }


@dataclass(frozen=True, slots=True)
class SnbiInformationalCatalog:
    dataset_version: str
    dataset_sha256: str
    observed_on: str
    authority_boundary: str
    fields: tuple[SnbiFieldInformation, ...]
    sources: tuple[InformationSource, ...]
    q_and_a_notes_json: tuple[str, ...]
    coverage_json: str
    may_satisfy_official_validation: bool = False
    runtime_network_required: bool = False

    def __post_init__(self) -> None:
        if self.dataset_version != INFORMATION_VERSION:
            raise ValueError(f"unsupported informational dataset version: {self.dataset_version}")
        if self.may_satisfy_official_validation is not False:
            raise ValueError("informational enrichment may not satisfy official validation")
        if self.runtime_network_required is not False:
            raise ValueError("informational enrichment must be runtime-offline")
        tags = tuple(field.tag for field in self.fields)
        if len(tags) != 154 or len(set(tags)) != 154 or tags != tuple(sorted(tags)):
            raise ValueError("informational catalog must contain 154 unique, sorted SNBI fields")
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("informational source IDs must be unique")
        for field in self.fields:
            referenced = set(field.source_ids) | set(field.code_meaning_source_ids)
            referenced.update(item.source_id for item in field.transitions)
            referenced.update(item.source_id for item in field.validation_notes)
            if not referenced <= source_ids:
                raise ValueError(f"{field.tag} has unresolved source IDs: {referenced - source_ids}")

    @property
    def fields_by_tag(self) -> Mapping[str, SnbiFieldInformation]:
        return MappingProxyType({field.tag: field for field in self.fields})

    @property
    def sources_by_id(self) -> Mapping[str, InformationSource]:
        return MappingProxyType({source.source_id: source for source in self.sources})

    @property
    def coverage(self) -> Mapping[str, object]:
        return MappingProxyType(json.loads(self.coverage_json))

    @property
    def q_and_a_notes(self) -> tuple[Mapping[str, object], ...]:
        return tuple(MappingProxyType(json.loads(item)) for item in self.q_and_a_notes_json)

    def get(self, tag: str) -> SnbiFieldInformation:
        normalized = str(tag).strip().upper()
        field = self.fields_by_tag.get(normalized)
        if field is None:
            raise KeyError(tag)
        return field

    def _select(self, tags: Iterable[str]) -> tuple[SnbiFieldInformation, ...]:
        normalized = tuple(sorted({str(tag).strip().upper() for tag in tags}))
        if len(normalized) > MAX_PROMPT_FIELDS:
            raise ValueError(f"at most {MAX_PROMPT_FIELDS} informational fields may be selected")
        unknown = tuple(tag for tag in normalized if tag not in self.fields_by_tag)
        if unknown:
            raise KeyError(", ".join(unknown))
        return tuple(self.fields_by_tag[tag] for tag in normalized)

    def prompt_payload(
        self,
        tags: Iterable[str],
        *,
        max_validation_rules_per_field: int = MAX_PROMPT_VALIDATION_RULES_PER_FIELD,
    ) -> dict[str, object]:
        if (
            isinstance(max_validation_rules_per_field, bool)
            or not isinstance(max_validation_rules_per_field, int)
            or not 0 <= max_validation_rules_per_field <= MAX_PROMPT_VALIDATION_RULES_PER_FIELD
        ):
            raise ValueError(
                "max_validation_rules_per_field must be between 0 and "
                f"{MAX_PROMPT_VALIDATION_RULES_PER_FIELD}"
            )
        selected = self._select(tags)
        return {
            "dataset_version": self.dataset_version,
            "dataset_sha256": self.dataset_sha256,
            "authority_status": INFORMATION_AUTHORITY,
            "authority_boundary": self.authority_boundary,
            "may_satisfy_official_validation": False,
            "runtime_network_required": False,
            "selected_tags": [field.tag for field in selected],
            "selected_fields": [
                field.to_prompt_dict(max_validation_rules=max_validation_rules_per_field)
                for field in selected
            ],
            "source_catalog": [source.to_dict() for source in self.sources],
            "q_and_a_notes": [json.loads(item) for item in self.q_and_a_notes_json],
            "coverage": json.loads(self.coverage_json),
        }


def load_snbi_informational_catalog(
    path: Path = _INFORMATION_PATH,
) -> SnbiInformationalCatalog:
    """Load and verify the generated artifact without network or DB access."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("SNBI informational artifact is unavailable or invalid") from exc
    payload = _mapping(raw, field_name="SNBI informational artifact")
    claimed_hash = _string(payload.get("dataset_sha256"), field_name="dataset_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", claimed_hash):
        raise ValueError("dataset_sha256 must be a lowercase SHA-256 digest")
    hash_input = dict(payload)
    del hash_input["dataset_sha256"]
    calculated_hash = hashlib.sha256(_stable_json(hash_input).encode("utf-8")).hexdigest()
    if calculated_hash != claimed_hash:
        raise ValueError("SNBI informational artifact hash mismatch")

    raw_fields = payload.get("fields")
    raw_sources = payload.get("sources")
    raw_qa = payload.get("q_and_a_notes")
    if not isinstance(raw_fields, list) or not isinstance(raw_sources, list) or not isinstance(raw_qa, list):
        raise ValueError("fields, sources, and q_and_a_notes must be lists")
    if payload.get("field_count") != len(raw_fields):
        raise ValueError("field_count does not match the artifact fields")

    fields = tuple(
        sorted(
            (
                SnbiFieldInformation.from_dict(
                    _mapping(item, field_name="informational field")
                )
                for item in raw_fields
            ),
            key=lambda item: item.tag,
        )
    )
    sources = tuple(
        InformationSource.from_dict(_mapping(item, field_name="information source"))
        for item in raw_sources
    )
    _verify_vendored_source_metadata(sources)
    return SnbiInformationalCatalog(
        dataset_version=_string(payload.get("dataset_version"), field_name="dataset_version"),
        dataset_sha256=claimed_hash,
        observed_on=_string(payload.get("observed_on"), field_name="observed_on"),
        authority_boundary=_string(
            payload.get("authority_boundary"), field_name="authority_boundary"
        ),
        fields=fields,
        sources=sources,
        q_and_a_notes_json=tuple(
            _stable_json(_mapping(item, field_name="q_and_a_note")) for item in raw_qa
        ),
        coverage_json=_stable_json(
            _mapping(payload.get("coverage"), field_name="coverage")
        ),
        may_satisfy_official_validation=payload.get("may_satisfy_official_validation"),
        runtime_network_required=payload.get("runtime_network_required"),
    )


def _verify_vendored_source_metadata(sources: Iterable[InformationSource]) -> None:
    """Bind verified-source claims to the checked-in FHWA registry metadata."""

    try:
        registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("FHWA artifact registry is unavailable or invalid") from exc
    artifacts = {
        str(item.get("id")): item
        for item in registry.get("artifacts", [])
        if isinstance(item, dict) and item.get("id")
    }
    for source in sources:
        if source.artifact_state != VENDORED_VERIFIED_SOURCE_STATE:
            continue
        artifact = artifacts.get(source.source_id)
        if not isinstance(artifact, dict):
            raise ValueError(f"missing verified registry source {source.source_id}")
        if (
            artifact.get("state") != "vendored_verified"
            or artifact.get("sha256") != source.observed_sha256
            or artifact.get("source_url") != source.source_url
        ):
            raise ValueError(
                f"informational source metadata differs from verified registry artifact {source.source_id}"
            )


DEFAULT_SNBI_INFORMATION = load_snbi_informational_catalog()


__all__ = [
    "DEFAULT_SNBI_INFORMATION",
    "INFORMATION_AUTHORITY",
    "INFORMATION_VERSION",
    "REMOTE_OBSERVATION_STATE",
    "VENDORED_VERIFIED_SOURCE_STATE",
    "VALIDATION_INFORMATION_STATUS",
    "InformationSource",
    "SnbiFieldInformation",
    "SnbiInformationalCatalog",
    "TransitionInformation",
    "ValidationInformation",
    "load_snbi_informational_catalog",
]
