# Bridgette SNBI field guide and 100-question evaluation bank

**Research date:** August 29, 2026
**Scope:** Official FHWA SNBI concepts that are queryable through Bridgette's current reviewed semantic catalog, plus deliberate tests for missing or ambiguous capabilities.
**Evaluation data:** [`bridgette_snbi_question_bank_v1.json`](../backend/app/data/query/bridgette_snbi_question_bank_v1.json)

## Outcome

The evaluation bank contains 100 unique natural-language questions:

| Expected behavior | Count | Meaning |
|---|---:|---|
| Answer | 81 | Bridgette has a reviewed resource and, where aggregation is needed, an approved metric or safe query path. “Current” is resolved by the server-selected latest-inventory scope. |
| Clarification | 14 | Bridgette should ask a focused follow-up because the wording is ambiguous or the required reviewed field, relationship, distinct aggregation, or policy decision is absent. |
| Abstention | 5 | The fact is outside SNBI and Bridgette must not substitute a nearby field or invent an answer. |

The cases cover bridge identity, location, county, owner, age, geometry, span configuration, deck material, wearing surfaces, protective systems, condition ratings, traffic, routes, clearances, navigation, work history, posting, and bridge elements. Each JSON case records the expected behavior, expected response code, required request scope, target resource, target fields, SNBI item IDs, and any known capability gap.

### Latest live evaluation

The completed plan-mode run on August 29, 2026 used Cerebras `gpt-oss-120b`, the real 120-bridge local fixture, and the migrated version-13 SQLite schema:

| Observed behavior | Before fixes | Final code |
|---|---:|---:|
| Direct answer | 33 | 69 |
| Guided clarification | 56 | 26 |
| Honest unsupported response | 5 | 5 |
| Raw error | 6 | 0 |

The final run matched the bank's intended disposition in 88 of 100 cases. The 12 mismatches were intended-answer questions for which the stochastic planner returned `query_needs_more_context`; each response included editable follow-up choices. All 14 intended clarification cases and all five unsupported-fact cases behaved as expected. Representative follow-ups for a ranking, an NHS definition, a multi-county grouping, and a supported-inventory alternative were also submitted and answered directly.

This is a behavior and evidence-path evaluation, not a manual certification of every returned cell. Bridgette's answer verifier kept generated numeric prose tied to query evidence and used deterministic rendering when a narrative claim could not be verified.

## Official-source boundary

The authoritative starting point is the [FHWA SNBI hub](https://www.fhwa.dot.gov/bridge/snbi.cfm), which links the incorporated March 2022 specification, errata, transition crosswalk, schema, and validation materials. The full official field dictionary is the [March 2022 SNBI publication](https://www.fhwa.dot.gov/bridge/snbi/snbi_march_2022_publication.pdf); this guide lists every field exposed to Bridgette, not every field in the complete federal specification.

The [June 2026 Errata #2 redline](https://www.fhwa.dot.gov/bridge/snbi/errata2_to_snbi_march_2022_publication.pdf) says its redlines are informational and do not yet constitute official changes. That distinction matters for proposed codes such as B.SP.09 `C06` for ultra-high-performance concrete. The bank therefore expects Bridgette to request clarification about C06 classification instead of silently adding it to the application's current concrete definition.

The [SNBI JSON schema](https://www.fhwa.dot.gov/bridge/snbi/schema.cfm) represents repeated datasets such as features, routes, spans, work, posting statuses, and elements. A count of rows in one of those datasets is not automatically a count of distinct bridges. The clarification tests deliberately exercise that distinction.

## How to run the bank

For each case:

1. Submit `question` as the user's text.
2. Apply `request_scope` to the HTTP request. A year written only in the question does not change the server-controlled time scope.
3. Compare Bridgette's disposition with `expected_behavior` and `expected_code`.
4. For an answer case, verify that the evidence/plan uses `target_resource`, `target_fields`, and `target_metric` when supplied.
5. Score answer correctness against the loaded workspace data; the bank specifies query intent, not fixed numeric answers.

Suggested scoring dimensions are disposition accuracy, plan/resource accuracy, numeric or row accuracy, source-field faithfulness, time-scope accuracy, and whether clarifications give a useful next step.

## Time and counting semantics

| Term | Required interpretation |
|---|---|
| Latest/current | Select each bridge's own latest published snapshot within the authorized workspace and state. Do not select only the database-wide maximum year. |
| Exact year | Use published snapshots whose `data_year` equals the supplied year. |
| Trend | Use the explicitly supplied start and end years; do not infer a range from question text alone. |
| Bridge count | Count bridge-snapshot rows at bridge grain, or bridge rows at current-bridge grain. |
| Child-row count | Features, routes, spans, works, posting statuses, and elements are many-to-one with a bridge. Count distinct bridge identifiers when the user asks for bridges rather than records. |
| Age at snapshot | `data_year - year_built`; B.W.01 remains the original construction year and is not replaced by a rehabilitation year. |
| Known-value total | Null values are excluded. The answer should disclose this when it materially changes the denominator. |

## Bridgette-exposed field dictionary

### Shared identity and publication fields

| Field | Meaning |
|---|---|
| `workspace_id` | Server-authorized workspace; never user-selectable SQL scope. |
| `snapshot_revision_id` | Immutable canonical revision behind the published snapshot. |
| `state_code` | State FIPS code. |
| `bridge_number` | Agency bridge identifier; leading zeros are significant (B.ID.01). |
| `data_year` | Exact inventory snapshot year. |
| `published_at` | Timestamp when the canonical revision was published. |

### Bridge snapshot fields

Available from `v_published_bridge_snapshots` for exact/trend work and `v_current_bridge_snapshots` for per-bridge latest work.

| Field | SNBI concept | Meaning/unit |
|---|---|---|
| `bridge_name` | B.ID.02 | Bridge or structure name. |
| `county_code` | B.L.02 | County code within the state. |
| `bridge_location` | B.L.11 | Inventory location description. |
| `latitude`, `longitude` | B.L.05, B.L.06 | Decimal-degree coordinates. |
| `owner` | B.CL.01 | Inventory owner code. |
| `maintenance_responsibility` | B.CL.02 | Maintenance-responsibility code. |
| `year_built` | B.W.01 | Original construction year. Rehabilitation and widening do not reset it. |
| `total_bridge_length` | B.G.02 | Total bridge length, feet. |
| `maximum_span_length` | B.G.03 | Maximum span length, feet. |
| `bridge_width_out_to_out` | B.G.05 | Out-to-out bridge width, feet. |
| `calculated_deck_area` | B.G.16 | Reported or transition-calculated deck area, square feet. |
| `nhs_designation` | NHS concept | Root-level value; feature-level B.H.03 is preferred for roadway questions. |
| `deck_condition_rating` | B.C.01 | Deck rating, 0–9 when applicable. |
| `superstructure_condition_rating` | B.C.02 | Superstructure rating, 0–9 when applicable. |
| `substructure_condition_rating` | B.C.03 | Substructure rating, 0–9 when applicable. |
| `culvert_condition_rating` | B.C.04 | Culvert rating, 0–9 when applicable. |
| `lowest_condition_rating_code` | B.C.13 | Stored lowest-condition rating when valid. |

### Feature and carried-highway fields

`v_bridge_features` contains all published feature rows. `v_carried_highway_features` is restricted to the H01 carried-highway feature and should be used for carried-route traffic, lanes, NHS, and roadway-clearance questions.

| Field | SNBI concept | Meaning/unit |
|---|---|---|
| `feature_type` | B.F.01 | Feature identifier such as H01. |
| `feature_location` | B.F.02 | Whether the feature is carried on or located below the bridge. |
| `feature_name` | B.F.03 | Feature name. |
| `functional_classification` | B.H.01 | Highway functional-class code. |
| `urban_code` | B.H.02 | Urban-area code. |
| `nhs_designation` | B.H.03 | Feature-level National Highway System designation. |
| `national_highway_freight_network` | B.H.04 | National Highway Freight Network designation. |
| `strahnet_designation` | B.H.05 | Strategic Highway Network designation. |
| `lrs_route_id`, `lrs_mile_point` | B.H.06, B.H.07 | Linear-referencing route identifier and mile point. |
| `lanes_on_highway` | B.H.08 | Number of highway lanes. |
| `annual_average_daily_traffic` | B.H.09 | AADT, vehicles per day. |
| `annual_average_daily_truck_traffic` | B.H.10 | AADTT for truck classes 4–13, trucks per day. |
| `year_of_annual_average_daily_traffic` | B.H.11 | Year represented by the traffic observation. |
| `highway_max_usable_vertical_clearance` | B.H.12 | Maximum usable highway vertical clearance, feet. |
| `highway_min_vertical_clearance` | B.H.13 | Minimum highway vertical clearance, feet. |
| `highway_min_horizontal_clearance_left`, `..._right` | B.H.14, B.H.15 | Minimum left/right horizontal clearance, feet. |
| `highway_max_usable_surface_width` | B.H.16 | Maximum usable roadway surface width, feet. |
| `bypass_detour_length` | B.H.17 | Bypass/detour length, miles. |
| `crossing_bridge_number` | B.H.18 | Related crossing bridge identifier. |
| `railroad_service_type` | Railroad feature | Railroad service type. |
| `railroad_min_vertical_clearance` | Railroad feature | Minimum railroad vertical clearance, feet. |
| `railroad_min_horizontal_offset` | Railroad feature | Minimum railroad horizontal offset, feet. |
| `navigable_waterway` | Navigation feature | Navigable-waterway designation. |
| `navigable_min_vertical_clearance` | Navigation feature | Minimum navigation vertical clearance, feet. |
| `movable_bridge_max_navigation_vertical_clearance` | Navigation feature | Maximum clearance when a movable bridge is open, feet. |
| `navigation_channel_width` | Navigation feature | Navigation channel width, feet. |
| `navigation_channel_min_horizontal_clearance` | Navigation feature | Minimum channel horizontal clearance, feet. |
| `substructure_navigation_protection` | Navigation feature | Substructure navigation-protection designation. |

### Route fields

Routes in `v_bridge_routes` belong to features and are not one-to-one with bridges.

| Field | SNBI concept | Meaning |
|---|---|---|
| `feature_type` | B.F.01 | Identifier of the parent feature. |
| `route_designation` | B.RT.01 | Route identifier within that feature. |
| `route_number` | B.RT.02 | Reported route number. |
| `route_direction` | B.RT.03 | Reported route direction. |
| `route_type` | B.RT.04 | Route type/signing class. |
| `service_type` | B.RT.05 | Designated service type. |

### Span and deck fields

`v_bridge_span_sets` is at span-configuration grain.

| Field | Item | Meaning |
|---|---|---|
| `span_configuration_designation` | B.SP.01 | Span-configuration identifier. |
| `number_of_spans` | B.SP.02 | Number of spans in the configuration. |
| `number_of_beam_lines` | B.SP.03 | Number of beam or girder lines. |
| `span_material` | B.SP.04 | Main span material code. |
| `span_continuity` | B.SP.05 | Continuity code. |
| `span_type` | B.SP.06 | Structural span-type code. |
| `span_protective_system` | B.SP.07 | Span protective-system code. |
| `deck_interaction` | B.SP.08 | Deck/structural interaction code. |
| `deck_material_and_type` | B.SP.09 | Deck material and construction type. |
| `wearing_surface` | B.SP.10 | Wearing-surface material/type. |
| `deck_protective_system` | B.SP.11 | Deck protective system. |
| `deck_reinforcing_protective_system` | B.SP.12 | Reinforcing-bar protective system. |
| `deck_stay_in_place_forms` | B.SP.13 | Stay-in-place form material/type. |

### Concrete deck codes used by Bridgette

| B.SP.09 code | Meaning | Included in current `has_concrete_deck` |
|---|---|---:|
| `C01` | Reinforced concrete, cast in place | Yes |
| `C02` | Reinforced concrete, precast | Yes |
| `C03` | Prestressed concrete, pre-tensioned | Yes |
| `C04` | Prestressed concrete, cast-in-place post-tensioned | Yes |
| `C05` | Prestressed concrete, precast post-tensioned | Yes |
| `CX` | Other concrete | Yes |
| `CP-T`, `CR-T` | Temporary concrete transition codes accepted by the local transition model | Yes |
| `C06` | UHPC proposed in informational Errata #2 | No; requires an explicit policy/code update |
| `0` | No deck component | No |

The deck summary resources collapse multiple span rows to one bridge snapshot. `has_concrete_deck` is true when any span set has one of the reviewed concrete codes. Percent metrics use all selected bridge snapshots as the denominator unless the user explicitly asks for known deck materials only.

| Deck-summary field | Meaning |
|---|---|
| `span_set_count` | Number of span-configuration rows for the bridge snapshot. |
| `known_deck_material_span_count` | Span rows having a nonblank B.SP.09 value other than `0`. |
| `concrete_deck_span_count` | Span rows whose B.SP.09 is a reviewed concrete code. |
| `has_concrete_deck` | True when at least one span row is concrete. |
| `has_known_deck_material` | True when at least one span row reports a nonzero deck material. |

B.SP.10 also distinguishes wearing surfaces, including `B01` bituminous/asphalt and multiple concrete surface codes. B.SP.11–B.SP.13 describe deck protection, reinforcing protection, and stay-in-place forms; these must not be treated as the deck's structural material.

### Condition fields

`v_bridge_condition` exposes B.C.01–B.C.04, B.C.13, and four reviewed derived fields:

| Field | Meaning |
|---|---|
| `bc01`, `bc02`, `bc03`, `bc04`, `bc13` | Deck, superstructure, substructure, culvert, and stored lowest ratings. |
| `controlling_rating` | Rating selected by the application's reviewed controlling-condition rule. |
| `condition_class` | Operational class: Good = 7–9, Fair = 5–6, Poor = 0–4, otherwise unknown. |
| `condition_basis` | Rule branch used to select the controlling rating. |
| `condition_conflict` | True when supplied ratings conflict or contain invalid evidence. |

These operational classes are suitable for inventory counts. They must not be confused with federal TPM deck-area percentages, which use separate definitions and are currently disabled in Bridgette.

### Work history

`v_bridge_works` exposes `year_work_performed` (B.W.02), the event year, and `work_performed` (B.W.03), the reported code or code list. B.W.01 remains the original construction year.

| Code family | Meaning |
|---|---|
| `BR1` | Bridge replaced. |
| `IP1`–`IP5` | Widened/extended, raised, strengthened, seismic retrofit, or lengthened. |
| `DK1`, `SP1`, `SB1` | Deck, superstructure, or substructure replaced. |
| `DK2`, `SP2`, `SB2`, `CU2` | Major rehabilitation of deck, superstructure, substructure, or culvert. |
| `DK3`, `SP3`, `SB3`, `CU3` | Minor rehabilitation. |
| `DK4`, `CU4` | Overlay. |
| `DK5`, `SP5`, `SB5`, `CU5` | Sealed. |
| `DK6`, `SP6`, `SB6`, `CU6` | New or replaced coating. |
| `DK7`, `SP7`, `SB7`, `CU7` | Existing coating preserved. |
| `BG1`, `JT1`, `RT1`, `SC1`, `CP1` | Bearing, joint, railing, scour countermeasure, or cathodic protection installed/replaced. |
| `BG2`, `JT2`, `RT2`, `SC2`, `CP2` | Corresponding component repaired. |
| `CH1` | Condition improved. |

A single bridge can have multiple work rows and multiple codes. Questions asking “how many distinct bridges ever had major rehabilitation” require an approved distinct-bridge/event interpretation, which is intentionally a clarification case today.

### Posting status and elements

`v_bridge_posting_statuses` exposes `load_posting_status` (B.PS.01), the reported open/posted/restricted/closed status, and `posting_status_change_date` (B.PS.02), the date that status changed. Posting rows are events; counting them does not equal counting distinct posted bridges.

| Element field | SNBI concept | Meaning |
|---|---|---|
| `element_number` | B.E.01 | Element identifier. |
| `element_parent_number` | B.E.02 | Parent element identifier when the element is a child/protective system. |
| `element_total_quantity` | B.E.03 | Total element quantity in its prescribed unit. |
| `element_quantity_condition_state_one` | B.CS.01 | Quantity in condition state 1. |
| `element_quantity_condition_state_two` | B.CS.02 | Quantity in condition state 2. |
| `element_quantity_condition_state_three` | B.CS.03 | Quantity in condition state 3. |
| `element_quantity_condition_state_four` | B.CS.04 | Quantity in condition state 4. |

Element reporting is required for NHS bridges and optional for non-NHS bridges, so a statewide element denominator can be incomplete outside the NHS population.

### County aggregate

`v_county_bridge_inventory` does not select current snapshots implicitly.

| Field | Meaning |
|---|---|
| `workspace_id`, `state_code`, `data_year` | Authorized workspace/state and exact inventory year. |
| `county_code`, `county_name` | County FIPS code within the state and reviewed county-name lookup. |
| `bridge_count` | Published bridge snapshots in the county/year. |
| `good_bridge_count`, `fair_bridge_count`, `poor_bridge_count` | Operational condition-class counts. |
| `unknown_condition_count` | Count without a valid controlling condition. |
| `total_deck_area` | Sum of known calculated deck area; null areas are excluded. |

## Approved aggregate metrics

| Metric | Meaning |
|---|---|
| `inventory.bridge_count` | Count of bridge-grain rows. |
| `inventory.concrete_deck_bridge_count`, `...percent` | Exact/trend bridge-level concrete count or percent. |
| `inventory.current_concrete_deck_bridge_count`, `...percent` | Per-bridge latest concrete count or percent. |
| `inventory.good_bridge_count`, `fair_bridge_count`, `poor_bridge_count`, `unknown_condition_count` | Operational controlling-condition counts. |
| `inventory.good_bridge_percent`, `fair_bridge_percent`, `poor_bridge_percent` | Operational class percentages. |
| `inventory.average_age_at_snapshot` | Mean of `data_year - year_built` for known valid years. |
| `inventory.average_daily_traffic` | Mean AADT across selected feature observations. |
| `inventory.total_bridge_length` | Sum of known bridge lengths. |
| `inventory.total_deck_area` | Sum of known calculated deck areas. |

`inventory.average_current_age` exists internally but requires a server-supplied reference year and is not currently usable by the planner. `federal.good_deck_area_percent` and `federal.poor_deck_area_percent` are deliberately disabled pending an approved federal TPM implementation.

## Known gaps exercised by the bank

The 14 clarification cases cover:

- proposed C06/UHPC classification;
- distinct bridges across repeated work, posting, and element rows;
- missing approved average BC01/BC02 aggregate metrics;
- inspection due dates, substructure material, and legal load-rating fields absent from the semantic catalog;
- disabled federal TPM deck-area percentages;
- ambiguous “NHS” and “bad condition” meanings (the selected latest scope resolves “current”);
- multi-county comparison scope; and
- unresolved named-bridge identity.

The five abstention cases cover original paint color, current toll price, crashes, live traffic speed/congestion, and current weather. Those are not SNBI inventory facts.

## Official references

- [FHWA SNBI resource hub](https://www.fhwa.dot.gov/bridge/snbi.cfm)
- [Specifications for the National Bridge Inventory, March 2022](https://www.fhwa.dot.gov/bridge/snbi/snbi_march_2022_publication.pdf)
- [SNBI Errata #2, June 2026](https://www.fhwa.dot.gov/bridge/snbi/errata2_to_snbi_march_2022_publication.pdf)
- [NBI-to-SNBI Data Crosswalk](https://www.fhwa.dot.gov/bridge/snbi/datacrosswalk.cfm)
- [Detailed Code Mapping](https://www.fhwa.dot.gov/bridge/snbi/codemapping.cfm)
- [SNBI JSON Submittal Schema](https://www.fhwa.dot.gov/bridge/snbi/schema.cfm)
- [SNBI Data Validation Logic](https://www.fhwa.dot.gov/bridge/snbi/data_validation_logic.cfm)
- [FHWA 2025 NBI download page](https://www.fhwa.dot.gov/BRIDGE/nbi/ascii2025.cfm)
