"""Deterministic semantic retrieval for Bridgette's SNBI query planner.

The retriever enriches the planner prompt; it is deliberately not an
authorization boundary and never narrows the semantic catalog.  Every field,
resource, metric, and join remains available through the existing reviewed
catalog.  Retrieval only ranks the definitions, value meanings, aliases, and
distinctions that deserve more prompt detail for one question.

The index is built entirely from checked-in, versioned evidence:

* the reviewed semantic catalog and metric registry;
* the hash-verified SNBI dictionary and informational field catalog; and
* a small reviewed BridgeGuard alias/policy registry.

No bridge rows, database metadata, network service, or model call are used.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from .metrics import DEFAULT_METRIC_REGISTRY
from .models import MetricRegistry, SemanticCatalog
from .snbi_dictionary import DEFAULT_SNBI_DICTIONARY, SnbiDictionary
from .snbi_official_information import (
    DEFAULT_SNBI_INFORMATION,
    SnbiInformationalCatalog,
)


RETRIEVAL_VERSION = "bridgeguard-semantic-retrieval-v3-contextual-codes"
ALIAS_SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 28
DEFAULT_MAX_DETAILED_TAGS = 18
_APP_ROOT = Path(__file__).resolve().parents[2]
_ALIAS_PATH = _APP_ROOT / "data" / "query" / "bridgette_semantic_aliases_v1.json"
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_TAG_RE = re.compile(r"^B[A-Z]{1,3}\d{2}$")
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,15}$")
_WORD_RE = re.compile(r"[a-z]+\d*|\d+")
_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "bridge",
        "bridges",
        "by",
        "do",
        "does",
        "each",
        "for",
        "from",
        "give",
        "have",
        "how",
        "in",
        "is",
        "it",
        "list",
        "many",
        "me",
        "of",
        "on",
        "or",
        "show",
        "that",
        "the",
        "their",
        "them",
        "these",
        "this",
        "to",
        "was",
        "were",
        "what",
        "which",
        "with",
    }
)


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _normalized_phrase(value: object) -> str:
    return " ".join(_WORD_RE.findall(str(value).casefold().replace("_", " ")))


def _stem(token: str) -> str:
    """Apply a deliberately small, domain-safe English normalization."""

    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(value: object, *, retain_stop_words: bool = False) -> tuple[str, ...]:
    output: list[str] = []
    for token in _WORD_RE.findall(str(value).casefold().replace("_", " ")):
        normalized = _stem(token)
        if len(normalized) < 2:
            continue
        if not retain_stop_words and normalized in _STOP_WORDS:
            continue
        if normalized not in output:
            output.append(normalized)
    return tuple(output)


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    output = tuple(_required_string(item, name) for item in value)
    if len(output) != len(set(output)):
        raise ValueError(f"{name} contains duplicates")
    return output


@dataclass(frozen=True, slots=True)
class SemanticAliasRecord:
    record_id: str
    kind: str
    aliases: tuple[str, ...]
    snbi_tags: tuple[str, ...]
    resource_ids: tuple[str, ...]
    field_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    canonical_values: tuple[str, ...]
    description: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SemanticAliasRecord":
        record_id = _required_string(raw.get("id"), "alias id")
        if not _IDENTIFIER_RE.fullmatch(record_id):
            raise ValueError(f"invalid alias id: {record_id}")
        kind = _required_string(raw.get("kind"), f"{record_id} kind")
        if kind not in {"field", "metric", "time_policy", "value", "value_set"}:
            raise ValueError(f"unsupported alias kind: {kind}")
        aliases = _string_list(raw.get("aliases"), f"{record_id} aliases")
        if not aliases:
            raise ValueError(f"{record_id} must have at least one alias")
        snbi_tags = tuple(
            tag.upper()
            for tag in _string_list(raw.get("snbi_tags"), f"{record_id} snbi_tags")
        )
        if any(not _TAG_RE.fullmatch(tag) for tag in snbi_tags):
            raise ValueError(f"{record_id} contains an invalid SNBI tag")
        return cls(
            record_id=record_id,
            kind=kind,
            aliases=aliases,
            snbi_tags=snbi_tags,
            resource_ids=_string_list(
                raw.get("resource_ids"), f"{record_id} resource_ids"
            ),
            field_ids=_string_list(raw.get("field_ids"), f"{record_id} field_ids"),
            metric_ids=_string_list(raw.get("metric_ids"), f"{record_id} metric_ids"),
            canonical_values=_string_list(
                raw.get("canonical_values"), f"{record_id} canonical_values"
            ),
            description=_required_string(
                raw.get("description"), f"{record_id} description"
            ),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "id": self.record_id,
            "kind": self.kind,
            "aliases": list(self.aliases),
            "snbi_tags": list(self.snbi_tags),
            "resource_ids": list(self.resource_ids),
            "field_ids": list(self.field_ids),
            "metric_ids": list(self.metric_ids),
            "canonical_values": list(self.canonical_values),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SemanticAliasCatalog:
    knowledge_version: str
    records: tuple[SemanticAliasRecord, ...]
    source_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        ids = tuple(record.record_id for record in self.records)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("semantic alias IDs must be unique")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("semantic alias source hash must be SHA-256")

    @property
    def records_by_id(self) -> Mapping[str, SemanticAliasRecord]:
        return MappingProxyType({record.record_id: record for record in self.records})


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    document_id: str
    kind: str
    title: str
    text: str
    aliases: tuple[str, ...] = ()
    snbi_tags: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    field_ids: tuple[str, ...] = ()
    metric_ids: tuple[str, ...] = ()
    canonical_values: tuple[str, ...] = ()
    source: str = "reviewed_semantic_catalog"

    @property
    def search_text(self) -> str:
        return " ".join(
            (
                self.document_id,
                self.title,
                self.text,
                *self.aliases,
                *self.snbi_tags,
                *self.resource_ids,
                *self.field_ids,
                *self.metric_ids,
                *self.canonical_values,
            )
        )


@lru_cache(maxsize=4_096)
def _document_tokens(document: RetrievalDocument) -> frozenset[str]:
    return frozenset(_tokens(document.search_text))


@lru_cache(maxsize=4_096)
def _document_alias_phrases(document: RetrievalDocument) -> tuple[str, ...]:
    return tuple(_normalized_phrase(alias) for alias in document.aliases)


@lru_cache(maxsize=4_096)
def _document_title_phrase(document: RetrievalDocument) -> str:
    return _normalized_phrase(document.title)


@dataclass(frozen=True, slots=True)
class RetrievedSemanticRecord:
    rank: int
    score: int
    match_reasons: tuple[str, ...]
    document: RetrievalDocument

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "score": self.score,
            "match_reasons": list(self.match_reasons),
            "id": self.document.document_id,
            "kind": self.document.kind,
            "title": self.document.title,
            "description": self.document.text,
            "aliases": list(self.document.aliases),
            "snbi_tags": list(self.document.snbi_tags),
            "resource_ids": list(self.document.resource_ids),
            "field_ids": list(self.document.field_ids),
            "metric_ids": list(self.document.metric_ids),
            "canonical_values": list(self.document.canonical_values),
            "source": self.document.source,
        }


@dataclass(frozen=True, slots=True)
class SemanticRetrievalResult:
    question_sha256: str
    knowledge_hash: str
    records: tuple[RetrievedSemanticRecord, ...]
    selected_tags: tuple[str, ...]
    selected_resource_ids: tuple[str, ...]
    selected_field_ids: tuple[str, ...]
    selected_metric_ids: tuple[str, ...]

    def prompt_payload(self) -> dict[str, object]:
        value_mappings = [
            {
                "record_id": record.document.document_id,
                "snbi_tags": list(record.document.snbi_tags),
                "field_ids": list(record.document.field_ids),
                "canonical_values": list(record.document.canonical_values),
                "description": record.document.text,
            }
            for record in self.records
            if record.document.canonical_values
        ]
        exact_semantic_requirements = [
            {
                "record_id": record.document.document_id,
                "field_ids": list(record.document.field_ids),
                "metric_ids": list(record.document.metric_ids),
                "canonical_values": list(record.document.canonical_values),
                "description": record.document.text,
            }
            for record in self.records
            if record.document.source == "bridgeguard_reviewed_alias_registry"
            # Canonical SNBI codes are heavily overloaded across fields (for
            # example S01 can mean a deck code, owner code, or maintenance
            # code). A bare code match may rank context, but it must not become
            # a fail-closed requirement unless the reviewed semantic alias
            # phrase itself is present in the question.
            and "exact_alias_phrase" in record.match_reasons
            and (
                record.document.kind in {"value", "value_set"}
                or record.document.document_id.startswith("alias.filter.")
            )
        ]
        return {
            "retrieval_version": RETRIEVAL_VERSION,
            "knowledge_hash": self.knowledge_hash,
            "question_sha256": self.question_sha256,
            "role": (
                "Additional ranked semantic context only. These results do not narrow "
                "the complete semantic catalog and are not an execution allowlist."
            ),
            "selection_rule": (
                "Prefer exact reviewed meanings when wording matches. Consider every "
                "catalog field and metric when these records are insufficient. Preserve "
                "distinctions such as owner versus maintenance responsibility, inventory "
                "year versus event year, and bridge rows versus child rows."
            ),
            "selected_tags": list(self.selected_tags),
            "selected_resource_ids": list(self.selected_resource_ids),
            "selected_field_ids": list(self.selected_field_ids),
            "selected_metric_ids": list(self.selected_metric_ids),
            "candidate_value_mappings": value_mappings,
            "exact_semantic_requirements": exact_semantic_requirements,
            "exact_semantic_requirement_rule": (
                "Every listed exact requirement must survive planning. Represent it "
                "with one of its reviewed metrics or with its exact field/value filter; "
                "never answer after silently dropping an exact requirement."
            ),
            "records": [record.to_prompt_dict() for record in self.records],
        }


def load_semantic_alias_catalog(
    path: Path = _ALIAS_PATH,
) -> SemanticAliasCatalog:
    try:
        source = path.read_bytes()
        raw = json.loads(source.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Bridgette semantic alias catalog is unavailable or invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("semantic alias catalog must be an object")
    if raw.get("schema_version") != ALIAS_SCHEMA_VERSION:
        raise ValueError("unsupported semantic alias schema version")
    records = raw.get("records")
    if not isinstance(records, list):
        raise ValueError("semantic alias records must be a list")
    return SemanticAliasCatalog(
        knowledge_version=_required_string(
            raw.get("knowledge_version"), "knowledge_version"
        ),
        records=tuple(
            SemanticAliasRecord.from_dict(item)
            for item in records
            if isinstance(item, dict)
        ),
        source_sha256=hashlib.sha256(source).hexdigest(),
    )


def _logical_links(
    catalog: SemanticCatalog, dictionary: SnbiDictionary
) -> Mapping[str, tuple[tuple[str, str], ...]]:
    by_projection: dict[str, list[tuple[str, str]]] = {}
    for resource in catalog.resources:
        for column in resource.columns:
            if column.physical_column:
                by_projection.setdefault(column.physical_column, []).append(
                    (resource.resource_id, column.field_id)
                )
    return MappingProxyType(
        {
            field.tag: tuple(
                sorted(by_projection.get(field.physical_column or "", ()))
            )
            for field in dictionary.fields
        }
    )


def _validate_alias_catalog(
    aliases: SemanticAliasCatalog,
    *,
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary,
    metrics: MetricRegistry,
) -> None:
    tags = dictionary.fields_by_tag
    resources = catalog.resources_by_id
    metric_ids = metrics.metrics_by_id
    every_field = {
        field_id
        for resource in catalog.resources
        for field_id in resource.column_ids
    }
    official_codes = {
        tag: set(field.code_meanings_by_value)
        for tag, field in DEFAULT_SNBI_INFORMATION.fields_by_tag.items()
    }
    for record in aliases.records:
        unknown_tags = set(record.snbi_tags).difference(tags)
        unknown_resources = set(record.resource_ids).difference(resources)
        unknown_fields = set(record.field_ids).difference(every_field)
        unknown_metrics = set(record.metric_ids).difference(metric_ids)
        if unknown_tags or unknown_resources or unknown_fields or unknown_metrics:
            raise ValueError(
                f"semantic alias {record.record_id} has unresolved targets: "
                f"tags={sorted(unknown_tags)}, resources={sorted(unknown_resources)}, "
                f"fields={sorted(unknown_fields)}, metrics={sorted(unknown_metrics)}"
            )
        if record.kind == "value" and len(record.canonical_values) != 1:
            raise ValueError(f"{record.record_id} value aliases require one value")
        if record.kind in {"value", "value_set"} and not record.snbi_tags:
            raise ValueError(f"{record.record_id} value aliases require an SNBI tag")
        if record.kind in {"value", "value_set"}:
            for value in record.canonical_values:
                if not _CODE_RE.fullmatch(value):
                    # Derived values such as Good/Fair/Poor belong to metric records,
                    # not value/value_set records.
                    raise ValueError(
                        f"{record.record_id} has an invalid canonical code {value}"
                    )
                if not any(value in official_codes.get(tag, set()) for tag in record.snbi_tags):
                    raise ValueError(
                        f"{record.record_id} code {value} is absent from its official field domains"
                    )


def _field_documents(
    *,
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary,
    information: SnbiInformationalCatalog,
) -> Iterable[RetrievalDocument]:
    links = _logical_links(catalog, dictionary)
    for field in dictionary.fields:
        info = information.fields_by_tag[field.tag]
        targets = links[field.tag]
        resource_ids = tuple(dict.fromkeys(resource for resource, _field in targets))
        field_ids = tuple(dict.fromkeys(field_id for _resource, field_id in targets))
        code_text = "; ".join(
            f"{code} means {meaning}" for code, meaning in info.code_meanings
        )
        notes = " ".join(
            (
                *info.reporting_notes,
                *info.interpretive_notes,
                field.temporal_semantics,
                field.null_semantics,
            )
        )
        yield RetrievalDocument(
            document_id=f"snbi.field.{field.tag.casefold()}",
            kind="snbi_field",
            title=f"{field.tag} {field.title}",
            text=" ".join(part for part in (notes, code_text) if part),
            aliases=tuple(dict.fromkeys((*field_ids, *field.legacy_source_fields))),
            snbi_tags=(field.tag,),
            resource_ids=resource_ids,
            field_ids=field_ids,
            canonical_values=tuple(code for code, _meaning in info.code_meanings),
            source="fhwa_snbi_information",
        )
        for code, meaning in info.code_meanings:
            # One record per code gives user wording such as "State toll authority"
            # enough weight to recover both its field and its exact value.
            yield RetrievalDocument(
                document_id=f"snbi.code.{field.tag.casefold()}.{code.casefold()}",
                kind="snbi_code",
                title=f"{field.tag} {code}: {meaning}",
                text=f"For {field.title}, canonical code {code} means {meaning}.",
                aliases=(meaning,),
                snbi_tags=(field.tag,),
                resource_ids=resource_ids,
                field_ids=field_ids,
                canonical_values=(code,),
                source="fhwa_snbi_code_meaning",
            )


def _catalog_documents(
    catalog: SemanticCatalog, metrics: MetricRegistry
) -> Iterable[RetrievalDocument]:
    for resource in catalog.resources:
        yield RetrievalDocument(
            document_id=f"catalog.resource.{resource.resource_id}",
            kind="semantic_resource",
            title=resource.resource_id,
            text=(
                f"{resource.description} Grain: {resource.grain.value}. "
                f"Time: {resource.time_semantics}."
            ),
            aliases=(resource.grain.value,),
            resource_ids=(resource.resource_id,),
            field_ids=resource.column_ids,
        )
        for column in resource.columns:
            yield RetrievalDocument(
                document_id=(
                    f"catalog.field.{resource.resource_id}.{column.field_id}"
                ),
                kind="semantic_field",
                title=f"{resource.resource_id}.{column.field_id}",
                text=(
                    f"{column.description} Unit: {column.unit or 'not specified'}. "
                    f"Nullable: {column.nullable}."
                ),
                aliases=(column.field_id,),
                resource_ids=(resource.resource_id,),
                field_ids=(column.field_id,),
            )
    for metric in metrics.metrics:
        yield RetrievalDocument(
            document_id=f"metric.{metric.metric_id}",
            kind="metric",
            title=f"{metric.metric_id}: {metric.label}",
            text=(
                f"{metric.description} Unit: {metric.unit}. "
                f"Nulls: {metric.null_semantics} Denominator: "
                f"{metric.denominator_semantics} Time: {metric.time_semantics} "
                f"Status: {metric.status.value}."
            ),
            aliases=(metric.label, metric.description),
            resource_ids=(metric.source_resource_id,),
            field_ids=metric.required_columns,
            metric_ids=(metric.metric_id,),
            source="bridgeguard_metric_registry",
        )
    for join in catalog.joins:
        yield RetrievalDocument(
            document_id=f"catalog.join.{join.join_id}",
            kind="semantic_join",
            title=join.join_id,
            text=(
                f"{join.description} Cardinality: {join.cardinality.value}. "
                f"Join {join.left_resource_id} to {join.right_resource_id}."
            ),
            aliases=(join.left_resource_id, join.right_resource_id),
            resource_ids=(join.left_resource_id, join.right_resource_id),
            field_ids=tuple(dict.fromkeys((*join.left_fields, *join.right_fields))),
        )


def _alias_documents(aliases: SemanticAliasCatalog) -> Iterable[RetrievalDocument]:
    for record in aliases.records:
        yield RetrievalDocument(
            document_id=f"alias.{record.record_id}",
            kind=record.kind,
            title=record.aliases[0],
            text=record.description,
            aliases=record.aliases,
            snbi_tags=record.snbi_tags,
            resource_ids=record.resource_ids,
            field_ids=record.field_ids,
            metric_ids=record.metric_ids,
            canonical_values=record.canonical_values,
            source="bridgeguard_reviewed_alias_registry",
        )


def _score_document(
    question: str,
    *,
    normalized_question: str,
    question_tokens: frozenset[str],
    explicit_tags: frozenset[str],
    document: RetrievalDocument,
) -> tuple[int, tuple[str, ...]]:
    document_tokens = _document_tokens(document)
    reasons: list[str] = []
    score = 0

    normalized_tags = {tag.replace(".", "") for tag in document.snbi_tags}
    if explicit_tags.intersection(normalized_tags):
        score += 1_000
        reasons.append("exact_snbi_tag")

    for normalized_alias in _document_alias_phrases(document):
        if len(normalized_alias) >= 3 and normalized_alias in normalized_question:
            score += 320 + min(len(normalized_alias), 80)
            reasons.append("exact_alias_phrase")
            break

    for value in document.canonical_values:
        if len(value) >= 2 and re.search(
            rf"(?<![A-Za-z0-9-]){re.escape(value)}(?![A-Za-z0-9-])",
            question,
            re.IGNORECASE,
        ):
            score += 260
            reasons.append("exact_canonical_value")
            break

    title = _document_title_phrase(document)
    if len(title) >= 3 and title in normalized_question:
        score += 220
        reasons.append("exact_title_phrase")

    overlap = question_tokens.intersection(document_tokens)
    if overlap:
        score += 18 * len(overlap)
        coverage = len(overlap) / max(1, len(question_tokens))
        score += round(40 * coverage)
        reasons.append("lexical_overlap")

    if question_tokens and question_tokens.issubset(document_tokens):
        score += 60
        reasons.append("complete_term_coverage")

    fuzzy_hits = 0
    fuzzy_candidate = score > 0 or any(
        len(question_token) >= 5
        and any(
            len(candidate) >= 4
            and question_token[:2] == candidate[:2]
            and abs(len(question_token) - len(candidate)) <= 3
            for candidate in document_tokens
        )
        for question_token in question_tokens
    )
    if (
        fuzzy_candidate
        and question_tokens
        and document_tokens
        and document.kind
        in {"field", "metric", "time_policy", "value", "value_set", "snbi_field"}
    ):
        for question_token in question_tokens:
            if question_token in document_tokens or len(question_token) < 5:
                continue
            best = max(
                (
                    SequenceMatcher(None, question_token, candidate).ratio()
                    for candidate in document_tokens
                    if abs(len(question_token) - len(candidate)) <= 3
                    and len(candidate) >= 4
                ),
                default=0.0,
            )
            if best >= 0.82:
                fuzzy_hits += 1
        if fuzzy_hits:
            score += 8 * min(fuzzy_hits, 4)
            reasons.append("fuzzy_spelling")

    # Reviewed alias and exact-code records win ties over broad field/resource
    # documents, but broad catalog records remain available for recall.
    if document.source == "bridgeguard_reviewed_alias_registry" and score:
        score += 12
    if document.kind == "snbi_code" and "exact_alias_phrase" in reasons:
        score += 18
    return score, tuple(dict.fromkeys(reasons))


@lru_cache(maxsize=16)
def _retrieval_documents(
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary,
    information: SnbiInformationalCatalog,
    metrics: MetricRegistry,
    aliases: SemanticAliasCatalog,
) -> tuple[RetrievalDocument, ...]:
    return tuple(
        (
            *_alias_documents(aliases),
            *_field_documents(
                catalog=catalog,
                dictionary=dictionary,
                information=information,
            ),
            *_catalog_documents(catalog, metrics),
        )
    )


def _knowledge_hash(
    *,
    aliases: SemanticAliasCatalog,
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary,
    information: SnbiInformationalCatalog,
    metrics: MetricRegistry,
) -> str:
    payload = {
        "retrieval_version": RETRIEVAL_VERSION,
        "alias_version": aliases.knowledge_version,
        "alias_sha256": aliases.source_sha256,
        "catalog_definition_version": catalog.definition_version,
        "dictionary_sha256": dictionary.dictionary_hash,
        "information_sha256": information.dataset_sha256,
        "metric_registry_sha256": metrics.registry_hash,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def retrieve_semantic_context(
    question: str,
    *,
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary = DEFAULT_SNBI_DICTIONARY,
    information: SnbiInformationalCatalog = DEFAULT_SNBI_INFORMATION,
    metrics: MetricRegistry = DEFAULT_METRIC_REGISTRY,
    aliases: SemanticAliasCatalog | None = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_detailed_tags: int = DEFAULT_MAX_DETAILED_TAGS,
) -> SemanticRetrievalResult:
    """Return additive ranked context for one natural-language question."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    if isinstance(max_records, bool) or not 1 <= int(max_records) <= 100:
        raise ValueError("max_records must be between 1 and 100")
    if isinstance(max_detailed_tags, bool) or not 1 <= int(max_detailed_tags) <= 50:
        raise ValueError("max_detailed_tags must be between 1 and 50")
    active_aliases = aliases or DEFAULT_SEMANTIC_ALIASES
    _validate_alias_catalog(
        active_aliases,
        catalog=catalog,
        dictionary=dictionary,
        metrics=metrics,
    )
    documents = _retrieval_documents(
        catalog,
        dictionary,
        information,
        metrics,
        active_aliases,
    )
    scored: list[tuple[int, str, tuple[str, ...], RetrievalDocument]] = []
    normalized_question = _normalized_phrase(question)
    question_tokens = frozenset(_tokens(question))
    explicit_tags = frozenset(
        match.group(0).replace(".", "").upper()
        for match in re.finditer(
            r"\bB(?:\.[A-Z]{1,3}\.)?[A-Z]{1,3}\.?\d{2}\b",
            question.upper(),
        )
    )
    for document in documents:
        score, reasons = _score_document(
            question,
            normalized_question=normalized_question,
            question_tokens=question_tokens,
            explicit_tags=explicit_tags,
            document=document,
        )
        if score > 0:
            scored.append((score, document.document_id, reasons, document))
    scored.sort(key=lambda item: (-item[0], item[1]))
    minimum_score = max(36, round(scored[0][0] * 0.15)) if scored else 36

    # Diversity keeps a large owner code table from consuming every slot while
    # still permitting several exact code/value matches from one field.
    selected: list[tuple[int, str, tuple[str, ...], RetrievalDocument]] = []
    kind_counts: dict[str, int] = {}
    for item in scored:
        if item[0] < minimum_score:
            break
        document = item[3]
        limit_for_kind = 10 if document.kind in {"value", "value_set", "snbi_code"} else 12
        if kind_counts.get(document.kind, 0) >= limit_for_kind:
            continue
        selected.append(item)
        kind_counts[document.kind] = kind_counts.get(document.kind, 0) + 1
        if len(selected) >= int(max_records):
            break

    records = tuple(
        RetrievedSemanticRecord(
            rank=index,
            score=score,
            match_reasons=reasons,
            document=document,
        )
        for index, (score, _identifier, reasons, document) in enumerate(selected, 1)
    )
    selected_tags = tuple(
        dict.fromkeys(
            tag
            for record in records
            for tag in record.document.snbi_tags
        )
    )[: int(max_detailed_tags)]
    return SemanticRetrievalResult(
        question_sha256=hashlib.sha256(question.strip().encode("utf-8")).hexdigest(),
        knowledge_hash=_knowledge_hash(
            aliases=active_aliases,
            catalog=catalog,
            dictionary=dictionary,
            information=information,
            metrics=metrics,
        ),
        records=records,
        selected_tags=selected_tags,
        selected_resource_ids=tuple(
            dict.fromkeys(
                resource_id
                for record in records
                for resource_id in record.document.resource_ids
            )
        ),
        selected_field_ids=tuple(
            dict.fromkeys(
                field_id
                for record in records
                for field_id in record.document.field_ids
            )
        ),
        selected_metric_ids=tuple(
            dict.fromkeys(
                metric_id
                for record in records
                for metric_id in record.document.metric_ids
            )
        ),
    )


DEFAULT_SEMANTIC_ALIASES = load_semantic_alias_catalog()


__all__ = [
    "ALIAS_SCHEMA_VERSION",
    "DEFAULT_MAX_DETAILED_TAGS",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_SEMANTIC_ALIASES",
    "RETRIEVAL_VERSION",
    "RetrievedSemanticRecord",
    "SemanticAliasCatalog",
    "SemanticAliasRecord",
    "SemanticRetrievalResult",
    "load_semantic_alias_catalog",
    "retrieve_semantic_context",
]
