"""Reviewed logical resources for bridge-inventory questions.

These definitions are code-reviewed configuration, not a reflection of the
live SQLite schema.  They can back physical views in a later migration, but the
logical contract is useful immediately for plan validation and prompt context.
"""

from __future__ import annotations

from .models import (
    CatalogIdentity,
    JoinCardinality,
    ResourceGrain,
    SafeFunction,
    SemanticCatalog,
    SemanticColumn,
    SemanticDataType,
    SemanticJoin,
    SemanticResource,
)


CATALOG_DEFINITION_VERSION = "bridgeguard-semantic-v1"
_SCOPE_FIELDS = ("workspace_id", "state_code", "bridge_number", "data_year")
_SNAPSHOT_JOIN_FIELDS = _SCOPE_FIELDS


_TEXT_OPERATORS = (
    "eq",
    "ne",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "is_null",
    "is_not_null",
)
_NUMBER_OPERATORS = (
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "between",
    "is_null",
    "is_not_null",
)
_BOOLEAN_OPERATORS = ("eq", "ne", "is_null", "is_not_null")


def _column(
    field_id: str,
    data_type: SemanticDataType,
    nullable: bool,
    description: str,
    *,
    physical_column: str | None = None,
    unit: str | None = None,
) -> SemanticColumn:
    if data_type in (SemanticDataType.INTEGER, SemanticDataType.NUMBER):
        operators = _NUMBER_OPERATORS
    elif data_type is SemanticDataType.BOOLEAN:
        operators = _BOOLEAN_OPERATORS
    else:
        operators = _TEXT_OPERATORS
    return SemanticColumn(
        field_id=field_id,
        data_type=data_type,
        nullable=nullable,
        description=description,
        physical_column=physical_column if physical_column is not None else field_id,
        unit=unit,
        allowed_operators=operators,
    )


def _derived_column(
    field_id: str,
    data_type: SemanticDataType,
    nullable: bool,
    description: str,
    *,
    unit: str | None = None,
) -> SemanticColumn:
    column = _column(
        field_id,
        data_type,
        nullable,
        description,
        physical_column=field_id,
        unit=unit,
    )
    # ``None`` explicitly signals that the field comes from a reviewed logical
    # expression rather than a physical projection column with the same name.
    object.__setattr__(column, "physical_column", None)
    return column


_IDENTITY_COLUMNS = (
    _column("workspace_id", SemanticDataType.TEXT, False, "Authorized workspace scope."),
    _column(
        "snapshot_revision_id",
        SemanticDataType.TEXT,
        False,
        "Immutable canonical revision backing this published snapshot.",
    ),
    _column("state_code", SemanticDataType.INTEGER, False, "State FIPS code."),
    _column(
        "bridge_number",
        SemanticDataType.TEXT,
        False,
        "Agency bridge identifier; preserve leading zeros.",
    ),
    _column("data_year", SemanticDataType.INTEGER, False, "Exact inventory snapshot year."),
)


_BRIDGE_COLUMNS = _IDENTITY_COLUMNS + (
    _column("published_at", SemanticDataType.DATETIME, False, "Publication timestamp."),
    _column("bridge_name", SemanticDataType.TEXT, True, "Bridge or structure name."),
    _column("county_code", SemanticDataType.INTEGER, True, "County code within the state."),
    _column("bridge_location", SemanticDataType.TEXT, True, "Inventory location description."),
    _column("latitude", SemanticDataType.NUMBER, True, "Latitude in decimal degrees.", unit="degree"),
    _column("longitude", SemanticDataType.NUMBER, True, "Longitude in decimal degrees.", unit="degree"),
    _column("owner", SemanticDataType.TEXT, True, "Inventory owner code."),
    _column(
        "maintenance_responsibility",
        SemanticDataType.TEXT,
        True,
        "Inventory maintenance-responsibility code.",
    ),
    _column("year_built", SemanticDataType.INTEGER, True, "Original construction year."),
    _column(
        "total_bridge_length",
        SemanticDataType.NUMBER,
        True,
        "Total bridge length reported for the snapshot.",
        unit="feet",
    ),
    _column(
        "maximum_span_length",
        SemanticDataType.NUMBER,
        True,
        "Maximum span length reported for the snapshot.",
        unit="feet",
    ),
    _column(
        "bridge_width_out_to_out",
        SemanticDataType.NUMBER,
        True,
        "Out-to-out bridge width.",
        unit="feet",
    ),
    _column(
        "calculated_deck_area",
        SemanticDataType.NUMBER,
        True,
        "Reported or transition-calculated deck area.",
        unit="square_feet",
    ),
    _column(
        "nhs_designation",
        SemanticDataType.TEXT,
        True,
        "Root inventory NHS designation; feature-level NHS is preferred for roadway questions.",
    ),
    _column(
        "deck_condition_rating",
        SemanticDataType.NUMBER,
        True,
        "SNBI BC01 deck condition rating.",
        unit="rating_0_to_9",
    ),
    _column(
        "superstructure_condition_rating",
        SemanticDataType.NUMBER,
        True,
        "SNBI BC02 superstructure condition rating.",
        unit="rating_0_to_9",
    ),
    _column(
        "substructure_condition_rating",
        SemanticDataType.NUMBER,
        True,
        "SNBI BC03 substructure condition rating.",
        unit="rating_0_to_9",
    ),
    _column(
        "culvert_condition_rating",
        SemanticDataType.NUMBER,
        True,
        "SNBI BC04 culvert condition rating.",
        unit="rating_0_to_9",
    ),
    _column(
        "lowest_condition_rating_code",
        SemanticDataType.NUMBER,
        True,
        "Stored SNBI BC13 lowest-condition value, when valid.",
        unit="rating_0_to_9",
    ),
)


_FEATURE_COLUMNS = _IDENTITY_COLUMNS + (
    _column("feature_type", SemanticDataType.TEXT, False, "SNBI BF01 feature identifier."),
    _column("feature_location", SemanticDataType.TEXT, True, "SNBI BF02 carried/below location code."),
    _column("feature_name", SemanticDataType.TEXT, True, "SNBI BF03 feature name."),
    _column("functional_classification", SemanticDataType.TEXT, True, "Feature functional class."),
    _column("urban_code", SemanticDataType.TEXT, True, "Feature urban-area code."),
    _column("nhs_designation", SemanticDataType.TEXT, True, "Feature NHS designation."),
    _column(
        "national_highway_freight_network",
        SemanticDataType.TEXT,
        True,
        "Feature National Highway Freight Network designation.",
    ),
    _column("strahnet_designation", SemanticDataType.TEXT, True, "Feature STRAHNET designation."),
    _column("lrs_route_id", SemanticDataType.TEXT, True, "Feature linear-referencing route ID."),
    _column("lrs_mile_point", SemanticDataType.NUMBER, True, "Feature LRS mile point.", unit="mile"),
    _column("lanes_on_highway", SemanticDataType.INTEGER, True, "Number of lanes on the highway."),
    _column(
        "annual_average_daily_traffic",
        SemanticDataType.INTEGER,
        True,
        "Annual average daily traffic for this feature observation.",
        unit="vehicles_per_day",
    ),
    _column(
        "annual_average_daily_truck_traffic",
        SemanticDataType.INTEGER,
        True,
        "Annual average daily truck traffic for this feature observation.",
        unit="trucks_per_day",
    ),
    _column(
        "year_of_annual_average_daily_traffic",
        SemanticDataType.INTEGER,
        True,
        "Year represented by the AADT values.",
    ),
    _column(
        "highway_max_usable_vertical_clearance",
        SemanticDataType.NUMBER,
        True,
        "Maximum usable highway vertical clearance.",
        unit="feet",
    ),
    _column(
        "highway_min_vertical_clearance",
        SemanticDataType.NUMBER,
        True,
        "Minimum highway vertical clearance.",
        unit="feet",
    ),
    _column(
        "highway_min_horizontal_clearance_left",
        SemanticDataType.NUMBER,
        True,
        "Minimum horizontal clearance on the left.",
        unit="feet",
    ),
    _column(
        "highway_min_horizontal_clearance_right",
        SemanticDataType.NUMBER,
        True,
        "Minimum horizontal clearance on the right.",
        unit="feet",
    ),
    _column(
        "highway_max_usable_surface_width",
        SemanticDataType.NUMBER,
        True,
        "Maximum usable roadway surface width.",
        unit="feet",
    ),
    _column("bypass_detour_length", SemanticDataType.INTEGER, True, "Bypass/detour length.", unit="mile"),
    _column("crossing_bridge_number", SemanticDataType.TEXT, True, "Related crossing bridge identifier."),
    _column("railroad_service_type", SemanticDataType.TEXT, True, "Railroad service type."),
    _column(
        "railroad_min_vertical_clearance",
        SemanticDataType.NUMBER,
        True,
        "Minimum railroad vertical clearance.",
        unit="feet",
    ),
    _column(
        "railroad_min_horizontal_offset",
        SemanticDataType.NUMBER,
        True,
        "Minimum railroad horizontal offset.",
        unit="feet",
    ),
    _column("navigable_waterway", SemanticDataType.TEXT, True, "Navigable-waterway designation."),
    _column(
        "navigable_min_vertical_clearance",
        SemanticDataType.NUMBER,
        True,
        "Minimum navigation vertical clearance.",
        unit="feet",
    ),
    _column(
        "movable_bridge_max_navigation_vertical_clearance",
        SemanticDataType.NUMBER,
        True,
        "Maximum navigation clearance for a movable bridge.",
        unit="feet",
    ),
    _column("navigation_channel_width", SemanticDataType.NUMBER, True, "Navigation channel width.", unit="feet"),
    _column(
        "navigation_channel_min_horizontal_clearance",
        SemanticDataType.NUMBER,
        True,
        "Minimum horizontal clearance in the navigation channel.",
        unit="feet",
    ),
    _column(
        "substructure_navigation_protection",
        SemanticDataType.TEXT,
        True,
        "Navigation-protection designation for the substructure.",
    ),
)


PUBLISHED_BRIDGE_SNAPSHOTS = SemanticResource(
    resource_id="v_published_bridge_snapshots",
    description=(
        "Only canonical revisions currently published for an exact bridge and data year; "
        "staged, superseded, archived, and unlinked projection rows are excluded."
    ),
    grain=ResourceGrain.BRIDGE_SNAPSHOT,
    columns=_BRIDGE_COLUMNS,
    time_semantics="data_year is exact; no implicit latest-year selection is applied",
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_published_bridge_snapshots", "published_snapshots", "bridges"),
    implementation_id="published_snapshot_join_v1",
)


CURRENT_BRIDGE_SNAPSHOTS = SemanticResource(
    resource_id="v_current_bridge_snapshots",
    description=(
        "Latest published snapshot selected independently for each bridge inside the authorized "
        "workspace/state scope; never the database-wide maximum year."
    ),
    grain=ResourceGrain.CURRENT_BRIDGE,
    columns=_BRIDGE_COLUMNS,
    time_semantics="one maximum published data_year per bridge within the server-injected scope",
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_current_bridge_snapshots", "published_snapshots", "bridges"),
    implementation_id="per_bridge_latest_published_v1",
)


BRIDGE_FEATURES = SemanticResource(
    resource_id="v_bridge_features",
    description="Feature rows belonging to published bridge snapshots only.",
    grain=ResourceGrain.BRIDGE_FEATURE,
    columns=_FEATURE_COLUMNS,
    time_semantics="feature data_year is the exact published parent snapshot year",
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_bridge_features", "published_snapshots", "features"),
    implementation_id="published_feature_join_v1",
)


CARRIED_HIGHWAY_FEATURES = SemanticResource(
    resource_id="v_carried_highway_features",
    description=(
        "Published SNBI H01 carried-highway features. Use this resource for carried-route NHS, "
        "traffic, lanes, and roadway-clearance questions."
    ),
    grain=ResourceGrain.BRIDGE_FEATURE,
    columns=_FEATURE_COLUMNS,
    time_semantics="feature data_year is the exact published parent snapshot year",
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_carried_highway_features", "published_snapshots", "features"),
    implementation_id="published_feature_type_h01_v1",
)


BRIDGE_ROUTES = SemanticResource(
    resource_id="v_bridge_routes",
    description="Route rows associated with a published bridge feature snapshot.",
    grain=ResourceGrain.BRIDGE_ROUTE,
    columns=_IDENTITY_COLUMNS
    + (
        _column("feature_type", SemanticDataType.TEXT, False, "Parent SNBI BF01 feature identifier."),
        _column("route_designation", SemanticDataType.TEXT, True, "Route designation within the feature."),
        _column("route_number", SemanticDataType.TEXT, True, "Reported route number."),
        _column("route_direction", SemanticDataType.TEXT, True, "Reported route direction."),
        _column("route_type", SemanticDataType.TEXT, True, "Reported route type."),
        _column("service_type", SemanticDataType.TEXT, True, "Reported service type."),
    ),
    time_semantics="route data_year is the exact published parent snapshot year",
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_bridge_routes", "published_snapshots", "routes"),
    implementation_id="published_route_join_v1",
)


BRIDGE_ELEMENTS = SemanticResource(
    resource_id="v_bridge_elements",
    description="Element condition-state quantities belonging to published snapshots.",
    grain=ResourceGrain.BRIDGE_ELEMENT,
    columns=_IDENTITY_COLUMNS
    + (
        _column("element_number", SemanticDataType.TEXT, False, "SNBI element number."),
        _column("element_parent_number", SemanticDataType.TEXT, True, "Parent element number, when present."),
        _column("element_total_quantity", SemanticDataType.INTEGER, True, "Element total quantity."),
        _column("element_quantity_condition_state_one", SemanticDataType.INTEGER, True, "Quantity in condition state 1."),
        _column("element_quantity_condition_state_two", SemanticDataType.INTEGER, True, "Quantity in condition state 2."),
        _column("element_quantity_condition_state_three", SemanticDataType.INTEGER, True, "Quantity in condition state 3."),
        _column("element_quantity_condition_state_four", SemanticDataType.INTEGER, True, "Quantity in condition state 4."),
    ),
    time_semantics="element data_year is the exact published parent snapshot year",
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_bridge_elements", "published_snapshots", "elements"),
    implementation_id="published_element_join_v1",
)


BRIDGE_WORKS = SemanticResource(
    resource_id="v_bridge_works",
    description="Work-history rows attached to published bridge snapshots.",
    grain=ResourceGrain.BRIDGE_WORK,
    columns=_IDENTITY_COLUMNS
    + (
        _column("year_work_performed", SemanticDataType.INTEGER, True, "Year the work was performed."),
        _column("work_performed", SemanticDataType.TEXT, True, "Reported work code or description."),
    ),
    time_semantics=(
        "data_year identifies the inventory snapshot; year_work_performed identifies the work event"
    ),
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_bridge_works", "published_snapshots", "works"),
    implementation_id="published_work_join_v1",
)


BRIDGE_POSTING_STATUSES = SemanticResource(
    resource_id="v_bridge_posting_statuses",
    description="Load-posting status rows attached to published bridge snapshots.",
    grain=ResourceGrain.POSTING_STATUS,
    columns=_IDENTITY_COLUMNS
    + (
        _column("load_posting_status", SemanticDataType.TEXT, True, "Reported load-posting status."),
        _column(
            "posting_status_change_date",
            SemanticDataType.DATE,
            True,
            "Date on which the posting status changed.",
        ),
    ),
    time_semantics=(
        "data_year identifies the inventory snapshot; posting_status_change_date is an event date"
    ),
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_bridge_posting_statuses", "published_snapshots", "posting_statuses"),
    implementation_id="published_posting_status_join_v1",
)


BRIDGE_CONDITION = SemanticResource(
    resource_id="v_bridge_condition",
    description=(
        "One deterministic condition result per published snapshot using controlling_condition_v1."
    ),
    grain=ResourceGrain.BRIDGE_CONDITION,
    columns=_IDENTITY_COLUMNS
    + (
        _column("bc01", SemanticDataType.NUMBER, True, "Deck condition rating (BC01).", physical_column="deck_condition_rating", unit="rating_0_to_9"),
        _column("bc02", SemanticDataType.NUMBER, True, "Superstructure condition rating (BC02).", physical_column="superstructure_condition_rating", unit="rating_0_to_9"),
        _column("bc03", SemanticDataType.NUMBER, True, "Substructure condition rating (BC03).", physical_column="substructure_condition_rating", unit="rating_0_to_9"),
        _column("bc04", SemanticDataType.NUMBER, True, "Culvert condition rating (BC04).", physical_column="culvert_condition_rating", unit="rating_0_to_9"),
        _column("bc13", SemanticDataType.NUMBER, True, "Stored lowest condition rating (BC13).", physical_column="lowest_condition_rating_code", unit="rating_0_to_9"),
        _derived_column("controlling_rating", SemanticDataType.NUMBER, True, "Selected controlling rating.", unit="rating_0_to_9"),
        _derived_column("condition_class", SemanticDataType.TEXT, True, "Operational Good, Fair, Poor, or unknown class."),
        _derived_column("condition_basis", SemanticDataType.TEXT, False, "Rule branch used to select the rating."),
        _derived_column("condition_conflict", SemanticDataType.BOOLEAN, False, "True when supplied ratings conflict or contain invalid evidence."),
    ),
    time_semantics="condition is evaluated at the exact published data_year",
    scope_fields=_SCOPE_FIELDS,
    physical_relations=("v_bridge_condition", "v_published_bridge_snapshots", "published_snapshots", "bridges"),
    implementation_id="controlling_condition_v1",
)


COUNTY_BRIDGE_INVENTORY = SemanticResource(
    resource_id="v_county_bridge_inventory",
    description=(
        "Published operational inventory counts grouped by workspace, state, county, and exact data year."
    ),
    grain=ResourceGrain.COUNTY_INVENTORY,
    columns=(
        _column("workspace_id", SemanticDataType.TEXT, False, "Authorized workspace scope."),
        _column("state_code", SemanticDataType.INTEGER, False, "State FIPS code."),
        _column("county_code", SemanticDataType.INTEGER, True, "County code within the state."),
        _column("county_name", SemanticDataType.TEXT, True, "Reviewed county name lookup."),
        _column("data_year", SemanticDataType.INTEGER, False, "Exact inventory snapshot year."),
        _derived_column("bridge_count", SemanticDataType.INTEGER, False, "Published bridge snapshot count.", unit="bridges"),
        _derived_column("good_bridge_count", SemanticDataType.INTEGER, False, "Operational Good bridge count.", unit="bridges"),
        _derived_column("fair_bridge_count", SemanticDataType.INTEGER, False, "Operational Fair bridge count.", unit="bridges"),
        _derived_column("poor_bridge_count", SemanticDataType.INTEGER, False, "Operational Poor bridge count.", unit="bridges"),
        _derived_column("unknown_condition_count", SemanticDataType.INTEGER, False, "Count lacking a valid controlling condition.", unit="bridges"),
        _derived_column("total_deck_area", SemanticDataType.NUMBER, True, "Sum of known deck area; null areas are excluded.", unit="square_feet"),
    ),
    time_semantics="every row is an exact data_year aggregate; no implicit latest selection",
    scope_fields=("workspace_id", "state_code", "data_year"),
    physical_relations=(
        "v_county_bridge_inventory",
        "v_bridge_condition",
        "v_published_bridge_snapshots",
        "ref_counties",
        "published_snapshots",
        "bridges",
    ),
    implementation_id="county_published_inventory_v1",
)


_RESOURCES = (
    PUBLISHED_BRIDGE_SNAPSHOTS,
    CURRENT_BRIDGE_SNAPSHOTS,
    BRIDGE_FEATURES,
    CARRIED_HIGHWAY_FEATURES,
    BRIDGE_ROUTES,
    BRIDGE_ELEMENTS,
    BRIDGE_WORKS,
    BRIDGE_POSTING_STATUSES,
    BRIDGE_CONDITION,
    COUNTY_BRIDGE_INVENTORY,
)


def _snapshot_join(
    join_id: str,
    right_resource_id: str,
    cardinality: JoinCardinality,
    description: str,
    *,
    left_resource_id: str = "v_published_bridge_snapshots",
) -> SemanticJoin:
    return SemanticJoin(
        join_id=join_id,
        left_resource_id=left_resource_id,
        right_resource_id=right_resource_id,
        left_fields=_SNAPSHOT_JOIN_FIELDS,
        right_fields=_SNAPSHOT_JOIN_FIELDS,
        cardinality=cardinality,
        description=description,
    )


_JOINS = (
    _snapshot_join(
        "published_to_condition",
        "v_bridge_condition",
        JoinCardinality.ONE_TO_ONE,
        "Attach the single deterministic condition result for the same published snapshot.",
    ),
    _snapshot_join(
        "published_to_features",
        "v_bridge_features",
        JoinCardinality.ONE_TO_MANY,
        "Attach all features for the same published snapshot.",
    ),
    _snapshot_join(
        "published_to_carried_highways",
        "v_carried_highway_features",
        JoinCardinality.ONE_TO_MANY,
        "Attach only carried H01 highway features for the same published snapshot.",
    ),
    _snapshot_join(
        "published_to_elements",
        "v_bridge_elements",
        JoinCardinality.ONE_TO_MANY,
        "Attach elements for the same published snapshot.",
    ),
    _snapshot_join(
        "published_to_works",
        "v_bridge_works",
        JoinCardinality.ONE_TO_MANY,
        "Attach work records for the same published snapshot.",
    ),
    _snapshot_join(
        "published_to_posting_statuses",
        "v_bridge_posting_statuses",
        JoinCardinality.ONE_TO_MANY,
        "Attach posting statuses for the same published snapshot.",
    ),
    _snapshot_join(
        "current_to_condition",
        "v_bridge_condition",
        JoinCardinality.ONE_TO_ONE,
        "Attach condition only for each bridge's independently selected current snapshot.",
        left_resource_id="v_current_bridge_snapshots",
    ),
    _snapshot_join(
        "current_to_carried_highways",
        "v_carried_highway_features",
        JoinCardinality.ONE_TO_MANY,
        "Attach carried highways only for each bridge's independently selected current snapshot.",
        left_resource_id="v_current_bridge_snapshots",
    ),
    SemanticJoin(
        join_id="features_to_routes",
        left_resource_id="v_bridge_features",
        right_resource_id="v_bridge_routes",
        left_fields=_SNAPSHOT_JOIN_FIELDS + ("feature_type",),
        right_fields=_SNAPSHOT_JOIN_FIELDS + ("feature_type",),
        cardinality=JoinCardinality.ONE_TO_MANY,
        description="Attach routes only to their exact published parent feature.",
    ),
)


_FUNCTIONS = (
    SafeFunction("count", "COUNT", 0, 1, True, True, "Count rows or non-null values."),
    SafeFunction("sum", "SUM", 1, 1, True, True, "Sum non-null numeric values."),
    SafeFunction("average", "AVG", 1, 1, True, True, "Mean of non-null numeric values."),
    SafeFunction("minimum", "MIN", 1, 1, True, True, "Minimum non-null value."),
    SafeFunction("maximum", "MAX", 1, 1, True, True, "Maximum non-null value."),
    SafeFunction("round", "ROUND", 1, 2, False, True, "Round a numeric result for display."),
    SafeFunction("coalesce", "COALESCE", 2, None, False, True, "Select the first non-null expression."),
    SafeFunction("nullif", "NULLIF", 2, 2, False, True, "Return null when two expressions are equal."),
    SafeFunction("absolute", "ABS", 1, 1, False, True, "Absolute numeric value."),
    SafeFunction("lower", "LOWER", 1, 1, False, True, "Lowercase text for a bounded comparison."),
    SafeFunction("upper", "UPPER", 1, 1, False, True, "Uppercase text for a bounded comparison."),
)


def build_semantic_catalog(
    *,
    database_identity: str,
    workspace_id: str,
    schema_generation: int,
    data_generation: int,
) -> SemanticCatalog:
    """Build a deterministic catalog keyed by database and workspace revision."""

    return SemanticCatalog(
        definition_version=CATALOG_DEFINITION_VERSION,
        identity=CatalogIdentity(
            database_identity=database_identity,
            workspace_id=workspace_id,
            schema_generation=schema_generation,
            data_generation=data_generation,
        ),
        resources=_RESOURCES,
        joins=_JOINS,
        functions=_FUNCTIONS,
    )


def semantic_catalog_hash(
    *,
    database_identity: str,
    workspace_id: str,
    schema_generation: int,
    data_generation: int,
) -> str:
    return build_semantic_catalog(
        database_identity=database_identity,
        workspace_id=workspace_id,
        schema_generation=schema_generation,
        data_generation=data_generation,
    ).catalog_hash


__all__ = [
    "BRIDGE_CONDITION",
    "BRIDGE_ELEMENTS",
    "BRIDGE_FEATURES",
    "BRIDGE_POSTING_STATUSES",
    "BRIDGE_ROUTES",
    "BRIDGE_WORKS",
    "CARRIED_HIGHWAY_FEATURES",
    "CATALOG_DEFINITION_VERSION",
    "COUNTY_BRIDGE_INVENTORY",
    "CURRENT_BRIDGE_SNAPSHOTS",
    "PUBLISHED_BRIDGE_SNAPSHOTS",
    "build_semantic_catalog",
    "semantic_catalog_hash",
]
