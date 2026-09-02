"""Deterministic, workspace-scoped bridge resolution for map navigation.

The resolver is deliberately independent of Bridgette's model/query path.  It
reads only the published inventory projection (with an explicit legacy-only
fallback for installations that have not yet backfilled revisions), applies a
fixed match order, and returns immutable identity tokens with every match.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any, Literal, Mapping

from app.services.bridge_identity import (
    BridgeIdentityError,
    BridgeSnapshotNotFound,
    resolve_published_bridge_identity,
)


MAX_QUERY_CHARS = 512
MAX_SEARCH_ROWS = 25_000
MAX_RESPONSE_CANDIDATES = 5
AMBIGUITY_MARGIN = 12.0

_NJ_COUNTY_NAMES = {
    1: "Atlantic", 3: "Bergen", 5: "Burlington", 7: "Camden", 9: "Cape May",
    11: "Cumberland", 13: "Essex", 15: "Gloucester", 17: "Hudson",
    19: "Hunterdon", 21: "Mercer", 23: "Middlesex", 25: "Monmouth",
    27: "Morris", 29: "Ocean", 31: "Passaic", 33: "Salem", 35: "Somerset",
    37: "Sussex", 39: "Union", 41: "Warren",
}


class MapResolutionError(RuntimeError):
    """A safe, typed resolver failure."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class InventoryGeneration:
    workspace_id: str
    data_generation: int
    schema_generation: int


@dataclass(frozen=True)
class _RankedBridge:
    row: Mapping[str, Any]
    score: float
    basis: Literal["bridge_number", "approved_alias", "controlled_text"]


_ACTION_WORDS = frozenset(
    {
        "a",
        "an",
        "bridge",
        "card",
        "center",
        "centre",
        "find",
        "is",
        "locate",
        "map",
        "me",
        "on",
        "please",
        "show",
        "take",
        "the",
        "to",
        "where",
        "zoom",
    }
)


def _normal_form(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def normalize_bridge_identifier(value: object) -> str:
    """Normalize formatting, never numeric value (leading zeroes are retained)."""

    return "".join(char for char in _normal_form(value).upper() if char.isalnum())


def normalize_search_text(value: object) -> str:
    text = _normal_form(value).casefold()
    return " ".join(
        "".join(char if char.isalnum() else " " for char in text).split()
    )


def _clean_action_text(query: str) -> str:
    tokens = normalize_search_text(query).split()
    clean = [token for token in tokens if token not in _ACTION_WORDS]
    return " ".join(clean) or " ".join(tokens)


def _identifier_needles(query: str, state_code: int) -> tuple[str, ...]:
    needles: list[str] = []
    for raw in re.findall(r"(?<![\w])(?:[A-Za-z0-9][A-Za-z0-9_.-]{2,63})(?![\w])", query):
        normalized = normalize_bridge_identifier(raw)
        if not normalized or not any(char.isdigit() for char in normalized):
            continue
        # A bare calendar year in a sentence is a time selector, not a bridge.
        if len(normalized) == 4 and normalized.isdigit() and 1900 <= int(normalized) <= 2100:
            continue
        needles.append(normalized)
        state_prefix = str(int(state_code))
        if normalized.startswith(state_prefix) and len(normalized) - len(state_prefix) >= 3:
            needles.append(normalized[len(state_prefix) :])
    entire = normalize_bridge_identifier(_clean_action_text(query))
    if entire and any(char.isdigit() for char in entire):
        needles.append(entire)
    return tuple(dict.fromkeys(needles))


def _connect_readonly(database_path: str) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA busy_timeout=1500")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    if not _table_exists(connection, table):
        return frozenset()
    return frozenset(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _load_generation(
    connection: sqlite3.Connection, workspace_id: str
) -> InventoryGeneration:
    if not _table_exists(connection, "workspace_state"):
        raise MapResolutionError(
            503,
            "inventory_revision_unavailable",
            "The published inventory revision is not available.",
        )
    row = connection.execute(
        "SELECT workspace_id, data_generation, schema_generation "
        "FROM workspace_state WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise MapResolutionError(404, "workspace_not_found", "Workspace not found.")
    return InventoryGeneration(
        workspace_id=str(row["workspace_id"]),
        data_generation=int(row["data_generation"]),
        schema_generation=int(row["schema_generation"]),
    )


def _column_expression(columns: frozenset[str], name: str, prefix: str = "b") -> str:
    if name in columns:
        return f'{prefix}."{name}" AS "{name}"'
    return f'NULL AS "{name}"'


def _inventory_sql(
    connection: sqlite3.Connection,
    *,
    has_revisions: bool,
) -> str:
    columns = _table_columns(connection, "bridges")
    required = {"state_code", "bridge_number", "data_year", "latitude", "longitude"}
    if not required.issubset(columns):
        raise MapResolutionError(
            503,
            "map_projection_unavailable",
            "The bridge map projection is not available.",
        )
    projected_names = (
        "state_code",
        "bridge_number",
        "data_year",
        "latitude",
        "longitude",
        "county_code",
        "bridge_name",
        "bridge_location",
        "year_built",
    )
    bridge_projection = ",\n                   ".join(
        _column_expression(columns, name) for name in projected_names
    )
    has_features = _table_exists(connection, "features")
    feature_columns = _table_columns(connection, "features")
    feature_capable = {
        "state_code",
        "bridge_number",
        "data_year",
        "feature_name",
        "feature_location",
    }.issubset(feature_columns)
    if has_features and feature_capable:
        aliases = """
               (SELECT GROUP_CONCAT(TRIM(f.feature_name), CHAR(31)) FROM features f
                 WHERE f.state_code=s.state_code AND f.bridge_number=s.bridge_number
                   AND f.data_year IS s.data_year
                   AND NULLIF(TRIM(f.feature_name), '') IS NOT NULL) AS feature_aliases,
               (SELECT NULLIF(TRIM(f.feature_name), '') FROM features f
                 WHERE f.state_code=s.state_code AND f.bridge_number=s.bridge_number
                   AND f.data_year IS s.data_year AND f.feature_location='C'
                   AND NULLIF(TRIM(f.feature_name), '') IS NOT NULL
                 ORDER BY f.feature_type LIMIT 1) AS carried_name,
               (SELECT NULLIF(TRIM(f.feature_name), '') FROM features f
                 WHERE f.state_code=s.state_code AND f.bridge_number=s.bridge_number
                   AND f.data_year IS s.data_year AND f.feature_location='B'
                   AND NULLIF(TRIM(f.feature_name), '') IS NOT NULL
                 ORDER BY f.feature_type LIMIT 1) AS below_name
        """
    else:
        aliases = "NULL AS feature_aliases, NULL AS carried_name, NULL AS below_name"

    if has_revisions:
        inventory = f"""
            SELECT {bridge_projection},
                   ps.snapshot_revision_id AS snapshot_revision_id,
                   sr.revision_number AS revision_number,
                   sr.canonical_sha256 AS canonical_sha256,
                   'published' AS identity_source
            FROM published_snapshots ps
            JOIN snapshot_revisions sr
              ON sr.workspace_id=ps.workspace_id AND sr.id=ps.snapshot_revision_id
            JOIN bridges b
              ON b.state_code=ps.state_code AND b.bridge_number=ps.bridge_number
             AND b.data_year=ps.data_year
            WHERE ps.workspace_id=? AND ps.state_code=?
            UNION ALL
            SELECT {bridge_projection},
                   NULL AS snapshot_revision_id, NULL AS revision_number,
                   NULL AS canonical_sha256, 'legacy_projection' AS identity_source
            FROM bridges b
            WHERE b.state_code=?
              AND NOT EXISTS (
                  SELECT 1 FROM published_snapshots any_ps
                  WHERE any_ps.workspace_id=?
                    AND any_ps.state_code=b.state_code
                    AND any_ps.bridge_number=b.bridge_number
              )
        """
    else:
        inventory = f"""
            SELECT {bridge_projection},
                   'loaded-inventory:' || CAST(b.state_code AS TEXT) || ':' ||
                   b.bridge_number || ':' || CAST(b.data_year AS TEXT) AS snapshot_revision_id,
                   1 AS revision_number,
                   NULL AS canonical_sha256, 'loaded_inventory' AS identity_source
            FROM bridges b WHERE b.state_code=?
        """

    return f"""
        WITH inventory AS ({inventory}),
        scoped AS (
            SELECT i.* FROM inventory i
            WHERE i.data_year IS NOT NULL
              AND ((? IS NOT NULL AND i.data_year=?) OR
                   (? IS NULL AND i.data_year IS (
                       SELECT MAX(i2.data_year) FROM inventory i2
                       WHERE i2.state_code=i.state_code
                         AND i2.bridge_number=i.bridge_number
                   )))
        )
        SELECT s.*, {aliases}
        FROM scoped s
        WHERE s.latitude BETWEEN -90 AND 90 AND s.longitude BETWEEN -180 AND 180
        ORDER BY s.bridge_number, s.data_year
        LIMIT ?
    """


def _load_inventory_rows(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    state_code: int,
    year: int | None,
) -> tuple[Mapping[str, Any], ...]:
    # Exact yearly inventory rows are authoritative. Publication pointers are
    # deliberately outside the active map-resolution path.
    has_revisions = False
    sql = _inventory_sql(connection, has_revisions=has_revisions)
    prefix: list[Any]
    if has_revisions:
        prefix = [workspace_id, state_code, state_code, workspace_id]
    else:
        prefix = [state_code]
    params = [*prefix, year, year, year, MAX_SEARCH_ROWS + 1]
    rows = tuple(dict(row) for row in connection.execute(sql, params).fetchall())
    if len(rows) > MAX_SEARCH_ROWS:
        raise MapResolutionError(
            503,
            "map_search_capacity_exceeded",
            "The bridge search is temporarily too broad. Use a structure number or a more specific name.",
        )
    return rows


def _finite_coordinate(value: object, minimum: float, maximum: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def _aliases(row: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("bridge_name", "carried_name", "below_name"):
        value = _normal_form(row.get(key))
        if value:
            values.append(value)
    carried = _normal_form(row.get("carried_name"))
    below = _normal_form(row.get("below_name"))
    if carried and below:
        values.append(f"{carried} over {below}")
    feature_aliases = _normal_form(row.get("feature_aliases"))
    if feature_aliases:
        values.extend(item.strip() for item in feature_aliases.split("\x1f") if item.strip())
    return tuple(dict.fromkeys(values))


def _text_score(query: str, row: Mapping[str, Any]) -> float:
    query_tokens = tuple(dict.fromkeys(token for token in query.split() if len(token) >= 2))
    if not query_tokens:
        return 0.0
    fields = [*_aliases(row), _normal_form(row.get("bridge_location"))]
    normalized_fields = [normalize_search_text(field) for field in fields if field]
    combined_tokens = set(" ".join(normalized_fields).split())
    matched = sum(1 for token in query_tokens if token in combined_tokens)
    coverage = matched / len(query_tokens)
    if matched == 0 or (len(query_tokens) > 1 and coverage < 0.60):
        return 0.0
    phrase_bonus = 24.0 if any(query and query in field for field in normalized_fields) else 0.0
    name_bonus = 16.0 if any(
        any(token in normalize_search_text(alias).split() for token in query_tokens)
        for alias in _aliases(row)
    ) else 0.0
    specificity = min(sum(len(token) for token in query_tokens), 40) / 4.0
    return round(55.0 * coverage + phrase_bonus + name_bonus + specificity, 3)


def _rank(query: str, state_code: int, rows: tuple[Mapping[str, Any], ...]) -> tuple[_RankedBridge, ...]:
    identifier_needles = set(_identifier_needles(query, state_code))
    exact_ids = [
        _RankedBridge(row=row, score=1000.0, basis="bridge_number")
        for row in rows
        if normalize_bridge_identifier(row.get("bridge_number")) in identifier_needles
    ]
    if exact_ids:
        return tuple(sorted(exact_ids, key=_rank_sort_key))

    clean_query = _clean_action_text(query)
    alias_matches = [
        _RankedBridge(row=row, score=900.0, basis="approved_alias")
        for row in rows
        if clean_query and clean_query in {normalize_search_text(alias) for alias in _aliases(row)}
    ]
    if alias_matches:
        return tuple(sorted(alias_matches, key=_rank_sort_key))

    ranked = [
        _RankedBridge(row=row, score=_text_score(clean_query, row), basis="controlled_text")
        for row in rows
    ]
    return tuple(sorted((item for item in ranked if item.score >= 55.0), key=_rank_sort_key))


def _rank_sort_key(item: _RankedBridge) -> tuple[float, str, int]:
    return (
        -item.score,
        normalize_bridge_identifier(item.row.get("bridge_number")),
        int(item.row.get("data_year") or 0),
    )


def _candidate_payload(
    item: _RankedBridge,
    *,
    database_path: str,
    generation: InventoryGeneration,
) -> dict[str, Any]:
    row = item.row
    state_code = int(row["state_code"])
    bridge_number = str(row["bridge_number"])
    snapshot_year = int(row["data_year"])
    bridge_id = f"{generation.workspace_id}:{state_code}:{bridge_number}"
    snapshot_id = f"{bridge_id}:{snapshot_year}"
    identity_payload: dict[str, object] | None = None
    if row.get("identity_source") == "published":
        try:
            identity = resolve_published_bridge_identity(
                database_path,
                workspace_id=generation.workspace_id,
                state_code=state_code,
                bridge_number=bridge_number,
                data_year=snapshot_year,
            )
            identity_payload = identity.public_payload()
        except BridgeSnapshotNotFound as exc:
            raise MapResolutionError(
                503,
                "published_identity_mismatch",
                "The published bridge identity could not be verified.",
            ) from exc
        except BridgeIdentityError as exc:
            raise MapResolutionError(
                503,
                "published_identity_unavailable",
                "The published bridge identity is unavailable.",
            ) from exc
    if identity_payload is not None:
        if int(identity_payload["data_generation"]) != generation.data_generation:
            raise MapResolutionError(
                503,
                "published_identity_generation_mismatch",
                "The published bridge identity changed during map resolution.",
            )
        if int(identity_payload["schema_generation"]) != generation.schema_generation:
            raise MapResolutionError(
                503,
                "published_identity_schema_mismatch",
                "The published bridge identity schema changed during map resolution.",
            )
    inventory_revision = str(row.get("snapshot_revision_id") or "") or None
    carried = _normal_form(row.get("carried_name"))
    below = _normal_form(row.get("below_name"))
    display_name = _normal_form(row.get("bridge_name"))
    if not display_name:
        display_name = f"{carried} over {below}" if carried and below else carried or below
    label = display_name or f"Bridge {bridge_number}"
    latitude = _finite_coordinate(row.get("latitude"), -90.0, 90.0)
    longitude = _finite_coordinate(row.get("longitude"), -180.0, 180.0)
    county_code = int(row["county_code"]) if row.get("county_code") is not None else None
    return {
        "bridge_id": bridge_id,
        "snapshot_id": snapshot_id,
        "snapshot_revision_id": inventory_revision,
        "inventory_revision": inventory_revision,
        "identity_source": "loaded_inventory",
        "identity_status": "exact_year_loaded_inventory",
        "revision_number": (
            int(identity_payload["revision_number"]) if identity_payload is not None else None
        ),
        "canonical_sha256": (
            str(identity_payload["canonical_sha256"]) if identity_payload is not None else None
        ),
        "profile_id": str(identity_payload["profile_id"]) if identity_payload is not None else None,
        "rule_bundle_sha256": (
            str(identity_payload["rule_bundle_sha256"]) if identity_payload is not None else None
        ),
        "published_at": (
            str(identity_payload["published_at"]) if identity_payload is not None else None
        ),
        "data_revision": generation.data_generation,
        "schema_generation": generation.schema_generation,
        "state_code": state_code,
        "bridge_number": bridge_number,
        "snapshot_year": snapshot_year,
        "data_year": snapshot_year,
        "county_code": county_code,
        "county_name": _NJ_COUNTY_NAMES.get(county_code) if state_code == 34 else None,
        "bridge_name": _normal_form(row.get("bridge_name")) or None,
        "display_name": display_name or None,
        "bridge_location": _normal_form(row.get("bridge_location")) or None,
        "year_built": row.get("year_built"),
        "latitude": latitude,
        "longitude": longitude,
        "coordinates": {"latitude": latitude, "longitude": longitude},
        "label": label,
        "match_basis": item.basis,
        "match_score": item.score,
    }


def resolve_bridge(
    database_path: str,
    *,
    workspace_id: str,
    state_code: int,
    query: str,
    year: int | None = None,
) -> dict[str, Any]:
    """Resolve one map target without model inference or client-selected scope."""

    clean_query = _normal_form(query)
    if not clean_query:
        raise MapResolutionError(422, "query_required", "A bridge search query is required.")
    if len(clean_query) > MAX_QUERY_CHARS:
        raise MapResolutionError(422, "query_too_long", "The bridge search query is too long.")
    if year is not None and not 1900 <= int(year) <= 2100:
        raise MapResolutionError(422, "invalid_year", "The inventory year is invalid.")

    try:
        connection = _connect_readonly(database_path)
    except sqlite3.Error as exc:
        raise MapResolutionError(503, "inventory_unavailable", "The bridge inventory is unavailable.") from exc
    try:
        generation = _load_generation(connection, workspace_id)
        rows = _load_inventory_rows(
            connection,
            workspace_id=workspace_id,
            state_code=int(state_code),
            year=year,
        )
    except sqlite3.Error as exc:
        raise MapResolutionError(503, "map_search_unavailable", "The bridge search is unavailable.") from exc
    finally:
        connection.close()

    ranked = _rank(clean_query, int(state_code), rows)
    base: dict[str, Any] = {
        "workspace_id": workspace_id,
        "state_code": int(state_code),
        "requested_year": year,
        "data_generation": generation.data_generation,
        "schema_generation": generation.schema_generation,
        "candidate_count": len(ranked),
    }
    if not ranked:
        return {**base, "status": "not_found", "bridge": None, "candidates": []}

    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    tied_exact = second is not None and top.basis != "controlled_text"
    insufficient_margin = (
        second is not None
        and top.basis == "controlled_text"
        and top.score - second.score < AMBIGUITY_MARGIN
    )
    if tied_exact or insufficient_margin:
        candidates = [
            _candidate_payload(item, database_path=database_path, generation=generation)
            for item in ranked[:MAX_RESPONSE_CANDIDATES]
        ]
        return {**base, "status": "ambiguous", "bridge": None, "candidates": candidates}

    bridge = _candidate_payload(top, database_path=database_path, generation=generation)
    return {
        **base,
        "status": "exact",
        "inventory_revision": bridge["inventory_revision"],
        "bridge": bridge,
        "candidates": [],
    }


__all__ = [
    "AMBIGUITY_MARGIN",
    "MAX_RESPONSE_CANDIDATES",
    "MapResolutionError",
    "normalize_bridge_identifier",
    "normalize_search_text",
    "resolve_bridge",
]
