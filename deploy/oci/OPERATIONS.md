# BridgeGuard OCI Operations Runbook

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
6. The workflow checks out the exact tested SHA, runs preflight, creates an
   online backup (or checksum-verifies and reuses one created within the
   previous hour during a retry), pulls pinned dependencies, rebuilds the app,
   waits for `/ready`, restarts Cloudflare Tunnel, and runs
   `deploy/oci/verify.sh`.
7. Confirm the server checkout, container health, and readiness:

   ```powershell
   ssh -i "<OCI_SSH_KEY_PATH>" ubuntu@<OCI_ACTIVE_HOST> `
     "git -C /opt/bridgeguard log -1 --oneline; docker compose -f /opt/bridgeguard/deploy/oci/compose.yaml ps; docker compose -f /opt/bridgeguard/deploy/oci/compose.yaml exec -T app python -c `"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=15).read().decode())`""
   ```

The production VM uses CPU-only Nomic TEI embeddings and remote Cerebras
generation/vision. Do not invoke a workstation or server GPU.

## Safe recovery checks

- A failed preflight or backup occurs before the application is rebuilt.
- Do not bypass the backup. A complete backup may take several minutes because
  it compresses and checksums reports, RAG assets, point clouds, and databases.
- Check backup progress without reading application data:

  ```powershell
  ssh -i "<OCI_SSH_KEY_PATH>" ubuntu@<OCI_ACTIVE_HOST> `
    "docker top oci-app-1 -eo pid,etime,pcpu,pmem,cmd | grep app.backup || true; df -h /srv/bridgeguard/data"
  ```

- Run manual backups with `sudo -n sh deploy/oci/backup.sh` so the shared lock
  remains usable by automated deployments.
- Keep the legacy VM stopped/unchanged as rollback until the E4 deployment,
  authentication, map focus, report retrieval, embeddings, and point-cloud
  workflows have been observed successfully.
