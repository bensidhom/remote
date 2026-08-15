# BridgeGuard dedicated-customer operations

Status: implementation and operator contract for PR10. This is not a claim
that the application supports unrelated tenants in one shared process.

## Supported deployment boundary

BridgeGuard currently supports either:

1. `research_shared`: the existing Rowan/NJDOT research workspace; or
2. `dedicated_customer`: exactly one customer, workspace, database, data root,
   cache set, object tree, application process, embedding sidecar, and
   Cloudflare Access application/tunnel.

`shared_multitenant` is deliberately rejected at startup. Do not route two
unrelated paying customers to the same application/data volume. PostgreSQL row
level security remains a future alternative; it is not silently approximated
with client-provided workspace IDs.

## Provision a customer data plane

Run the provisioning command against a new absolute data directory:

```text
python -m scripts.provision_dedicated_customer \
  --data-root /srv/bridgeguard-customers/<customer>/data \
  --customer-slug <customer> \
  --workspace-id <customer>-production
```

The command creates only an empty directory layout and an exact deployment
marker containing a generated deployment UUID. It refuses to adopt an
unmarked populated directory and refuses to reuse a marked directory for
another customer. It does not create users, databases, tunnels, DNS, or
secrets. A version-1 marker is not silently upgraded or adopted; provision a
new root and use an explicit, reviewed data migration.

Set the corresponding server-owned values:

```text
BRIDGEGUARD_ENV=production
BRIDGEGUARD_DEPLOYMENT_MODE=dedicated_customer
BRIDGEGUARD_CUSTOMER_SLUG=<customer>
BRIDGEGUARD_WORKSPACE_ID=<customer>-production
BRIDGEGUARD_WORKSPACE_NAME=<display name>
BRIDGEGUARD_DATA_DIR=/data
RAG_WORKING_DIR=/data/rag_storage
SNBI_TEMP_DIR=/data/temp
BRIDGEGUARD_WORKER_COUNT=1
BRIDGEGUARD_AUTH_MODE=cloudflare
BRIDGEGUARD_REQUIRE_COMMERCIAL_READY=true
BRIDGEGUARD_PDF_ENGINE_DISPOSITION=<permissive_replacement|commercial_license|approved_agpl_compliance>
BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID=<internal-review-receipt-id>
BRIDGEGUARD_AI_PROVIDER_ID=cerebras
BRIDGEGUARD_AI_MODEL_ID=<exact-CEREBRAS_MODEL-value>
BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID=<procurement-or-provider-receipt-id>
BRIDGEGUARD_AI_DPA_RECEIPT_ID=<privacy-review-receipt-id>
BRIDGEGUARD_MAP_SERVICE_MODE=<arcgis_premium|arcgis_sdk_keyless_osm>
BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID=<map-sdk-basemap-tile-policy-receipt-id>
BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID=<access-tunnel-service-receipt-id>
BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID=<third-party-notices-review-receipt-id>
BRIDGEGUARD_SBOM_RECEIPT_ID=<release-SBOM-review-receipt-id>
BRIDGEGUARD_OWNER_EMAIL=<explicit customer administrator>
BRIDGEGUARD_ADMIN_EMAILS=
BRIDGEGUARD_EDITOR_EMAILS=<explicit comma-separated customer editors>
BRIDGEGUARD_ALLOWED_EMAILS=<explicit comma-separated customer viewers>
BRIDGEGUARD_ALLOWED_DOMAINS=
```

The commercial-ready flag is a fail-closed launch gate: startup is refused
unless the dedicated marker, customer/workspace identity, Cloudflare Access
configuration, isolated data root, in-root RAG/temp/attachment/cache paths,
explicit role users, one-worker coordination, and the commercial entitlement
evidence contract all agree. Customer roots, markers, and store paths may not
be symlinks, contain `..`, or resolve outside the marker-backed root.

The entitlement fields are references to operator-controlled records outside
the application. For the active PDF engine, record exactly one reviewed
disposition: a verified permissive replacement, a commercial license, or an
approved AGPL compliance plan. For hosted generation, record the provider and
exact deployed model plus separate provider-entitlement and data-processing
agreement (DPA) review receipts. Receipt IDs accept only short opaque
identifier characters; placeholders and filesystem paths are rejected. They
must not contain contract text, credentials, API keys, or secrets.

The map receipt must cover the ArcGIS Maps SDK/CDN and the actual build-time
basemap mode. `arcgis_premium` means the ArcGIS API key, service plan,
referrer restrictions, hosted services, attribution, and metering were
reviewed. `arcgis_sdk_keyless_osm` means the ArcGIS SDK plus the actual
provider-backed OSM tile service, attribution, acceptable-use/volume policy,
and availability were reviewed; “keyless” does not grant unrestricted
production tile use. The Cloudflare receipt must cover the external-customer
Access/Zero Trust seat or service entitlement and Tunnel use for this customer.
Separate OSS-notice and release-SBOM receipts are required so a commercial
image cannot launch on an unrecorded dependency/license inventory.

This interlock proves only that syntactically valid operator references were
recorded and that the declared Cerebras model matches `CEREBRAS_MODEL`. It does
not inspect a contract, establish license compliance, approve data transfers,
or provide legal clearance. Preserve the underlying signed agreements and
written reviews in the organization's controlled system of record. Receipt IDs
remain server-side and are not returned by `/ready`; readiness exposes only
safe booleans and the selected PDF disposition.

The OCI Compose service reads deployment identity and role lists from
`BRIDGEGUARD_ENV_FILE`; it does not repeat those keys in the service
`environment` block. This is intentional: service-level values override
`env_file`, and `${VAR:-default}` there would let the Compose host silently
replace a dedicated file with research defaults. The optional AI environment
file is loaded first and the customer file last, so customer identity wins any
duplicate.

Compose interpolation (including the host side of the `/data` bind mount) is a
separate host-level concern and does not read the service `env_file`. Put at
least these non-secret values in a Compose host env file:

```text
BRIDGEGUARD_ENV_FILE=/etc/bridgeguard/<customer>.env
BRIDGEGUARD_AI_ENV_FILE=/etc/bridgeguard/ai.env
BRIDGEGUARD_DATA_DIR_HOST=/srv/bridgeguard-customers/<customer>/data
BRIDGEGUARD_MODELS_DIR=/srv/bridgeguard/models
CLOUDFLARE_ENV_FILE=/etc/bridgeguard/<customer>-cloudflare.env
```

Then render and launch with that file explicitly:

```text
docker compose --env-file /etc/bridgeguard/<customer>-compose.env \
  -f deploy/oci/compose.yaml config --format json
```

Before the first launch, build the reviewed image and run the one-shot
database bootstrap with the tunnel and web process stopped:

```text
docker compose --env-file /etc/bridgeguard/<customer>-compose.env \
  -f deploy/oci/compose.yaml build app
docker compose --env-file /etc/bridgeguard/<customer>-compose.env \
  -f deploy/oci/compose.yaml run --rm --no-deps app \
  python -m scripts.bootstrap_dedicated_customer --data-root /data
```

Bootstrap accepts only the exact empty marker-backed layout created by the
provisioning command. It creates and migrates `snbi_bridges.db`, creates the
single configured workspace and attachment catalog, and inserts one immutable
database binding containing the marker deployment UUID, customer, workspace,
and exact marker digest. Repeating the command against the same valid binding
is read-only and reports `created: false`. An existing research database,
another customer's database, an unbound database, or any populated layout is
rejected rather than adopted. Load customer data only after this bootstrap.

Normal startup and `deploy/oci/preflight.sh` never create this binding. They
open the database read-only first and reject a missing/mismatched binding, an
unexpected workspace catalog, any foreign workspace value, and every NULL
workspace value (including legacy attachment rows) before migrations or
backfills can run.

Confirm that `services.app.environment` contains the intended dedicated mode,
customer, workspace, commercial gate, one worker, and `/data`-rooted data,
RAG, and temp paths. Built-in Rowan/NJDOT pilot roles are disabled in every
dedicated or commercial deployment; even a pilot address must be listed
explicitly to receive a role. Domain-wide viewer grants are also rejected for
this boundary; enumerate each authorized viewer in `BRIDGEGUARD_ALLOWED_EMAILS`.

Every automated deployment helper requires that same file path through
`BRIDGEGUARD_COMPOSE_ENV_FILE`; no helper falls back to the shared research
volume. Configure it as the protected GitHub environment variable
`BRIDGEGUARD_COMPOSE_ENV_FILE`, and set `BRIDGEGUARD_CUSTOMER_SLUG` so workflow
concurrency is customer-specific.

Also configure a customer-specific Cloudflare audience/team domain, tunnel,
allowed accounts/roles, API secrets, OCI volume, backup destination, and
monitoring destination. Never clone another customer's environment or data
volume. The in-memory cancellation and request registries require one worker;
startup fails if a higher count is asserted.

## Release and rollback

Before every release:

1. Run backend tests, frontend build, the offline Bridgette golden-plan
   evaluation (`python -m app.services.query.evaluation`), the disposable
   runtime/sandbox gate (`python -m scripts.run_bridgette_runtime_benchmark`),
   and the dedicated deployment-policy tests. The runtime receipt explicitly
   excludes external model/provider quality and availability; evaluate those
   separately when a production provider is selected.
2. Run `deploy/oci/preflight.sh`.
3. Build the exact candidate while the old release remains live; then drain
   its tunnel and stop the old app writer.
4. Run the backup and clean restore drill in one-shot containers from the
   candidate image. Require the current manifest format, exact marker/database
   deployment identity, all database schemas/user versions, and complete
   attachment size/SHA reconciliation. Legacy backups are not release-reusable.
5. Deploy the exact tested revision and require `/ready` to report the
   deployment (including safe entitlement-evidence booleans), SQLite,
   migration, database, and embedding checks as ready.

Rollback disables the new application, restores the last verified backup into
a clean customer-specific directory, runs the restore drill and readiness
checks, then switches that customer's tunnel only. Never restore into a live
directory or into another customer's deployment.

The current operating objectives are an RPO of no more than 24 hours (daily
plus pre-release backup) and a four-hour RTO. They are internal targets, not a
guarantee, until recurring production drills demonstrate them.

## Data lifecycle

- Source artifacts, canonical revisions, lineage, validation, mutation/query
  audit, user edits, reports, and point clouds are backed up as durable data.
- Each content-addressed conversion/import store has a hard byte ceiling
  (`BRIDGEGUARD_SOURCE_ARTIFACT_STORE_MAX_BYTES`); ingestion reserves the exact
  additional object-plus-manifest bytes under a store lock and fails closed
  before publishing a new manifest when the ceiling would be crossed.
- Accepted source/preflight cleanup is an explicit lower-frequency maintenance
  operation, not part of the minute-level request/job loop. Call
  `run_source_artifact_retention_sweep` only after the job-retention sweep. Its
  grace period must be at least the operation-job retention period. The sweep
  protects canonical `source_artifacts`/`source_records`, every import-job
  source and preflight receipt, source receipts embedded in a protected
  preflight, unexpired artifact policies, and legal holds. It removes at most
  the configured batch, deletes a deduplicated object only after its last
  manifest, and emits immutable, path-free SHA-256 receipts under each store's
  `gc/receipts` directory. A malformed reference catalog or protected preflight
  aborts all deletion; a corrupt manifest prevents object deletion.
- Regenerable embeddings/derived RAG indexes may be rebuilt, but their profile
  and source identities remain in manifests.
- Private chat sessions default to 30 days, allow at most 90 days, and support
  owner-only export and deletion.
- Customer deletion is an explicit, separately approved offboarding operation:
  export what the contract requires, stop the tunnel/process, verify the exact
  customer marker, apply the retention/legal-hold decision, and remove the
  isolated volume/backups under a recorded operator ticket. The app never
  performs recursive customer-root deletion automatically.

## Known gate

This dedicated-instance design is the commercial isolation path. A shared
multi-customer launch remains blocked until every legacy row, attachment,
report, point cloud, cache, job, session, audit and backup is repository-scoped
and adversarially tested, preferably with PostgreSQL row-level security.
