# BridgeGuard hardening implementation status

**Observed:** 2026-08-14  
**Scope:** implementation status for `nbi-snbi-conversion-loader-bridgette-hardening-plan.md` and `forecasting-lcca-kpis-hardening-plan.md`  
**Boundary:** this receipt is an engineering verification record. It is not an FHWA/NBTIS submission acceptance, a professional-engineering approval, or a commercial-release authorization.

## Current outcome

The planned research/shared-workspace architecture is implemented through the hardened converter, immutable revision loader, published-snapshot Editor/export path, scoped Bridgette query system, report/visual/map identity chain, and dedicated-customer operational controls. The application now fails closed where an external or standards-dependent acceptance step is unavailable.

### Implemented and tested

- Immutable upload artifacts, bounded NBI/CSV/XML parsing, collision-safe bridge identity, eligibility decisions, BID01 policy, and record-level provenance.
- Legacy element XML that claims FHWA Element Data Schema Version 1.0 is validated before projection against the exact locally pinned XSD; unknown, conflicting, nested, or structurally invalid schema claims fail closed.
- Typed NBI-to-SNBI transitions with explicit blank/invalid/temporary/assumed/review states, rule-specific rounding, repeated-record assumptions, and field-level lineage.
- Strict annual NBI inspection-history conversion now requires an explicit contiguous multi-file/year manifest, namespaces every row by artifact hash and inventory year, reduces Item 90/91 and applicable 92/93 events before canonical validation, and keeps the single-file converter fail-closed instead of guessing initial versus routine events.
- Non-destructive canonical normalization, projection coverage, checksummed SQLite migrations, exact relational round trips, atomic writes, revisions, publish/archive/restore, and deterministic export manifests.
- Durable two-phase import preflight and commit jobs with ownership, quotas, progress, idempotency, tamper detection, stale-generation rejection, restart recovery, and rollback.
- Editor reads only published revisions, stages RFC 7396 patches, preserves untouched fields and extensions, surfaces exact validation/diff evidence, and downloads immutable server export packages.
- One shared `data_generation` observer invalidates Map, Bridgette, KPI, Loader, Explorer, and cached bridge-resolution state across clients.
- Bridgette uses only reviewed semantic views and metrics, a strict typed plan, server-injected workspace/state/bridge/time scope, a read-only SQLite sandbox, bounded execution, owner-scoped cancellation, deterministic answers, verified narrative claims, and privacy-minimized audits.
- Bridgette's offline field knowledge covers all 154 SNBI v04 tags. It combines the hash-verified schema, March 2022 specification, transition crosswalk, detailed code mapping, validation-workbook observations, local projection/transition evidence, and FHWA Q&A notes. Each source and derived fact retains an explicit trust boundary.
- Report RAG, visual investigation, map resolution, and Ask share published bridge/year/revision identity; report/image provenance and request budgets are explicit; selected report sources are filtered before any VLM call. Visual investigation now uses a durable two-phase enqueue/status/result/cancel workflow in dedicated/production mode: the accepted job freezes the exact published snapshot, selected report byte receipts, and VLM/profile identity, recovers expired leases after restart, polls durable cancellation at application boundaries, and stores the path-free result/evidence as an immutable content-addressed artifact.
- Dedicated deployments enforce isolated storage/identity binding, explicit users, authentication, quotas, retention/legal holds, audited deletion, durable report/import jobs, consistent backup/restore drills, and a disposable SQLite scale gate.

## Forecasting, LCCA, and KPI implementation receipt

The research implementation now follows the fixed analytical chain `published observations -> annual forecast -> annual LCCA -> separately labeled KPIs`.

- **Shared published source:** the analytical projection is workspace-, state-, year-, revision-, and generation-bound. It selects carried-highway features deterministically, retains ambiguity, computes condition through the shared controller, and keeps observed, inventory-reported, inferred, and simulated work in distinct evidence classes.
- **Forecasting v2:** annual monotone Markov transitions are fitted only from eligible no-action intervals. Rolling-origin hindcasts bind every prediction to an exact later published ground-truth revision and compare the candidate with persistence. Model datasets, packages, evaluation rows, bridge runs, and annual probability vectors are immutable and content-addressed. Training/evaluation is a durable job; bridge reads never rebuild training data.
- **Forecast-driven LCCA:** every simulated bridge/component/year calls the bound forecast provider. The LCCA has no independent deterioration curve or blanket component reset. It supports no-action, evidenced-history replay, named NJDOT preventive cycles, condition-triggered rehabilitation, explicit schedules, and replacement, with material/type applicability, named treatment/cost evidence, annual pre/post-condition and cost ledgers, real/nominal economics, NPV, residual value, and EAC. Simulated actions never become observed work.
- **Forecast and LCCA UI:** Forecasts shows observed history, annual expected ratings, probability ranges, model identity, and the historical ground-truth evaluation. Bridge History now uses only the exact-revision v2 forecast service. Rehabilitation simulation is confined to LCCA, where actions, costs, compatibility, and annual results are visible.
- **Operational KPIs:** published-snapshot counts, Good/Fair/Poor/Unknown denominators, snapshot age, deck area and exclusions, carried-AADT completeness, counties including Unknown, candidate state-maintained acceptable-condition measures, and five-year age/condition deck-area bins are computed server-side with definition and revision receipts.
- **Program KPIs:** strict bounded CSV/XLSX ingestion, immutable source receipts, duplicate/unit/year/formula validation, preparer/reviewer separation, append-only approval, and approved fiscal-year reads are implemented. No program-delivery value is inferred from SNBI.
- **FHWA TPM diagnostic:** pre-2026, 2026–2027 transition, and 2028+ calculation profiles include NHS/ramp/border/length/condition/area/rounding evidence and exclusions. The output is always labeled diagnostic-only, unofficial, and not submission-ready.
- **Exports and coherence:** KPI JSON/CSV/PDF packages are deterministic, hashed, and spreadsheet-safe. Tests prove forecast rows cannot alter observed KPIs; a publication change changes the shared generation/revision receipt; untouched historical populations retain their exact revision identity. Dedicated/commercial mode returns 410 for all legacy raw KPI, request-mutating forecast, and raw LCCA endpoints.

The active database schema is version 10. It adds immutable forecast/evaluation/run and governed KPI definition/source/approval/value evidence without converting forecasts or scenarios into inventory observations.

### Analytical items still requiring real-world evidence or approval

1. Fit the forecast package on the intended published customer dataset and archive the resulting rolling-origin evaluation/model card. A research model that does not pass the configured release threshold remains visibly unqualified and cannot drive commercial LCCA.
2. Replace research treatment-effect and cost assumptions with reviewed NJDOT libraries, linked work-order/contract evidence, and engineer approval. Deterministic annual comparisons are available; a released probabilistic sensitivity/optimization claim is not made.
3. Approve the state-maintained code set, Unknown denominator policy, program KPI definitions/units/fiscal cutoffs, and import actual governed program source files with an independent reviewer.
4. Compare the FHWA diagnostic against an independently prepared known-year FHWA/NJDOT result and archive explained differences. It remains a diagnostic until that external step is complete.
5. Re-run the production-sized shadow migration/restore receipt against schema version 10. The archived receipt below proves the earlier schema-8 projection migration, not the new analytical tables.

## Federal artifact and validation boundary

The exact bytes below are locally vendored and SHA-256 verified:

| Artifact | SHA-256 | Status |
|---|---|---|
| FHWA-HIF-22-017, March 2022 | `2b55c855c0ed306dfd02b06a0533d48103c3aee7e1685aee5c5e35a671b96142` | Source bytes verified; narrative rules are not automatically executable |
| NBI-to-SNBI crosswalk XLSX | `9b81e0b7c9d808cbee65e8a5436f759e5d68b56665fb416ec51e69c40717d4fc` | Source bytes/layout verified; mapping behavior remains separately reviewed/tested |
| Detailed code mapping XLSX | `63f120e71363138a33d8cf1344199d00e363633c22ccc959d9ab49f27b76a475` | Source bytes/layout verified; mapping behavior remains separately reviewed/tested |
| SNBI schema v04 JSON | `2555813afaab4701c11e3d41b49cb1f894dfcae772afbd1638d9d25348e383a0` | Source bytes verified; declared draft/2024-07 dialect remains unsupported |
| Validation logic v1.1 XLSX | `765776e38facb1e42a528230342317b261ba47e64676ecde418f57ee7d0e2232` | 382 contiguous rows inventoried; every row remains explicitly unimplemented/pending |
| FHWA Element Data Schema v1.0 XSD | `193cda71b64933e2d5316e7d193d6f780c9479a875ea3ebcb21dcaee36f0a95f` | Legacy claimed-format validation is executable; it is not SNBI submission validation |

The deterministic reviewed-rule inventory is `backend/app/data/fhwa/derived/reviewed-rule-coverage.json`. It records `official_validation=false` and `submission_ready=false`. The 2026/2027 submission profiles remain blocked because governing memos/errata/legal-page evidence is incomplete, the schema dialect is unsupported, official workbook rules are not fully executable, and external FHWA/NBTIS checking has not occurred.

## Production-sized migration rehearsal

The source database was opened read-only and copied with SQLite's online backup API to a disposable rollback-journal shadow. The source was not migrated or checkpointed.

| Evidence | Result |
|---|---|
| Source size / hash | 1,226,502,144 bytes / `b259d07db94bcc49e12f257d7006f61a7e337f0332fef083973b731b6d5d70ea` |
| Schema versions | source 0 → shadow 8; migrations 1–8 applied |
| Integrity | source/shadow `quick_check=ok`; 0 FK violations; 0 target-schema drift |
| Projection preservation | exact row count, identity hash, and full-row content hash matched for all 10 projection tables |
| Largest tables checked | 585,154 elements; 569,234 features; 313,380 routes; 221,804 bridge snapshots |
| Timing | copy 2.952 s; migration 15.277 s; verification 69.772 s |
| Receipt SHA-256 | `70ee324c7e4f0435a0deeb9b79f3dae2ffe025af16a178d26695d9ef91c5286d` |

This proves preservation for the exact local source projection. It does not prove that historically discarded native-SNBI fields can be reconstructed, and it does not replace a deployment-specific maintenance-window/restore rehearsal.

## Bridgette release benchmark

The versioned runtime fixture hash is `9e0d4bc7368d1cab63e5d3c6a746f1a3e377f3645b4eb4984fcdeeedc23deaf5`.

- 10/10 typed semantic/compiler/sandbox/evidence cases passed.
- 12/12 security and resource probes passed.
- Coverage includes current/exact/trend time semantics, counties, ranking/ties/nulls, age, culvert-only condition, NHS/features/routes, prompt-injection text, hostile SQL, recursive CTEs, join/row/byte/VM/deadline limits, cancellation isolation, concurrent workspaces, and stale-generation/revision identity.
- The benchmark intentionally makes no network or external model call. Provider behavior, latency, commercial entitlement, and model-specific quality require a separate deployment evaluation.

## Remaining release blockers

1. **FHWA submission support:** not ready for the reasons above. Research conversion remains labeled as a draft with external verification required.
2. **Commercial software/service clearance:** active PyMuPDF must be replaced, AGPL-complied with, or commercially licensed; the configured hosted text/vision model needs a production entitlement/SLA and a deployment-specific model/DPA review.
3. **Commercial/IP authority:** Rowan ownership/license, contributor assignments, outside-activity/conflict clearance, and the controlling NJDOT contract/data permissions remain external written-approval gates.
4. **Deployment acceptance:** run the complete archive/restore and operational smoke scenario on the intended customer host, archive its receipts, and verify the actual provider model, data-processing agreement, storage, domain, users, and quotas. Cancellation of visual work is guaranteed at application boundaries; an already in-flight hosted-provider call is not advertised as forcibly interruptible.

## Supported claims

- **Converter:** research-ready transition workflow, subject to final full-suite/release receipt.
- **Loader/Editor/export:** canonical revision workflow ready for controlled shared-workspace use, subject to deployment acceptance.
- **Bridgette:** bounded shared-workspace semantic query workflow with executable offline benchmark; provider evaluation remains separate.
- **Multi-customer:** supported only as one isolated dedicated deployment per customer after all deployment and commercial gates pass. Shared-database multitenancy is not claimed.
- **FHWA submission-support ready:** **no**.
- **Commercially cleared:** **no** until external licensing/IP/service items above are resolved.
