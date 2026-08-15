"""Deterministic pre-query interpretation gate for Bridgette.

The gate handles a deliberately small set of high-risk ambiguities before any
model or database query is allowed to run.  It is conservative by design: a
``proceed`` decision means that none of the *known* patterns below matched; it
does not claim that every unsupported request can be recognized from text.

This module performs no database access and does not construct SQL.  Callers
may pass the reviewed semantic catalog solely to report whether a nearby SNBI
field is exposed by the current query surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable

from .metrics import DEFAULT_METRIC_REGISTRY
from .models import MetricRegistry, SemanticCatalog
from .snbi_dictionary import DEFAULT_SNBI_DICTIONARY, SnbiDictionary


INTERPRETATION_VERSION = "bridgette-query-interpretation-v1"
DETECTION_SCOPE = "conservative_known_patterns_not_exhaustive"


class InterpretationDecision(str, Enum):
    PROCEED = "proceed"
    CLARIFICATION = "clarification"
    ABSTENTION = "abstention"


@dataclass(frozen=True, slots=True)
class InterpretationOption:
    """One stable, user-selectable clarification choice."""

    option_id: str
    label: str
    value: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.option_id):
            raise ValueError("option_id must be a stable lowercase identifier")
        if not self.label.strip() or not self.value.strip():
            raise ValueError("option label and value are required")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.option_id, "label": self.label, "value": self.value}


@dataclass(frozen=True, slots=True)
class CountyReference:
    """Normalized New Jersey county identity (state FIPS 34)."""

    name: str
    county_fips: str
    state_fips: str = "34"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "state_fips": self.state_fips,
            "county_fips": self.county_fips,
        }


@dataclass(frozen=True, slots=True)
class NearbySupportedField:
    """A real dictionary field shown for orientation, never as a proxy."""

    field_id: str
    snbi_tag: str
    title: str
    query_availability: str

    def to_dict(self) -> dict[str, str]:
        return {
            "field_id": self.field_id,
            "snbi_tag": self.snbi_tag,
            "title": self.title,
            "query_availability": self.query_availability,
        }


@dataclass(frozen=True, slots=True)
class QueryInterpretation:
    """Frozen result of the deterministic pre-query interpretation gate."""

    decision: InterpretationDecision
    code: str
    message: str
    options: tuple[InterpretationOption, ...] = ()
    normalized_counties: tuple[CountyReference, ...] = ()
    unknown_county_text: str | None = None
    unresolved_bridge_reference: str | None = None
    requested_fact: str | None = None
    nearby_supported_fields: tuple[NearbySupportedField, ...] = ()
    detected_issue_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    version: str = INTERPRETATION_VERSION
    detection_scope: str = DETECTION_SCOPE

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "normalized_counties", tuple(self.normalized_counties))
        object.__setattr__(self, "nearby_supported_fields", tuple(self.nearby_supported_fields))
        object.__setattr__(self, "detected_issue_codes", tuple(self.detected_issue_codes))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", self.code):
            raise ValueError("interpretation code must be a safe lowercase identifier")
        if not self.message.strip():
            raise ValueError("interpretation message is required")
        if self.decision is InterpretationDecision.PROCEED and self.options:
            raise ValueError("a proceed result cannot contain clarification options")
        if self.decision is InterpretationDecision.CLARIFICATION and len(self.options) < 2:
            raise ValueError("a clarification result needs at least two choices")
        if self.decision is InterpretationDecision.ABSTENTION and self.options:
            raise ValueError("an abstention result cannot contain clarification options")

    @property
    def should_query(self) -> bool:
        return self.decision is InterpretationDecision.PROCEED

    @property
    def nearest_fields_not_substituted(self) -> tuple[str, ...]:
        return tuple(field.field_id for field in self.nearby_supported_fields)

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-ready representation for logs and tests."""

        return {
            "version": self.version,
            "detection_scope": self.detection_scope,
            "decision": self.decision.value,
            "code": self.code,
            "message": self.message,
            "options": [option.to_dict() for option in self.options],
            "normalized_counties": [
                county.to_dict() for county in self.normalized_counties
            ],
            "unknown_county_text": self.unknown_county_text,
            "unresolved_bridge_reference": self.unresolved_bridge_reference,
            "requested_fact": self.requested_fact,
            "nearest_fields_not_substituted": list(
                self.nearest_fields_not_substituted
            ),
            "nearby_supported_fields": [
                field.to_dict() for field in self.nearby_supported_fields
            ],
            "detected_issue_codes": list(self.detected_issue_codes),
            "warnings": list(self.warnings),
        }

    def to_event_payload(self) -> dict[str, object]:
        """Return the existing Bridgette decision-event shape.

        A proceed result is intentionally not a stream event; callers should
        continue to planning when :attr:`should_query` is true.
        """

        if self.should_query:
            raise ValueError("proceed results are not Bridgette stream events")
        payload = self.to_dict()
        payload.pop("decision")
        payload["type"] = self.decision.value
        return payload


_NJ_COUNTIES: tuple[CountyReference, ...] = (
    CountyReference("Atlantic", "001"),
    CountyReference("Bergen", "003"),
    CountyReference("Burlington", "005"),
    CountyReference("Camden", "007"),
    CountyReference("Cape May", "009"),
    CountyReference("Cumberland", "011"),
    CountyReference("Essex", "013"),
    CountyReference("Gloucester", "015"),
    CountyReference("Hudson", "017"),
    CountyReference("Hunterdon", "019"),
    CountyReference("Mercer", "021"),
    CountyReference("Middlesex", "023"),
    CountyReference("Monmouth", "025"),
    CountyReference("Morris", "027"),
    CountyReference("Ocean", "029"),
    CountyReference("Passaic", "031"),
    CountyReference("Salem", "033"),
    CountyReference("Somerset", "035"),
    CountyReference("Sussex", "037"),
    CountyReference("Union", "039"),
    CountyReference("Warren", "041"),
)


@dataclass(frozen=True, slots=True)
class _UnsupportedRule:
    pattern: re.Pattern[str]
    requested_fact: str
    nearby_field_ids: tuple[str, ...]


_UNSUPPORTED_RULES: tuple[_UnsupportedRule, ...] = (
    _UnsupportedRule(
        re.compile(
            r"(?:\boriginal\b.{0,28})?(?:\bpaint(?:ed)?\b|\bcoating\b)"
            r".{0,28}\bcolou?r\b|\bcolou?r\b.{0,28}"
            r"(?:\bpaint(?:ed)?\b|\bcoating\b)",
            re.IGNORECASE,
        ),
        "original paint color",
        ("span_material", "span_protective_system"),
    ),
    _UnsupportedRule(
        re.compile(
            r"\btoll\b.{0,24}\b(?:price|rate|fee|cost|amount|dollars?)\b|"
            r"\b(?:price|rate|fee|cost|amount)\b.{0,24}\btoll\b",
            re.IGNORECASE,
        ),
        "toll price or fee",
        ("toll",),
    ),
    _UnsupportedRule(
        re.compile(r"\b(?:crash(?:es)?|collisions?|accidents?)\b", re.IGNORECASE),
        "crash or accident history",
        ("annual_average_daily_traffic", "bridge_location"),
    ),
    _UnsupportedRule(
        re.compile(
            r"\b(?:live|real[- ]?time)\b.{0,24}\b(?:traffic|speed|congestion)\b|"
            r"\b(?:traffic|speed|congestion)\b.{0,24}\b(?:live|real[- ]?time)\b",
            re.IGNORECASE,
        ),
        "live traffic or congestion",
        (
            "annual_average_daily_traffic",
            "year_of_annual_average_daily_traffic",
        ),
    ),
    _UnsupportedRule(
        re.compile(
            r"\b(?:current|live|today'?s?)\b.{0,20}\bweather\b|"
            r"\bweather\b.{0,20}\b(?:current|live|today|now)\b",
            re.IGNORECASE,
        ),
        "current weather",
        ("bridge_location", "latitude", "longitude"),
    ),
    _UnsupportedRule(
        re.compile(
            r"\b(?:owner|maintenance)\b.{0,28}\b(?:phone|email|contact)\b|"
            r"\b(?:phone|email|contact)\b.{0,28}\b(?:owner|maintenance)\b",
            re.IGNORECASE,
        ),
        "owner or maintainer contact details",
        ("owner", "maintenance_responsibility"),
    ),
)


_CURRENT_RE = re.compile(r"\b(?:current|currently|now)\b", re.IGNORECASE)
_CURRENT_QUALIFIED_RE = re.compile(
    r"\b(?:latest|most recent)\s+(?:published\s+)?(?:inventory|snapshot|record|data)\b|"
    r"\b(?:current|latest)\s+published\s+(?:inventory|snapshot|record|data)\b|"
    r"\b(?:current\s+)?calendar\s+(?:date|year)\b|\bas of \d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
_NHS_RE = re.compile(r"\bNHS\b|\bNational Highway System\b", re.IGNORECASE)
_NHS_QUALIFIED_RE = re.compile(
    r"\bBH03\b|\b(?:carried|roadway|feature)[- ]level\b|"
    r"\bcarried\s+(?:highway|roadway|route|feature)\b|"
    r"\b(?:root|inventory|bridge)[- ]level\s+(?:inventory\s+)?designation\b|"
    r"\broot\s+inventory\b|\bfederal\s+(?:performance|population)\b|"
    r"\bTPM\b|\b23\s*CFR\b|\bdeck[- ]area[- ]weighted\b",
    re.IGNORECASE,
)
_CONDITION_RE = re.compile(r"\bcondition(?:s)?\b", re.IGNORECASE)
_BAD_BRIDGE_RE = re.compile(
    r"\bbad\b.{0,28}\b(?:bridge|condition)s?\b|"
    r"\b(?:bridge|condition)s?\b.{0,28}\bbad\b",
    re.IGNORECASE,
)
_CONDITION_QUALIFIED_RE = re.compile(
    r"\b(?:deck|superstructure|substructure|culvert|bearing|joint|railing|"
    r"channel|scour|NSTM|underwater|element|component)\b|"
    r"\bBC(?:0[1-9]|1[0-5])\b|\b(?:controlling|lowest|operational)\b|"
    r"\b(?:Good|Fair|Poor)\b|\bcondition\s+(?:class|classification)\b",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|good\s+(?:morning|afternoon|evening))"
    r"(?:\s+(?:bridgette|there))?[!.?]*$",
    re.IGNORECASE,
)


def _catalog_field_ids(catalog: SemanticCatalog | None) -> set[str] | None:
    if catalog is None:
        return None
    return {
        column.field_id
        for resource in catalog.resources
        for column in resource.columns
    }


def _nearby_fields(
    field_ids: Iterable[str],
    *,
    dictionary: SnbiDictionary,
    catalog: SemanticCatalog | None,
) -> tuple[NearbySupportedField, ...]:
    by_column = {
        field.physical_column: field
        for field in dictionary.fields
        if field.physical_column
    }
    catalog_ids = _catalog_field_ids(catalog)
    result: list[NearbySupportedField] = []
    for field_id in field_ids:
        definition = by_column.get(field_id)
        if definition is None:
            # Never present a nearby field unless the reviewed dictionary
            # proves that it is an actual SNBI field.
            continue
        if catalog_ids is None:
            availability = "not_evaluated"
        elif field_id in catalog_ids:
            availability = "semantic_catalog"
        else:
            availability = "snbi_dictionary_only"
        result.append(
            NearbySupportedField(
                field_id=field_id,
                snbi_tag=definition.tag,
                title=definition.title,
                query_availability=availability,
            )
        )
    return tuple(result)


def _normalize_for_matching(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("-", " ")).strip()


def _county_matches(question: str) -> tuple[CountyReference, ...]:
    normalized = _normalize_for_matching(question)
    lowered = normalized.casefold()
    plural_context = bool(re.search(r"\bcounties\b", lowered))
    matches: list[tuple[int, CountyReference]] = []
    for county in _NJ_COUNTIES:
        name = re.escape(county.name.casefold()).replace(r"\ ", r"\s+")
        patterns = (
            rf"\b{name}\s+count(?:y|ies)\b",
            rf"\b(?:in|within|for|across|inside|of)\s+(?:the\s+)?{name}\b",
        )
        positions = [
            match.start()
            for pattern in patterns
            if (match := re.search(pattern, lowered)) is not None
        ]
        if plural_context:
            plain = re.search(rf"\b{name}\b", lowered)
            if plain is not None:
                positions.append(plain.start())
        if positions:
            matches.append((min(positions), county))
    return tuple(county for _, county in sorted(matches, key=lambda item: (item[0], item[1].county_fips)))


def _unknown_county(question: str, known: tuple[CountyReference, ...]) -> str | None:
    if known:
        return None
    patterns = (
        re.compile(
            r"\b(?:in|within|for|across|inside|of)\s+(?:the\s+)?"
            r"(?P<name>[A-Za-z][A-Za-z .'-]{0,39}?)\s+count(?:y|ies)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?P<name>[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,2})"
            r"\s+County\b"
        ),
    )
    for pattern in patterns:
        match = pattern.search(question)
        if match is None:
            continue
        candidate = re.sub(r"\s+", " ", match.group("name")).strip(" .'-")
        if candidate and candidate.casefold() not in {
            "the",
            "a",
            "each",
            "every",
            "any",
        }:
            return candidate
    return None


def _bridge_reference(question: str) -> str | None:
    demonstrative = re.search(
        r"\b(?P<reference>(?:this|that|selected)\s+(?:bridge|structure))\b",
        question,
        re.IGNORECASE,
    )
    if demonstrative is not None:
        return demonstrative.group("reference")[:80]

    identifier = re.search(
        r"\b(?:bridge|structure)\s*(?:number|no\.?|id|#)\s*[:#-]?\s*"
        r"(?P<identifier>[A-Z0-9][A-Z0-9._/-]{1,39})\b",
        question,
        re.IGNORECASE,
    )
    if identifier is not None:
        return identifier.group("identifier")[:80]

    named = re.search(
        r"\b(?:bridge|structure)\s+(?:named|called)\s+"
        r"(?P<name>[^?.,;]{2,80})",
        question,
        re.IGNORECASE,
    )
    if named is not None:
        return named.group("name").strip()[:80]

    proper_name = re.search(
        r"\b(?:of|for|on|at|about)\s+(?:the\s+)?"
        r"(?P<name>[A-Z][A-Za-z0-9'&.-]*(?:\s+[A-Z][A-Za-z0-9'&.-]*){0,6}"
        r"\s+(?:Bridge|Skyway|Viaduct|Overpass))\b",
        question,
    )
    if proper_name is not None:
        return proper_name.group("name")[:80]

    # Also catch a bare proper-name suffix (for example, "Pulaski Skyway")
    # without interpreting generic singular/plural uses of "bridge" as names.
    bare = re.search(
        r"\b(?P<name>[A-Z][A-Za-z0-9'&.-]+(?:\s+[A-Z][A-Za-z0-9'&.-]+){0,5}"
        r"\s+(?:Bridge|Skyway|Viaduct|Overpass))\b",
        question,
    )
    if bare is not None:
        name = bare.group("name")
        if name.casefold() not in {
            "a bridge",
            "any bridge",
            "each bridge",
            "every bridge",
            "the bridge",
        }:
            return name[:80]
    return None


def _clarification(
    *,
    code: str,
    message: str,
    options: tuple[InterpretationOption, ...],
    counties: tuple[CountyReference, ...],
    all_codes: tuple[str, ...],
    unknown_county_text: str | None = None,
    unresolved_bridge_reference: str | None = None,
) -> QueryInterpretation:
    return QueryInterpretation(
        decision=InterpretationDecision.CLARIFICATION,
        code=code,
        message=message,
        options=options,
        normalized_counties=counties,
        unknown_county_text=unknown_county_text,
        unresolved_bridge_reference=unresolved_bridge_reference,
        detected_issue_codes=all_codes,
        warnings=("No SQL or model call was made.",),
    )


def interpret_bridge_query(
    question: str,
    *,
    bridge_number: str | None = None,
    state_code: int | str = 34,
    dictionary: SnbiDictionary = DEFAULT_SNBI_DICTIONARY,
    catalog: SemanticCatalog | None = None,
    metrics: MetricRegistry = DEFAULT_METRIC_REGISTRY,
) -> QueryInterpretation:
    """Classify one question before planning, model use, or database access.

    ``metrics`` is accepted as reviewed semantic evidence for callers and
    future rules.  Accessing its immutable hash also ensures a malformed
    custom registry fails before a query could proceed.
    """

    text = str(question).strip()
    if not text:
        raise ValueError("question is required")
    # Force deterministic validation of a caller-supplied registry without
    # claiming that every natural-language metric can be recognized here.
    _ = metrics.registry_hash

    if _GREETING_RE.fullmatch(text):
        return QueryInterpretation(
            decision=InterpretationDecision.ABSTENTION,
            code="greeting",
            message=(
                "Hello! Ask me about published New Jersey bridge inventory, "
                "SNBI fields, condition ratings, traffic, owners, counties, or "
                "historical trends. For example: ‘How many published bridge "
                "snapshots have data year 2025?’"
            ),
            detected_issue_codes=("greeting",),
            warnings=("No SQL or model call was made.",),
        )

    try:
        is_new_jersey = int(state_code) == 34 and not isinstance(state_code, bool)
    except (TypeError, ValueError):
        is_new_jersey = False
    counties = _county_matches(text) if is_new_jersey else ()
    unknown_county = _unknown_county(text, counties) if is_new_jersey else None
    unresolved_bridge = None if str(bridge_number or "").strip() else _bridge_reference(text)

    unsupported = next(
        (rule for rule in _UNSUPPORTED_RULES if rule.pattern.search(text)),
        None,
    )
    current_ambiguous = bool(
        _CURRENT_RE.search(text) and not _CURRENT_QUALIFIED_RE.search(text)
    )
    nhs_ambiguous = bool(_NHS_RE.search(text) and not _NHS_QUALIFIED_RE.search(text))
    condition_ambiguous = bool(
        (_CONDITION_RE.search(text) or _BAD_BRIDGE_RE.search(text))
        and not _CONDITION_QUALIFIED_RE.search(text)
    )

    issue_codes: list[str] = []
    if unsupported is not None:
        issue_codes.append("unsupported_fact")
    if unknown_county is not None:
        issue_codes.append("unknown_nj_county")
    if len(counties) > 1:
        issue_codes.append("multiple_nj_counties")
    if unresolved_bridge is not None:
        issue_codes.append("unresolved_bridge_identity")
    if current_ambiguous:
        issue_codes.append("current_time_semantics")
    if nhs_ambiguous:
        issue_codes.append("nhs_meaning")
    if condition_ambiguous:
        issue_codes.append("condition_metric")
    all_codes = tuple(issue_codes)

    # Known unsupported facts win over ambiguities: choosing another meaning
    # must never turn a nearby field into an invented proxy answer.
    if unsupported is not None:
        nearby = _nearby_fields(
            unsupported.nearby_field_ids,
            dictionary=dictionary,
            catalog=catalog,
        )
        return QueryInterpretation(
            decision=InterpretationDecision.ABSTENTION,
            code="unsupported_fact",
            message=(
                "The reviewed SNBI dictionary and semantic query surface do not "
                f"represent the requested {unsupported.requested_fact}. Nearby "
                "fields are listed only for orientation and were not substituted."
            ),
            normalized_counties=counties,
            unresolved_bridge_reference=unresolved_bridge,
            requested_fact=unsupported.requested_fact,
            nearby_supported_fields=nearby,
            detected_issue_codes=all_codes,
            warnings=("No nearby field was used as a proxy.",),
        )

    if unknown_county is not None:
        return _clarification(
            code="unknown_nj_county",
            message=(
                "The county name could not be normalized to one of New Jersey's "
                "21 counties. Please choose a valid county or remove the county filter."
            ),
            options=(
                InterpretationOption(
                    "choose_nj_county",
                    "Choose a New Jersey county",
                    "provide_nj_county_name",
                ),
                InterpretationOption(
                    "remove_county_filter",
                    "Do not filter by county",
                    "remove_county_filter",
                ),
            ),
            counties=counties,
            all_codes=all_codes,
            unknown_county_text=unknown_county,
            unresolved_bridge_reference=unresolved_bridge,
        )

    if len(counties) > 1:
        county_options = tuple(
            InterpretationOption(
                f"county_{county.county_fips}_only",
                f"{county.name} County only",
                f"county_fips={county.county_fips}",
            )
            for county in counties
        )
        return _clarification(
            code="multiple_nj_counties",
            message=(
                "More than one New Jersey county was named. Specify one county, "
                "or state whether the counties should be separate groups or a combined set."
            ),
            options=county_options
            + (
                InterpretationOption(
                    "counties_separate",
                    "Compare as separate county groups",
                    "group_by_county",
                ),
                InterpretationOption(
                    "counties_combined",
                    "Treat as one combined set",
                    "combine_counties",
                ),
            ),
            counties=counties,
            all_codes=all_codes,
            unresolved_bridge_reference=unresolved_bridge,
        )

    if unresolved_bridge is not None:
        return _clarification(
            code="unresolved_bridge_identity",
            message=(
                "The question targets a bridge by an unresolved name or identifier, "
                "but the authorized request scope has no exact bridge number."
            ),
            options=(
                InterpretationOption(
                    "provide_bridge_number",
                    "Provide the exact agency bridge number",
                    "provide_bridge_number",
                ),
                InterpretationOption(
                    "select_bridge_match",
                    "Search and select a bridge by name and location",
                    "resolve_bridge_name",
                ),
            ),
            counties=counties,
            all_codes=all_codes,
            unresolved_bridge_reference=unresolved_bridge,
        )

    if current_ambiguous:
        return _clarification(
            code="current_time_semantics",
            message=(
                "Does current mean the calendar date/year, or the latest published "
                "inventory snapshot selected independently for each bridge?"
            ),
            options=(
                InterpretationOption(
                    "latest_published_inventory",
                    "Latest published inventory per bridge",
                    "latest_published_inventory",
                ),
                InterpretationOption(
                    "current_calendar_time",
                    "Current calendar date or year",
                    "current_calendar_time",
                ),
            ),
            counties=counties,
            all_codes=all_codes,
        )

    if nhs_ambiguous:
        return _clarification(
            code="nhs_meaning",
            message=(
                "NHS can refer to a bridge-level inventory projection, the SNBI "
                "designation of a carried highway feature, or a regulated federal "
                "performance-measure population. Which meaning is intended?"
            ),
            options=(
                InterpretationOption(
                    "bridge_inventory_nhs",
                    "Bridge-level inventory NHS designation",
                    "bridge_inventory_nhs",
                ),
                InterpretationOption(
                    "carried_feature_nhs",
                    "Carried-highway feature NHS (SNBI BH03)",
                    "carried_feature_nhs",
                ),
                InterpretationOption(
                    "federal_performance_nhs",
                    "Federal performance-measure population",
                    "federal_performance_nhs",
                ),
            ),
            counties=counties,
            all_codes=all_codes,
        )

    if condition_ambiguous:
        return _clarification(
            code="condition_metric",
            message=(
                "Condition could mean the reviewed controlling operational class, "
                "a particular SNBI component rating, or a regulated federal "
                "performance measure. Which definition should be used?"
            ),
            options=(
                InterpretationOption(
                    "controlling_operational_condition",
                    "Controlling operational Good/Fair/Poor class",
                    "controlling_operational_condition",
                ),
                InterpretationOption(
                    "component_condition",
                    "A specific component rating",
                    "specify_condition_component",
                ),
                InterpretationOption(
                    "federal_condition_measure",
                    "Federal bridge-condition performance measure",
                    "federal_condition_measure",
                ),
            ),
            counties=counties,
            all_codes=all_codes,
        )

    return QueryInterpretation(
        decision=InterpretationDecision.PROCEED,
        code="ready_for_planning",
        message=(
            "No known deterministic ambiguity or unsupported-fact pattern matched. "
            "This conservative gate is not an exhaustive support classifier."
        ),
        normalized_counties=counties,
        detected_issue_codes=(),
    )


__all__ = [
    "CountyReference",
    "DETECTION_SCOPE",
    "INTERPRETATION_VERSION",
    "InterpretationDecision",
    "InterpretationOption",
    "NearbySupportedField",
    "QueryInterpretation",
    "interpret_bridge_query",
]
