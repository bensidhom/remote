# BridgeGuard OCI Operations Runbook

The checked-in OCI workflow is research-only. Compose forces
`research_shared` and disables the commercial-readiness gate. Do not use this
runbook as evidence of commercial, survey, engineering, official-validation,
or submission readiness. Operational backup, authentication, integrity, and
recovery gates remain mandatory for the research data.

This runbook is the durable deployment procedure. Exact workstation-specific
addresses and paths belong in `deploy/oci/operator.local.env`, which is ignored
by Git. Load that file first when it exists; never print its referenced private
key or commit the file.

## Deployment inventory

The local inventory records:

- `OCI_ACTIVE_HOST`: the paid E4 production VM.
- `OCI_LEGACY_HOST`: the former 1 GB VM, retained only for rollback.
- `OCI_USER` and `OCI_SSH_KEY_PATH`.
- `OCI_KNOWN_HOSTS`: the verified active-host public key line.
- `OCI_INSTANCE_OCID` and region.
- GitHub repository, environment, workflow, and server filesystem paths.

Never deploy the CPU embedding stack to the legacy VM. Confirm the active host
reports `x86_64`, 2 vCPUs, and about 12 GB RAM:

```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes `
  -i "<OCI_SSH_KEY_PATH>" ubuntu@<OCI_ACTIVE_HOST> `
  "hostname; uname -m; nproc; free -m"
```

## Important server paths

| Purpose | Path |
|---|---|
| Git checkout | `/opt/bridgeguard` |
| Persistent application data | `/srv/bridgeguard/data` |
| TEI model cache | `/srv/bridgeguard/models` |
| Application environment | `/etc/bridgeguard/bridgeguard.env` |
| Optional AI environment | `/etc/bridgeguard/ai.env` |
| Cloudflare tunnel environment | `/etc/bridgeguard/cloudflare.env` |
| OCI Compose file | `/opt/bridgeguard/deploy/oci/compose.yaml` |
| Backups | `/srv/bridgeguard/data/backups` |

Environment files must be owned by `root:docker`, the directory must use mode
`750`, and files must use mode `640`. The persistent data directory and its
databases are owned by container UID/GID `10001`.

Before a commercial release, the application environment must contain the
opaque operator receipt references documented in `deploy/oci/README.md` for
the PDF-engine disposition, hosted AI entitlement and DPA/model binding, the
actual ArcGIS/OSM-backed map mode, Cloudflare external-customer service/seat
use, reviewed OSS notices, and the exact release SBOM. Preflight and startup
fail closed when any reference is missing, malformed, or a placeholder. These
references do not themselves establish legal or contractual clearance; retain
the underlying records in the controlled system of record. Never put a secret,
contract text, or filesystem path in a receipt identifier.

## GitHub production configuration

The protected GitHub environment is `production`. It requires:

```text
OCI_HOST
OCI_USER
OCI_SSH_KEY
OCI_KNOWN_HOSTS
```

`OCI_SSH_KEY` contains the private-key contents read from
`OCI_SSH_KEY_PATH`; the key itself remains under the ignored local `keys/`
directory. Never put a key value in a command argument, log, Markdown file, or
Git commit. Pipe it to `gh secret set` through standard input.

## Normal release procedure

1. Run the frontend build and backend tests locally.
2. Commit and push `main`.
3. Confirm the active VM and run preflight:

   ```powershell
   ssh -i "<OCI_SSH_KEY_PATH>" ubuntu@<OCI_ACTIVE_HOST> `
     "cd /opt/bridgeguard; sh deploy/oci/preflight.sh"
   ```

4. Dispatch `.github/workflows/deploy-oci.yml` on `main`.
5. Monitor the returned GitHub Actions run until it completes.
6. The workflow checks out the exact tested SHA, runs preflight, pulls pinned
   dependencies, and builds the candidate image while the old release remains
   live. It then drains Cloudflare Tunnel, stops the old app writer, and uses
   one-shot containers from that candidate image to create/verify the current
   backup and run a clean restore drill. Only a current-format backup created
   within the previous hour can be reused during a retry. The workflow then
   starts the candidate, waits for `/ready`, restarts Cloudflare Tunnel, and
   runs `deploy/oci/verify.sh`.
   The complete remote payload is materialized before execution so a Docker
   subprocess cannot consume later commands from the SSH input stream.
7. Confirm the server checkout, container health, and readiness:

   ```powershell
   ssh -i "<OCI_SSH_KEY_PATH>" ubuntu@<OCI_ACTIVE_HOST> `
     "git -C /opt/bridgeguard log -1 --oneline; docker compose -f /opt/bridgeguard/deploy/oci/compose.yaml ps; docker compose -f /opt/bridgeguard/deploy/oci/compose.yaml exec -T app python -c `"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=15).read().decode())`""
   ```

The production VM uses CPU-only Nomic TEI embeddings and remote Cerebras
generation/vision. Do not invoke a workstation or server GPU.

## Safe recovery checks

- A failed candidate build leaves the old release live. A failed backup or
  restore drill restarts the captured old app container (and its tunnel when
  it was previously running); it never starts the candidate as recovery.
- `backup.sh` fails when the old app is absent or stopped. Existing files are
  not accepted as evidence that a usable pre-release state was captured.
- Do not bypass the backup. A complete backup may take several minutes because
  it compresses and checksums reports, RAG assets, point clouds, and databases.
- Check backup progress without reading application data:

  ```powershell
  ssh -i "<OCI_SSH_KEY_PATH>" ubuntu@<OCI_ACTIVE_HOST> `
    "docker top oci-app-1 -eo pid,etime,pcpu,pmem,cmd | grep app.backup || true; df -h /srv/bridgeguard/data"
  ```

- Run manual backups with `sudo -n sh deploy/oci/backup.sh` so the shared lock
  remains usable by automated deployments.
- Every new backup runs an isolated restore drill inside the application data
  volume. The current format binds the exact deployment UUID/customer/workspace
  marker and database receipt, all required database schemas/user versions,
  operation jobs, and every attachment row to a confined file with matching
  size and SHA-256. The drill also rejects unsafe archive members and requires
  SQLite `quick_check=ok` with zero foreign-key violations. A deployment stops
  if either backup verification or the restore drill fails. Legacy formats
  remain available for reviewed recovery but are never reused for a release.
- Deployment verification also runs `python -m scripts.run_sqlite_scale_gate`
  inside the candidate container. The command cannot accept a customer
  database path: it creates a unique temporary WAL database and fails the
  release unless two writers and twenty simultaneous readers demonstrate
  snapshot isolation, bounded busy/cancellation behavior, complete
  checkpoints, `data_generation` refresh, and a matching online-backup
  restore. Its JSON receipt contains counts, timings, runtime identity, and
  hashes only; retain it with the deployment log.
- Operational objective for the dedicated-customer deployment: daily backups
  and a backup immediately before every release (RPO no greater than 24 hours),
  with a tested four-hour restoration target (RTO). Customer contracts may set
  stricter objectives; do not advertise stricter values until an observed drill
  demonstrates them.
- Keep the legacy VM stopped/unchanged as rollback until the E4 deployment,
  authentication, map focus, report retrieval, embeddings, and point-cloud
  workflows have been observed successfully.
