# Forecasting, LCCA, and KPI hardening implementation plan

**Prepared:** August 14, 2026  
**Revised:** August 14, 2026 — forecast-to-LCCA coupling, historical ground-truth hindcasting, bridge life-history evidence, and annual rehabilitation simulation  
**Primary input:** [`forecasting-lcca-kpis-research-report.md`](forecasting-lcca-kpis-research-report.md)  
**Implementation order:** forecasting, then LCCA, then KPIs  
**Status:** core research implementation completed through the PR14 coherence boundary on August 14, 2026. External model qualification, NJDOT measure/cost/treatment approval, known-year FHWA comparison, and a schema-v10 production-sized deployment rehearsal remain release gates.

## Implementation checkpoint

Implemented code now includes the shared published analytical projection, annual forecast and exact ground-truth hindcasts, immutable forecast packages/runs, forecast-driven annual LCCA alternatives and rehab controls, operational/program/FHWA-diagnostic KPI services, governed program import/approval, deterministic KPI packages, exact-year UIs, cross-client generation invalidation, and commercial retirement of the raw analytical endpoints.

The implementation deliberately does **not** claim that a locally fitted research model is released, that research cost/treatment effects are agency-approved, that the candidate NJDOT KPI definitions are official, or that the FHWA diagnostic is submission-ready. Those states require the external receipts listed in this document and in `hardening-implementation-status.md`.

## 1. Outcome

This work will replace the current loosely connected forecasting, LCCA, and KPI paths with three versioned analytical products that read the same immutable published inventory evidence. **The forecast is the state-transition engine for LCCA**: LCCA may not invent a separate deterioration curve or rehabilitation recovery assumption.

1. **Forecasting:** a reproducible, workspace-scoped, action-aware condition forecast with corrected feature joins, explicit fallback evidence, full state distributions, historical hindcasts against later observed inspections, treatment-effect evidence, and durable runs.
2. **LCCA:** a planning-level, year-by-year simulation driven by an approved forecast run. It compares multiple named rehabilitation strategies using component-specific actions, forecasted deterioration and recovery, an auditable annual ledger, consistent real/nominal economics, equivalent annual cost, residual value, and sensitivity analysis.
3. **KPIs:** separate panels for operational inventory condition, NJDOT-style acceptable-condition and program-delivery KPIs, an age-by-condition deck-area view, and a separately labeled FHWA TPM diagnostic.

All three products will return the same source identity: workspace, state, snapshot year or as-of rule, published revision IDs, `data_generation`, schema generation, analytical specification version, and content hashes.

The analytical flow is therefore fixed:

`published observations -> historical work/treatment evidence -> forecast fit -> historical hindcast gate -> approved forecast version -> annual LCCA simulation -> scenario KPIs`

An LCCA result is invalid if its forecast version, evaluation receipt, treatment library, or input revision identity is missing or stale.

## 2. Current-state reconciliation

The research report audited revision `8f4d002`. The repository has since gained important foundations, but the old analytical code has not been migrated to them.

| Area | Current repository status | Plan decision |
|---|---|---|
| Canonical revisions | Immutable `snapshot_revisions`, `published_snapshots`, validation evidence, and `workspace_state` exist | Reuse; analytics read published revisions only |
| Cross-client freshness | A singleton frontend data-generation observer exists | Reuse; add exact generation/hash to every analytical response |
| Published semantic views | Published/current bridge, feature, carried-feature, condition, county, route, work, and element views exist | Extend into one analytical bridge-year projection |
| Long-running jobs | Durable operation jobs, ownership, quotas, progress, cancellation, and artifact receipts exist | Reuse for model fit, evaluation, and probabilistic LCCA |
| Forecast model | Legacy `markov.py` reads raw `bridges`, uses parent traffic columns, coarse cache identity, raw empirical transitions, and incomplete provenance | Replace behind a compatibility adapter |
| Forecast persistence | Separate mutable `training_series`, run, and prediction tables are not tied to published revisions | Replace with immutable workspace/revision-bound runs |
| Forecast evaluation | One fixed-cutoff pooled holdout; no equal-row persistence skill gate | Replace with rolling-origin evaluation |
| Forecast UI | Still says Prophet and calls state dispersion “confidence”; simulation GET may rebuild shared data | Rewrite claims and make GET paths read-only |
| LCCA | Two hard-coded policies; deck recovery applied to every controller; latest raw bridge row; manual NHS; incomplete area logic; hard-coded costs | Replace with versioned multi-alternative engine |
| KPI backend | Legacy pandas functions read raw tables and wall-clock age; all-feature max AADT; incomplete deck area; null counties can disappear | Replace with SQL/repository calculations on published snapshots |
| KPI UI | Operational count dashboard exists and now refetches on data-generation change | Retain useful count view; add accurate labels and new KPI families |
| Federal measure support | Not implemented | Add a separate diagnostic, never silently convert the operational dashboard |

The migration will not make forecast results observations, LCCA actions work records, or KPI totals official submissions.

## 3. Non-negotiable analytical contracts

### 3.1 One source population

Create `backend/app/services/analytics/` with:

- `identity.py`: immutable `AnalysisSnapshotIdentity` containing workspace, data/schema generation, state, exact year/as-of rule, published revision-set hash, row count, and created timestamp;
- `projection.py`: repository for one row per published bridge-year plus deterministic child-feature selection;
- `condition.py`: shared rating parsing, condition controller, condition class, basis, conflict, and invalid-value evidence;
- `deck_area.py`: purpose- and data-era-specific deck-area calculation with `Decimal` half-up rounding and exclusion reasons;
- `specifications.py`: versioned population, metric, bin, and threshold definitions;
- `errors.py`: typed, path-free errors.

The projection will expose at least:

- workspace/revision identity, state, bridge, year, county, owner, and maintenance responsibility;
- BW01 year built and age at the snapshot year;
- BG01, BG02, BG05, BG09, BG15, and BG16 geometry;
- canonical deck, wearing-surface, superstructure, substructure, culvert, joint, bearing, coating, and structural-system material/type codes needed to determine treatment compatibility, preserving both code and decoded label;
- BC01 through BC04 plus BC12/BC13 when reported, recomputed controller, basis, conflict, and class;
- exactly selected carried-highway feature evidence: BF01, BF02, BH03, BH09, BH10, feature count, and ambiguity status;
- same-year work evidence and its component/action classification when known;
- ordered bridge-life history: construction/reconstruction evidence, annual inspections, observed work, inventory-reported work, and explicitly labeled condition-change candidates;
- data-quality and exclusion codes rather than silently substituted zeros.

For multiple qualifying carried features, the projection will not use an arbitrary `MAX`. It will return a deterministic selection only under a reviewed rule and otherwise mark the record ambiguous. Forecasting may use a configured aggregate; NHS eligibility will use existential feature logic; both choices remain visible.

### 3.2 Read-only analytics

- Viewer GET requests never rebuild training data, mutate caches on disk, or promote a model.
- Build/evaluate/run operations are editor-authorized durable POST jobs.
- All analytical tables are append-only or lifecycle-controlled. Observed inventory remains separate.
- A data-generation change makes prior runs `stale`, not deleted. Stale results remain reproducible but are never returned as current without an explicit run ID.
- Commercial mode rejects legacy raw-table analytical fallbacks.

### 3.3 Shared classifications

One shared function will validate ratings as integer values 0 through 9 and return:

- Good: 7–9;
- Upper Fair: 6;
- Lower Fair: 5;
- Fair: 5–6;
- Poor: 0–4;
- Unknown: no usable condition;
- Invalid/conflict: retained as evidence, not coerced to Good or Poor.

The four-way Good / Upper Fair / Lower Fair / Poor split is an NJDOT portfolio visualization requested from the supplied example. It is not a new federal classification.

### 3.4 Forecast-to-LCCA contract

The forecast service exposes two related but separately evaluated transition families:

- **no-action deterioration:** the annual component-state distribution when no capital rehabilitation is applied;
- **action-conditioned transition:** the annual post-treatment state/effect distribution and subsequent deterioration for one reviewed treatment, component, material/structure family, and starting condition.

Each transition returns a full probability vector, expected rating, uncertainty, effective sample/exposure, cohort/fallback level, applicable bridge attributes, and model/evidence version. An action effect may be deterministic only when a reviewed engineering rule explicitly defines it; otherwise it is a distribution or unavailable.

LCCA consumes this service once per simulated bridge-year. It cannot copy transition matrices, hard-code a rating reset, or use a second forecast implementation. A released LCCA run must reference:

- the exact forecast model version and historical evaluation receipt;
- the no-action and every action-conditioned transition specification used;
- the bridge's exact published observation history and work-history receipt;
- the treatment/cost libraries and scenario configuration hashes.

Research users may explicitly run an unqualified forecast, but the UI and export must label the resulting LCCA `forecast_unvalidated`. Commercial planning defaults fail closed unless the forecast meets its approved release gate.

### 3.5 Observed, inferred, and simulated work are different evidence classes

Build one immutable bridge life-history ledger from construction year through the latest published year, but never pretend that annual inventory files contain a complete maintenance history:

- `observed_work`: an authoritative dated NJDOT work order, contract, inspection-linked repair record, or explicit source work field;
- `inventory_reported_work`: a dated work indication present in an annual NBI/SNBI record, with its exact source limitations;
- `condition_change_candidate`: a rating increase that may indicate work, data correction, inspector variability, or replacement and therefore cannot be treated as observed rehabilitation;
- `simulated_work`: an action created only inside a named LCCA scenario.

Every ledger entry records component/scope, treatment code, event year/date precision, cost if known, before/after observations, source artifact/record pointers, confidence/evidence class, and supersession status. If historical work records are unavailable between construction and the first retained annual file, the interval is visibly `history_unavailable`; the system does not manufacture a maintenance schedule for it.

## 4. Pull-request sequence

Each PR must be independently reviewable, migration-safe, and green before the next PR changes production behavior.

## PR 0 — Freeze analytical contracts with red tests

**Goal:** encode the required behavior before replacing legacy code.

**New tests**

- `backend/tests/test_analytics_projection_contract.py`
- `backend/tests/test_forecast_v2_contract.py`
- `backend/tests/test_forecast_evaluation_contract.py`
- `backend/tests/test_bridge_life_history_contract.py`
- `backend/tests/test_treatment_library_contract.py`
- `backend/tests/test_forecast_lcca_integration_contract.py`
- `backend/tests/test_lcca_v2_contract.py`
- `backend/tests/test_operational_kpis_contract.py`
- `backend/tests/test_program_kpis_contract.py`
- `backend/tests/test_fhwa_tpm_diagnostic_contract.py`
- `backend/tests/test_analytics_http_contract.py`
- `backend/tests/test_analytics_frontend_contract.py`

**Cases frozen immediately**

- same bridge/year in two workspaces cannot cross;
- staged/unpublished revisions are invisible;
- exact-year and latest-per-bridge behavior differ intentionally;
- carried H01/H## joins use the same state, bridge, year, and revision population;
- invalid ratings, missing geometry, null counties, duplicate features, and stale generations fail or report evidence predictably;
- GET simulation is read-only;
- all three analytical products expose matching source identity;
- a historical evaluation origin cannot see later observations or later work records;
- a forecast from a past origin is scored against the exact later published inspection that serves as ground truth;
- LCCA rejects a missing/stale forecast or silently substituted transition/effect model;
- observed, inventory-reported, inferred, and simulated work never share the same evidence label;
- legacy route response compatibility is documented, not assumed.

**Acceptance:** tests collect and fail only because new modules/APIs do not yet exist.

## PR 1 — Build the published analytical projection

**Goal:** provide one correct, indexed input surface for forecasting, LCCA, and KPIs.

**Files**

- new `backend/app/services/analytics/*` modules from Section 3;
- extend `backend/app/routers/services/db_schema.py` and `db_migrations.py` using the next available migration number after the current schema version;
- add/extend checksummed views such as `v_analytics_bridge_year`, `v_analytics_carried_features`, and `v_analytics_work_events`;
- add projection indexes only after production-shaped query-plan tests.

**Rules**

- source is `published_snapshots` joined to the exact same-year projection rows;
- workspace is mandatory;
- age is `data_year - BW01`, never wall-clock age for a historical snapshot;
- county name comes from `ref_counties`, with a retained Unknown bucket;
- component ratings are parsed once with complete status evidence;
- feature ambiguity and missingness are explicit;
- projection hash includes ordered revision IDs and analytical specification hash.
- material/type codes are decoded through the pinned SNBI/NBI dictionary and code-map receipt; unknown codes remain unknown and cannot be routed to a guessed treatment.

**Tests**

- golden all-field bridge-year fixture;
- multiple years, workspaces, carried/crossed features, ramps, culverts, border record, and null county;
- projection/explain-plan performance on a production-shaped fixture;
- schema/view checksum drift and migration rollback;
- source generation changes produce a new projection identity.
- life-history event ordering, duplicate/superseded work records, same-year action/inspection ordering, unknown history before the first retained record, and cross-source identity conflicts.

**Acceptance:** forecast, LCCA, and KPI repositories can obtain all required facts without querying raw tables independently.

## PR 2 — Correct and version the Markov forecast core

**Goal:** implement an explainable, annual, action-aware `markov-v2` estimator whose effective inputs match its claims and whose released transitions are the only deterioration/recovery engine used by LCCA.

**Files**

- new `backend/app/services/forecasting_v2/models.py`
- `estimator.py`, `transitions.py`, `projection.py`, `treatment_effects.py`, `recovery.py`, `serialization.py`
- retain `routers/services/markov.py` only as a temporary adapter.

**Corrections**

1. Read BH09/BH10 from the selected same-year carried feature, not empty parent columns.
2. Include workspace and state in all cohort keys; county keys are `(state, county)`.
3. Deduplicate fallback keys when traffic or age is missing.
4. Use metadata available at each interval origin; do not apply the final row’s county/traffic backward through history.
5. Retain each component’s true last-observation year and explicitly advance it to a common forecast as-of year.
6. Keep missing/invalid ratings out of training and record exclusion counts.
7. Version the multi-year interval allocation rule and test long gaps; do not imply reconstructed inspection dates.
8. Distinguish work by action/component when evidence allows; unknown-scope work excludes the interval with an explicit reason.
9. Return the selected fallback level, exposure, transition counts, and prior/smoothing information for every forecast step.
10. Use a documented smoothing/shrinkage profile rather than unqualified raw zero-frequency estimates; preserve the raw-frequency implementation as a comparator.
11. Retain the complete 10-state probability vector per component/year.
12. Keep state 0 absorbing and enforce probability sum, finiteness, and 0–9 bounds.
13. Fit a no-action model from intervals without usable work evidence and keep contaminated/unknown-work intervals out of that fit.
14. Fit treatment effects only from sufficiently identified before/action/after sequences; stratify by component, treatment, material/structural family, starting state, and other reviewed covariates when exposure supports them.
15. Apply minimum exposure and overlap checks. If the treatment cohort is sparse or materially different from the requested bridge, return a broader reviewed fallback with its evidence or `treatment_effect_unavailable`—never a confident rating reset.
16. Represent a treatment as an immediate or phased state transition followed by the same annual deterioration kernel. Preserve the full distribution and treatment-effect uncertainty.
17. Distinguish replacement/reconstruction from rehabilitation and preservation. Replacement creates a reviewed new-component/structural-age state; it does not rewrite the observed historical record.
18. Produce one-year transitions as the primitive operation. Multi-year forecasts and LCCA are repeated applications, so annual actions, costs, and triggers cannot be skipped inside a coarse step.

**Forecast modes**

- `no_action`: deterioration only, used for the LCCA baseline;
- `observed_history_replay`: use only work known by each historical origin, used for hindcast validation;
- `reviewed_policy`: apply an approved condition/cycle-triggered treatment library;
- `explicit_schedule`: apply only user-entered reviewed actions in specified years.

The forecast response identifies the mode. A chart or LCCA result may not merge modes without showing the change point and action evidence.

**Cache**

- bounded LRU keyed by workspace, published revision-set hash/data generation, training cutoff, estimator configuration hash, and implementation version;
- no key based only on database row count and maximum year;
- no path or mutable closure in persisted artifacts.

**Acceptance:** traffic bands are measurably populated on the pinned NJ fixture, no cohort exposure is doubled, no-action and action-conditioned effects are separately estimable/auditable, annual probability mass is conserved after interventions, and every forecast is reproducible from its source/configuration hashes.

## PR 3 — Durable forecast datasets, runs, and APIs

**Goal:** replace the mutable side database workflow with immutable source-bound runs.

**Schema**

- `forecast_datasets`: source identity, cutoff, population/config hashes, counts, completeness profile;
- `forecast_model_versions`: engine, implementation/config hash, training receipt, status;
- `forecast_treatment_models`: model version, treatment/component/material applicability, effect profile/distribution, exposure, overlap/fallback evidence, and release status;
- `forecast_runs`: workspace, dataset/model IDs, forecast mode, policy/schedule hash, horizon, target population, lifecycle, actor, timestamps;
- `forecast_predictions`: run, bridge revision, component, forecast year, pre-action distribution, action reference, post-action distribution, expected rating, dispersion, quantiles, probability vector, fallback evidence;
- `forecast_hindcast_predictions`: evaluation origin, only-as-of source receipt, exact target observation/revision, predicted distribution, observed rating, error, action-contamination/evidence class;
- `forecast_run_issues`: exclusions/warnings;
- immutable triggers and workspace-scoped foreign keys.

Do not store absolute source paths. Add these tables to backup, retention, deployment-binding, and migration-rehearsal manifests.

**Jobs and routes**

- `POST /api/forecast-jobs` with idempotency key and expected data generation;
- `GET /api/forecast-jobs/{id}` and `/result` owner-scoped;
- `POST /api/forecast-jobs/{id}/cancel`;
- `GET /api/forecasts/runs`, `/runs/{id}`, and exact bridge/component results;
- old `/api/forecasts/*` reads adapt to the latest non-stale v2 run for one release;
- old build/train mutations return deprecation metadata, then 410 in commercial mode.

**Behavior**

- operation jobs perform dataset materialization and model fitting;
- model publication requires the immutable hindcast/evaluation receipt from PR 4;
- stale-generation checks occur before work and before publication;
- interrupted jobs cannot publish partial predictions;
- forecast generation never updates `bridges` or `training_series`;
- no viewer route invokes `ensure_training_data`.
- LCCA obtains transitions through an internal typed forecast API keyed by model/evaluation version; it cannot read forecast tables and reinterpret them independently.

**Acceptance:** restart-safe job recovery, idempotent replay, exact cancellation, atomic result publication, stale-run detection, and cross-workspace non-enumeration all pass.

## PR 4 — Rolling-origin forecast evaluation and release gate

**Goal:** run genuine historical hindcasts: stop the database at a past as-of date, forecast forward without seeing the future, and compare the predicted component distributions with the later published inspection ratings that are the ground truth for that test.

**Evaluation design**

- multiple fixed historical origins selected by a sealed manifest;
- training, cohort selection, material/traffic/county values, treatment history, costs, and configuration use only evidence available at or before each origin;
- exact 1-, 2-, 3-, 5-, 10-, and configurable longer-year lead-time buckets;
- select each target by immutable later published revision and actual observation year; never use a reconstructed interpolation as ground truth;
- candidate and baseline score the identical bridge/component/target-revision rows;
- preserve origin observation, predicted probability vector/mean/quantiles, target observation, error, and inclusion/exclusion reason for every row;
- separate no-recorded-work, observed-treatment, inventory-reported-treatment, uncertain-condition-jump, replacement, and unknown-history results;
- evaluate treatment effects only where treatment identity, timing, scope, and before/after observations meet the approved evidence rule;
- score one-step forecasts and recursively simulated annual paths separately so error accumulation is visible;
- include a frozen final confirmation period that is never used for binning, smoothing, treatment-effect, threshold, or model selection;
- group bootstrap by bridge identity;
- stratify by component, starting rating, age, county, traffic, and fallback level.

**Metrics**

- `n`, distinct bridges, MAE, RMSE, median absolute error, bias;
- persistence and fleet/age-component baselines;
- MAE skill and MASE where defined;
- ranked probability score for ordinal distributions;
- interval/quantile coverage and sharpness;
- per-rating confusion/calibration, predicted probability assigned to the observed rating, and forecast-versus-observed change direction;
- treatment-effect error: predicted versus observed post-treatment rating/change and durability at each available follow-up year;
- exclusion and missing-target counts.

**Comparators**

- persistence is mandatory;
- raw-frequency and smoothed Markov are mandatory;
- semi-Markov/survival, ordinal regression, and a tree-based method remain research candidates behind the same evaluator;
- tune bins/exposure/prior values only inside training folds.

**Gate**

Create a versioned `forecast_release_policy.json`. Thresholds must be approved before looking at final confirmation results. The policy separately gates no-action deterioration and each action-conditioned treatment model. A treatment that lacks adequate historical ground truth may remain UI-selectable only as an explicitly deterministic engineer assumption or research sensitivity; it is not labeled learned/validated. Until a model has positive agreed skill and acceptable calibration, UI status is `research_unvalidated`; “confidence” is not inferred from MAE.

**Acceptance:** the August 2026 report checkpoints can be reproduced from the archived legacy fixture, and v2 produces a sealed row-level ground-truth comparison receipt without target leakage. Re-running from the same origin/model/target revisions is byte-deterministic; removing or shifting a future target cannot alter the fitted past-origin model.

## PR 5 — Forecast UX and provenance

**Goal:** make forecast behavior understandable and prevent historical/latest mismatches.

**Frontend**

- rewrite `Forecasts.tsx` subtitle from Prophet to the actual engine/version;
- make Forecast reachable from role-appropriate navigation;
- update `BridgeHistoryModal.tsx` and `BridgeDetailModal.tsx` to pass exact year/revision or visibly choose latest;
- label bounds as state-distribution dispersion or calibrated quantiles, never generic confidence;
- plot deck, superstructure, substructure, and culvert with missing observations visibly broken rather than connected;
- add a **Historical validation** view: choose a past origin, overlay the as-of forecast fan/path with later observed inspection points, show per-year/component error, and link each ground-truth point to its published revision;
- add portfolio hindcast panels for error by lead time, component, starting state, county/cohort, and treatment evidence class; show persistence beside the candidate, not on a separate hidden report;
- allow `no action`, `observed history replay`, reviewed policy, and explicit schedule modes, with simulated actions rendered as distinct markers and never mixed with observed work;
- show run status, source year/generation, model/evaluation version, last observation by component, fallback level, sample exposure, and stale warning;
- use durable progress/cancel and suppress stale responses;
- hide build/promote controls from viewers.

**Acceptance:** a user viewing 2015 cannot unknowingly receive a latest-snapshot forecast; every chart identifies observed, predicted, and simulated-treatment values and its uncertainty meaning. A reviewer can visually and numerically compare a forecast made from historical data with the actual later inspection without downloading a separate artifact.

## PR 6 — Correct LCCA facts, geometry, NHS, and cost libraries

**Goal:** make every LCCA input explicit and reproducible before changing policy logic.

**Files**

- new `backend/app/services/lcca_v2/models.py`, `facts.py`, `costs.py`, `economics.py`, `repository.py`;
- retain `routers/services/lcca.py` as a temporary adapter.

**Facts**

- exact published bridge revision and requested/as-of year;
- all component ratings with their actual observation years;
- controlling-component evidence;
- NHS from qualifying carried feature with ambiguity status and visible override;
- deck area through the shared purpose-specific area engine;
- geometry/source/exclusion evidence, never zero-area substitution;
- forecast model/run and component-specific transition/recovery evidence.

**Bridge life-history and treatment library**

Add `life_history.py` and `treatments.py`. The first reconstructs only evidenced events since BW01/construction; the second defines actions that may be simulated after the as-of year. They are not interchangeable.

Seed a versioned `njdot_preventive_maintenance_2016_tamp_profile` from the NJDOT TAMP treatment table as a reviewed simulation preset:

| Component | Candidate treatment | Published cycle/trigger |
|---|---|---|
| Whole bridge/deck | cleaning, washing, sweeping | every 2 years |
| Deck/wearing surface | seal asphalt-wearing-surface cracks | every 2 years |
| Deck | seal deck cracks | every 2 years |
| Parapet | seal cracks | every 2 years |
| Deck | seal concrete deck | every 5 years |
| Deck | corrosion inhibitor | every 5 years |
| Deck | repair/replace joints | every 10 years |
| Deck/sidewalk | concrete repair | every 10 years |
| Parapet | concrete repair | every 10 years |
| Base plates/curb | reseal | every 10 years |
| Approach/deck | approach slab repair, HMA patch, safety work | as needed |
| Superstructure | concrete repair | as needed |
| Bearings | lubricate | every 4 years, and only with other work under the cited profile note |
| Substructure | seal cracks | every 5 years |
| Substructure | concrete repair | every 10 years |
| Substructure | carbon wrap, seal concrete, erosion/scour repair | as needed |

The current NJDOT TAMP says preventive treatments are applied only when the component is rated 5 or higher, and gives additional element-condition criteria for some actions such as deck crack sealing. Encode those criteria as versioned predicates, not prose in the UI. If the required element-level input is absent, the rule is `not_evaluable`, not automatically eligible.

Add broader FHWA-reviewed treatment families for scenario construction:

- deck drainage/joint repair or replacement, deck patching, overlays and waterproofing, ECE/cathodic protection, approach slab work;
- concrete sealing/coating and repairs across applicable components;
- superstructure spot/zone/full painting, steel-member repair, fatigue-detail mitigation, bearing restoration/replacement, movable machinery service;
- substructure/culvert patching, coating, ECE/cathodic protection, pile jackets/wraps, channel cleaning and scour countermeasures;
- partial/full deck replacement, superstructure rehabilitation/replacement, substructure rehabilitation, and full reconstruction/replacement from the NJDOT planning categories.

Every action specification includes component, compatible materials/structural systems, required inspection/element conditions, prohibited cases, cyclic or condition trigger, duration, outage/work-zone assumptions, immediate and durable forecast effect, repeat interval/minimum spacing, quantity/cost unit, residual-life rule, and source/review receipt. Material-specific examples—concrete deck overlay versus steel painting, for example—must be filtered mechanically from the bridge facts rather than offered for every bridge.

Published cycles are defaults for comparing a named policy, not evidence that an individual bridge received the work. The UI must show `typical NJDOT profile`, source edition, and review status. Actual bridge history still requires work-order/contract/inspection evidence.

**Cost library**

- version by jurisdiction, NHS class, action/scope, source publication, collected-cost years, currency, and real-dollar base year;
- store source URL/artifact receipt and review status;
- import current FHWA replacement/rehabilitation screening tables rather than editing Python constants;
- NJDOT bid-history or approved planning estimates are a separate, preferred library when available;
- no NJ default silently applies to another state.

The FHWA unit-cost table is a screening source and describes three-year averaging; it is not a project estimate. The current public 2024 table also describes rehabilitation as 68% of replacement, which conflicts with the current application’s unsupported 40% default and must be resolved in the chosen library rather than hidden in code.

**Economic modes**

- constant-dollar mode: real costs + real discount rate;
- nominal mode: inflate each future cost + nominal discount rate;
- reject mixed modes;
- store analysis/base year, inflation source, real rate, nominal rate, and relationship check;
- provide a 7% real grant-BCA sensitivity profile only when the selected external program requires it; do not call it the universal bridge-management rate.

**Acceptance:** facts and costs alone can be exported as a signed/canonical reproducibility receipt before simulation begins.

## PR 7 — Multi-component, multi-alternative deterministic LCCA

**Goal:** match the supplied multi-option spreadsheet behavior with a true one-year time-step simulation driven by the historically evaluated forecast, while keeping the result planning-level.

**Engine**

- require one exact forecast model/evaluation receipt; an LCCA alternative cannot supply its own deterioration rates;
- forecast each component annually and recompute the controlling condition each year;
- actions declare affected components, deterministic effect/recovery, service life, minimum spacing, cost formula, duration, and residual treatment;
- initial action at year 0 is supported;
- named alternatives are ordered action schedules or transparent trigger policies;
- replacement resets appropriate component states and structural age evidence;
- component-specific recovery replaces the current hard-coded deck recovery;
- no consecutive rehabilitation unless the action specification explicitly allows it;
- compare all alternatives over one common analysis period.

**Exact annual simulation order**

For every alternative and year from the analysis start through the terminal year:

1. Load the previous year's post-action component-state distributions, structural/component ages, and active treatment durability.
2. Apply one annual no-action forecast transition to obtain the pre-action distribution.
3. Evaluate all cyclic, condition, age, work-history, budget, and minimum-spacing triggers using only information available at that simulated year.
4. Resolve simultaneous eligible actions with the alternative's explicit priority/conflict rule; no implicit cheapest-action choice.
5. Apply the selected action-conditioned forecast transition/effect, including phased work and downtime where specified.
6. Record quantities, agency cost, supported user cost, inflation, discounting, and funding year.
7. Update component/structural ages and active treatment clocks without altering observed historical facts.
8. Store the full pre-action/post-action distributions, trigger decisions including rejected candidates, and annual receipt before advancing one year.

No simulation may jump directly from year 0 to years 5, 10, or 25. If a forecast model internally uses a multi-year kernel, it must expose the equivalent reviewed one-year transitions or is ineligible for LCCA.

**Initial action library**

- do-minimum / run-to-replacement screening policy;
- full replacement;
- deck replacement;
- deck + substructure rehabilitation;
- superstructure rehabilitation/painting;
- user-defined reviewed action schedule.
- typical NJDOT preventive-maintenance cycle from PR 6;
- condition-triggered preservation/rehabilitation using only compatible reviewed actions;
- actual-history replay through the latest observed year followed by one of the forward strategies.

These names are templates, not automatic engineering recommendations. Unsupported scope remains unavailable rather than being approximated by the nearest action.

**Annual ledger**

For each alternative/year retain:

- pre-action and post-action component distributions/expected ratings;
- controlling component and condition;
- action and quantity;
- present cost, inflation factor, future nominal cost when applicable;
- discount factor and present value;
- agency routine, capital, and separately supported user-cost streams;
- residual credit and remaining-life basis.
- every evaluated trigger, compatibility result, treatment-effect model/fallback, and reason an eligible-looking action was not selected.

**Outputs**

- present-value life-cycle cost with cents;
- total undiscounted/future cost only when meaningful;
- residual-adjusted NPV;
- equivalent annual cost:
  - `EAC = NPV / n` when `r = 0`;
  - `EAC = NPV * r(1+r)^n / ((1+r)^n - 1)` otherwise;
- raw decision margin and configurable materiality tolerance;
- lowest-cost alternative label, not “optimal,” unless an optimization method actually searches the allowed decision space.

The current blanket 30% “user-cost” uplift is removed. Delay, vehicle operating, crash/safety, detour, emissions, and vulnerability costs are separate optional modules and default to unavailable without inputs.

**Acceptance:** fixtures reproduce spreadsheet-style alternative tables, exact 100-row annual ledgers for a 100-year period, year-0 actions, cyclic cleaning/sealing/bearing/joint work, condition-triggered rehabilitation, repeated painting, component-specific deterioration after treatment, terminal residual credits, EAC, zero-rate cases, and final-year action edge cases exactly. Changing one forecast transition changes the corresponding future LCCA path and receipt; no independent LCCA deterioration constant remains.

## PR 8 — Probabilistic LCCA, sensitivity, scenarios, and review

**Goal:** show decision uncertainty rather than converting one expected forecast path into a strong recommendation.

**Methods**

- propagate component state distributions and recovery distributions through Monte Carlo or exact state/action expectation;
- sample annual no-action and treatment-conditioned transitions from the same released forecast package used by deterministic LCCA; preserve cross-component/dependent uncertainty when the model supports it and disclose independence otherwise;
- include forecast-model uncertainty, hindcast residual/error calibration, and sparse-treatment-effect uncertainty instead of varying costs alone;
- deterministic seed recorded for reproducibility;
- one-way sensitivity for horizon, discount/inflation, action cost, treatment effect, trigger, service life, and geometry;
- probabilistic sensitivity for reviewed parameter distributions;
- report expected NPV/EAC, percentiles, probability each alternative is least-cost, break-even values, annual treatment-demand probability, and probability/timing of component or controlling-condition thresholds.

**Persistence**

- `lcca_cost_libraries`, `lcca_action_libraries`;
- `lcca_scenarios`, `lcca_alternatives`, `lcca_runs`;
- immutable annual-ledger and sensitivity artifacts stored content-addressed with hashes;
- owner, reviewer, source revision, model/evaluation version, assumptions, and timestamps;
- stage/review/approve lifecycle separate from inventory publication.

**Jobs**

- short deterministic comparisons may run synchronously under strict bounds;
- probabilistic/sensitivity runs use durable jobs with quotas, progress, cancellation, and restart recovery.

**Acceptance:** a data/model/cost/action change creates a new scenario identity; an old scenario remains reproducible and visibly stale.

## PR 9 — LCCA UX matching the supplied alternative table

**Goal:** replace the current two-policy modal with a transparent multi-alternative analysis.

**UI sections**

1. Exact bridge/snapshot identity and planning-only warning.
2. **Life since construction** timeline from BW01 through the latest observed year, with inspections and observed work visually distinct from inventory-reported work and uncertain condition-change candidates. Unknown periods stay blank/hatched.
3. Analysis period, base year, real/nominal mode, discount and inflation rates.
4. Cost/action library and provenance.
5. **Simulation strategy selector** with side-by-side options:
   - no capital action;
   - replay evidenced historical work, then no action;
   - typical NJDOT preventive-maintenance cycles;
   - condition-triggered preservation/rehabilitation;
   - explicit user schedule;
   - full replacement or other reviewed alternative templates.
6. Compatible-treatment builder. Dropdowns are filtered by deck/superstructure/substructure material and type, condition/element criteria, and available cost/effect evidence. Disabled actions explain the missing or incompatible input.
7. Annual simulation controls: start year, horizon, trigger thresholds, cycle on/off, action priority, minimum spacing, budget cap, and deterministic/probabilistic mode. Defaults are always named library values and include a reset-to-source action.
8. Alternative cards with editable reviewed schedules and a year-by-year action preview before running.
9. Spreadsheet-like ledger columns: action, year, pre/post component condition, trigger, present cost of work, future cost, present value, residual value, total NPV, and equivalent annual cost.
10. Condition, treatment, and cumulative-cost charts by alternative; clicking a simulated action opens its applicability, forecast-effect, cost, and source evidence.
11. Historical forecast-performance panel for the selected bridge/cohort so the user can see how similar past forecasts compared with later ground truth before accepting the LCCA scenario.
12. Sensitivity tornado/distribution view.
13. Engineer-review status and immutable export.

The UI will expose controlling component, feature/NHS evidence, deck-area method, forecast and hindcast-evaluation versions, treatment-effect source/sample count, forecast fallback, residual credit, unrounded margin, and missing inputs. It will not silently recommend a treatment when geometry, action scope, cost evidence, compatibility, or forecast qualification is unavailable. Simulated actions are labeled `scenario`; they never appear in inspection/work-history views as completed work.

## PR 10 — Operational and NJDOT-style portfolio KPIs

**Goal:** keep the useful count dashboard and add the condition KPIs shown in the supplied examples.

**Versioned KPI registry**

Each measure definition contains ID, title, purpose, population, numerator, denominator, unit, time basis, rounding, exclusions, source fields, status, and approval/version.

**Inventory measures**

- total, Good, Fair, Poor, and Unknown bridge counts and count percentages;
- average age at snapshot and configurable older-bridge count;
- carried-feature AADT/ADTT completeness and configured high-traffic count;
- county counts with Unknown county retained;
- total valid deck area and missing/excluded geometry.

**Requested NJDOT-style acceptable-condition measures**

1. **Percent of state-maintained bridges 20 ft or more in acceptable condition**
   - candidate population: approved state-maintained owner/maintenance codes, BG01 at least 20.0 ft, selected snapshot year;
   - acceptable: controlling rating at least 5 (Good or Fair);
   - default denominator: classifiable eligible bridges;
   - always return all-eligible count, unknown/invalid count, and the effect of alternate denominator policy.
2. **Percent of state-maintained bridge deck area in acceptable condition**
   - same approved state-maintained population;
   - numerator: valid deck area of acceptable bridges;
   - denominator: valid deck area of all classifiable eligible bridges;
   - return missing/excluded area separately.
3. **Deck area by age and condition**
   - age at snapshot;
   - bins `0–4`, `5–9`, continuing in five-year increments through `95–100`, then `>100`;
   - stacked classes Good, Upper Fair (6), Lower Fair (5), Poor, plus an optional Unknown visibility control;
   - display in thousand ft² while API retains exact ft².

The exact NJDOT owner/maintenance code set and denominator treatment must be approved and versioned. The implementation will fail closed with `definition_pending` rather than guess them.

**API**

- `GET /api/analytics/kpis/operational?state_code=&year=&definition_version=`;
- `GET /api/analytics/kpis/acceptable-condition?...`;
- `GET /api/analytics/kpis/age-condition-area?...`;
- body and headers include source generation, revision-set hash, metric version, exact numerator/denominator, rounding, exclusions, and ETag.

**Acceptance:** statewide totals reconcile with county/age bins plus explicit Unknown/excluded buckets; historical values do not drift with wall-clock time.

## PR 11 — Program-delivery KPI data and targets

**Goal:** support the fiscal-year target/actual table shown in the first image without fabricating operational data from SNBI.

The following are not available from bridge inventory alone and require governed annual program data:

- bridge safety inspections performed in-house;
- state-maintained inspections by consultants;
- county bridge safety inspections;
- percentage of planned construction projects awarded;
- construction contracts awarded/value;
- railroad grade-crossing inspection counts;
- ROW acquisition cost;
- bridges completed;
- lane-miles under construction;
- projects designed in-house and related budget metrics.

**Schema**

- `kpi_measure_definitions` with immutable versioned formula/ownership metadata;
- `kpi_reporting_periods` for fiscal/calendar year and status;
- `kpi_program_values` for actual, budgeted, revised, target, and estimate values;
- `kpi_program_sources` linking immutable source artifacts, worksheet/range or system receipt, import hash, actor, reviewer, and approval;
- `kpi_program_issues` for duplicate, missing, unit, and reconciliation failures;
- append-only audit and workspace-scoped foreign keys.

**Import**

- strict Excel/CSV template with duplicate-safe parsing, formula-value policy, units, fiscal year, organization, target/actual type, and source receipt;
- preview/diff, validation, idempotent commit, and reviewer approval;
- no direct spreadsheet formulas become trusted executable code;
- manual entry requires reason and second-person approval in commercial mode.

**Acceptance:** the dashboard can reproduce approved historical fiscal-year rows and clearly distinguishes source-recorded values from SNBI-derived values.

## PR 12 — Separate FHWA TPM diagnostic

**Goal:** implement a standards-traceable diagnostic without mislabeling it as an official FHWA result.

**Engine profiles**

- 2025 and earlier: Coding Guide-based FHWA procedure;
- 2026–2027: FHWA-HIF-25-010 transition-period rules;
- 2028 and later: SNBI-based FHWA-HIF-25-010 rules.

**Required behavior**

- population uses selected state, applicable NBIS bridge-length rule, qualifying carried highway/NHS features, ramps, and border handling;
- 2026–2027 area: positive BG05 else BG09, multiplied by BG02;
- 2028+: positive BG15 else BG02 × BG05;
- Decimal half-up area and final percentage rounding;
- condition based on valid prepared component ratings/BC13 logic per profile;
- excluded all-missing conditions and missing/zero geometry are counted by reason;
- output Good and Poor deck-area percentages, total applicable deck area, Fair denominator area, bridge counts, and all exclusions;
- artifact/profile/version references are returned.

FHWA states that these measures concern NHS bridge deck area, including connected ramps and border bridges; they are not all-bridge count percentages. The panel remains labeled **FHWA TPM Diagnostic — not an FHWA submission** until independently validated against FHWA prepared data and approved by the agency.

**Acceptance:** golden fixtures cover transition and 2028+ rules, exact half cases, ramps, multiple features, border records, culverts, missing ratings, and missing geometry; an external known-year comparison is archived with explained differences.

## PR 13 — KPI dashboard, trends, exports, and scenario KPIs

**Goal:** present distinct measures without mixing observed, program, federal-diagnostic, and forecast values.

**Tabs**

- Operational Inventory;
- Acceptable Condition;
- Deck Area by Age/Condition;
- Program Delivery;
- FHWA TPM Diagnostic;
- Future Scenario KPIs, disabled until a forecast/scenario run is explicitly selected.

**Visuals**

- count and deck-area condition cards with exact numerator/denominator tooltips;
- five-year age stacked bars matching the supplied Good/Upper Fair/Lower Fair/Poor layout;
- fiscal-year table with Actual, Budgeted, Revised, Target, and Estimate columns;
- targets and trends with missing values shown as missing, never zero;
- county and ownership filters whose totals reconcile;
- visible source year, generation, metric version, update time, exclusions, and diagnostic status.

**Exports**

- server-generated immutable CSV/JSON/PDF package;
- metric specification, filters, exact values, source identity, exclusions, and hashes included;
- spreadsheet-safe CSV values;
- no screenshot-only PDF as the sole reproducibility artifact.

**Scenario KPIs**

If later enabled, expected future Good/Fair/Poor count/area, probability of reaching Poor, expected intervention demand, and budget distributions must reference a forecast run and scenario. They are never displayed as observed inventory or FHWA TPM results.

## PR 14 — Cross-feature release gate and legacy retirement

**Goal:** prove the system is coherent at production scale and remove unsafe ambiguity.

**Integration tests**

- one published revision change increments data generation and makes forecast/LCCA/KPI artifacts stale together;
- only the targeted bridge/year changes;
- forecast predictions never alter observed KPIs;
- LCCA calls the released annual forecast transitions for every component/year and has no alternate deterioration/recovery code path;
- changing the bound forecast/treatment-effect version changes the LCCA identity and future ledger while preserving the old reproducible run;
- forecast release failure blocks commercial LCCA and produces an explicit research warning/override path only where authorized;
- a past-origin forecast is compared with exact later observed ratings before that version can drive a released LCCA;
- actual work, condition-change candidates, and simulated rehabilitation cannot be confused through APIs, exports, or UI styling;
- LCCA actions never become works without a separate authorized workflow;
- exact historical map year is honored by History, Forecast, and LCCA;
- same source identity appears in all three responses;
- concurrent build/evaluate/scenario jobs cannot overwrite or promote one another;
- viewer cannot build/promote/import program KPIs;
- backup/restore retains analytical tables, source receipts, and hashes;
- retention/legal hold covers scenario and program artifacts;
- shadow migration and query performance run against the production-sized database.

**Legacy retirement**

- remove `ensure_training_data` from requests;
- stop production calls to old `get_state_kpis`, `get_county_kpis`, and raw `compute_bridge_lcca`;
- remove Prophet UI text and optional path unless retained only as a research comparator;
- archive old forecast DB with a manifest, then make it read-only compatibility evidence;
- return 410 for retired mutation routes in commercial mode after one documented compatibility release.

**Release receipts**

- schema/migration manifest;
- production-sized row/count/hash reconciliation;
- forecast evaluation/model card;
- cost/action assumption register;
- KPI definition register;
- FHWA diagnostic comparison receipt;
- frontend build, backend tests, concurrency/backup drills, and accessibility checks.

## 5. Exact KPI definitions requested from the images

| Display | Source | Proposed calculation | Required confirmation |
|---|---|---|---|
| State-maintained bridges ≥20 ft acceptable | Published bridge snapshots | 100 × eligible classifiable bridges with controlling rating ≥5 / eligible classifiable bridges | State-maintained owner/maintenance codes; unknown denominator policy |
| State-maintained deck area acceptable | Published snapshots + area engine | 100 × valid area in rating ≥5 / all valid classifiable eligible area | State-maintained codes; area profile for each dataset year |
| Good/Upper Fair/Lower Fair/Poor deck area by age | Published snapshots | Sum valid ft² by five-year snapshot-age bin and condition subgroup | Population: statewide NBIS, state-maintained, or user filter |
| In-house/consultant/county inspections | Program KPI source | Approved fiscal-year counts by delivery type | Authoritative source system/file and counting event |
| Planned projects awarded | Program KPI source | Awarded eligible projects / planned eligible projects | Project universe, cutoff date, cancellations/change orders |
| Construction contracts awarded | Program KPI source | Count and/or approved award value | Unit shown in sheet must be confirmed |
| ROW acquisition cost | Program KPI source | Approved fiscal-year actual/estimate | Accounting basis and units |
| Bridges completed | Program KPI source | Count of projects/structures meeting approved completion definition | Whether multi-structure contracts count once or per bridge |
| Lane-miles under construction | Program KPI source | Approved project-level lane-mile aggregation | Overlap/double-counting and fiscal-year snapshot rule |

No program-delivery value will be reverse-engineered from SNBI fields.

## 6. Testing matrix

### Forecast

- boundaries for rating, age, traffic, work intervals, and cohort exposure;
- state/county/workspace isolation and duplicate fallback-key prevention;
- carried-feature selection and temporal no-leakage;
- probability conservation, absorbing state, finite values, and deterministic serialization;
- persistence-relative rolling evaluation and bridge bootstrap;
- sealed past-origin/future-target hindcasts with exact published ground-truth revisions and strict as-of leakage checks;
- one-step versus recursive-path error, probability calibration, and candidate-versus-persistence comparison at every lead time;
- action-free, observed-treatment, uncertain-jump, and replacement strata; treatment-effect before/after and durability follow-up;
- no-action/action-conditioned annual transition compatibility, sparse-cohort fallback, and unavailable-effect behavior;
- stale cache/run behavior and durable job failure/cancel/retry;
- legacy audit checkpoint reproduction.

### LCCA

- every geometry era and exclusion path;
- NHS auto-detection/ambiguity/override;
- real versus nominal consistency and zero-rate EAC;
- component-specific actions/recovery and controlling-component changes;
- mandatory forecast model/evaluation binding and rejection of any independent deterioration/recovery constant;
- observed/inventory-reported/inferred/simulated work provenance and unknown prehistory;
- treatment compatibility by component/material/type/condition plus cyclic/condition/as-needed rules;
- year-0, repeated, minimum-spacing, replacement, and final-year actions;
- exact annual step order, simultaneous-trigger priority, phased action, budget deferral, post-action deterioration, and 100-year ledger cardinality;
- annual ledger reconciliation to NPV, residual-adjusted NPV, and EAC;
- materiality tolerance and unrounded decision margin;
- sensitivity seeds, percentiles, and immutable replay.

### KPIs

- Good/Upper Fair/Lower Fair/Poor/Unknown/invalid cases;
- bridge and culvert control, BC13 conflict, and denominator policies;
- age boundary bins and historical age stability;
- null county, multi-feature, traffic, owner, and state-maintained filters;
- exact deck-area half-up cases and exclusions;
- statewide/county/age-bin reconciliation;
- program KPI duplicate/unit/fiscal-year/import approval;
- federal era, ramp, border, NHS, area, and rounding fixtures;
- frontend stale-response suppression, generation refresh, accessibility, and exports.

## 7. Rollout and rollback

1. Add schema/views/tables without changing legacy routes.
2. Run projection parity and migration rehearsal on a shadow copy of the full database.
3. Fit v2 and complete sealed historical hindcasts in research mode; do not promote automatically.
4. Switch Forecast reads behind `BRIDGEGUARD_FORECAST_MODE=v2|required|off`; no legacy production fallback.
5. Import/review NJDOT work history and treatment/action/cost libraries; keep absent history visibly unavailable.
6. Enable deterministic LCCA v2 as planning-only only after it is bound to a forecast version that passed the approved hindcast gate; then enable probabilistic jobs.
7. Enable operational/acceptable-condition KPIs, then governed program KPIs.
8. Enable FHWA diagnostic only after golden/external comparison.
9. Retire legacy routes after one compatibility release.

Rollback disables the affected analytical router and restores the prior read UI. It does not delete new immutable evidence or mutate observed inventory.

## 8. Decisions that require NJDOT/owner approval

Implementation can start before these decisions, but affected outputs remain disabled or explicitly provisional:

1. state-maintained owner/maintenance code set;
2. acceptable-condition denominator treatment for Unknown/invalid records;
3. exact population for the age/deck-area chart;
4. program-delivery KPI definitions, units, source systems, and fiscal cutoffs;
5. cost/action libraries and real-dollar base year;
6. forecast release-skill and calibration thresholds;
7. acceptable evidence for identifying a historical rehabilitation event and linking work orders/contracts to a bridge/component;
8. approved NJDOT treatment cycles, condition/element eligibility, material compatibility, action effects, and override authority;
9. which treatment effects may use engineer assumptions when historical exposure is insufficient;
10. LCCA materiality tolerance and approval workflow;
11. whether the FHWA panel remains internal diagnostic or proceeds to external validation.

## 9. Primary standards and guidance used

- [23 CFR 490.409 — bridge condition measure calculation](https://www.ecfr.gov/current/title-23/chapter-I/subchapter-E/part-490/subpart-D/section-490.409)
- [FHWA-HIF-25-010 — TPM Bridge Condition Measures, November 3, 2025](https://www.fhwa.dot.gov/tpm/guidance/hif25010.pdf)
- [FHWA-HIF-18-023 — Coding Guide-based procedure for 2025 and earlier](https://www.fhwa.dot.gov/tpm/guidance/hif18023.pdf)
- [FHWA 2024 Bridge Replacement Unit Costs](https://www.fhwa.dot.gov/bridge/nbi/sd2024.cfm)
- [FHWA Bridge Preservation Guide](https://www.fhwa.dot.gov/bridge/preservation/guide/guide.pdf)
- [FHWA Bridge Management resources](https://www.fhwa.dot.gov/bridge/management/)
- [FHWA InfoBridge condition-rating forecast — survival-based models](https://infobridge.fhwa.dot.gov/Content/Documents/Models/Bridge_Component_Condition_Forecast_Survival_Based_Models.pdf)
- [FHWA InfoBridge network forecasts — no-action and condition-improvement levels](https://infobridge.fhwa.dot.gov/Content/Documents/Models/Performance_Forecasts_of_Bridge_Networks_ML_Model.pdf)
- [New Jersey 2022 Transportation Asset Management Plan](https://www.nj.gov/transportation/about/asset/pdf/NJTAMP.pdf)
- [New Jersey 2019 Transportation Asset Management Plan — published preventive-maintenance cycles](https://www.nj.gov/transportation/about/asset/pdf/NJ_2019_TAMP_FHWA.pdf)
- [FHWA LCCA guidance and resources](https://www.fhwa.dot.gov/pavement/lcca/150304.cfm)
- [USDOT 2026 Benefit-Cost Analysis Guidance](https://www.transportation.gov/mission/office-secretary/office-policy/transportation-policy/benefit-cost-analysis-guidance)

These sources define or inform particular profiles. They do not certify BridgeGuard outputs.

## 10. Definition of done

The work is complete only when:

- the three analytical products use the same published source identity;
- traffic/NHS/geometry/condition semantics are shared and tested;
- no viewer GET mutates analytical state;
- forecast claims are gated by sealed past-origin hindcasts against exact later published ground truth and baseline-relative evaluation;
- separate no-action and action-conditioned forecast effects are annual, evidenced, calibrated where possible, and explicitly unavailable where not supported;
- every LCCA year is driven by the exact qualified forecast/treatment-effect package, with no second deterioration or blanket recovery model;
- actual bridge work history, uncertain inferred changes, and future simulated rehabilitation are visibly and structurally distinct;
- LCCA presents multiple auditable annual rehabilitation strategies, EAC, residual value, and uncertainty without calling a screening result an engineering recommendation;
- the UI can compare no-action, typical NJDOT cycle, condition-triggered, and explicit rehabilitation schedules while filtering actions to compatible bridge/component facts;
- operational, NJDOT program, and FHWA diagnostic KPIs are visibly distinct;
- the requested acceptable-condition and age/deck-area KPIs reconcile exactly;
- every result can be reproduced from immutable source/model/specification hashes;
- production-sized migration, performance, backup/restore, concurrency, and frontend tests pass;
- known unavailable inputs remain unavailable rather than being guessed.
