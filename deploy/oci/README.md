# BridgeGuard OCI E4 deployment

This deployment targets an x86_64 OCI `VM.Standard.E4.Flex` instance. The
recommended configuration for the current budget is **1 OCPU (2 vCPUs), 12 GB
RAM, and a 50 GB or larger boot volume**. At the OCI rates checked on
2026-07-24, compute is approximately $41/month; confirm the console estimate
and create budget alerts before launching it.

The old `VM.Standard.E2.1.Micro` cannot be resized to E4. Create a replacement
instance from the verified `bridgeguard-mvp-upgrade-root` boot volume, validate
it privately, move the Cloudflare Tunnel connector, and retain the stopped old
instance until the new one has been stable for at least 24 hours.

No GPU is required. Cerebras serves text and vision generation remotely. The VM
runs the pinned x86 CPU Nomic embedding service, SQLite, and bounded point-cloud
geometry.

## 1. Create the replacement instance

Wait until the tenancy shows **Pay As You Go**, then:

1. Create an instance from the restored boot volume.
2. Select `VM.Standard.E4.Flex`, 1 OCPU, and 12 GB RAM.
3. Use the existing VCN/subnet. Permit SSH only from the provisioning IP; do
   not expose ports 8000 or 8080.
4. Record the new public IP and verify its SSH host-key fingerprint through an
   OCI-trusted channel.
5. Keep the old instance available for rollback. Do not delete it yet.

Cloudflare Tunnel is outbound-only, so the application needs no public ingress
rule.

## 2. Prepare the server

Run on the replacement VM:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git rsync
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

sudo install -d -o "$USER" -g "$USER" /opt/bridgeguard
sudo install -d -o 10001 -g 10001 /srv/bridgeguard/data
sudo install -d -o "$USER" -g "$USER" /srv/bridgeguard/models
sudo install -d -o "$USER" -g "$USER" /srv/bridgeguard/import/local-data
sudo install -d -m 700 /etc/bridgeguard

git clone https://github.com/bensidhom/remote.git /opt/bridgeguard
cd /opt/bridgeguard
```

Log out and back in after adding the user to the Docker group.

The preflight requires x86_64, 2 vCPUs, roughly 12 GB RAM, 10 GiB of free disk,
the two databases, a nonempty bridge inventory, valid attachment paths, and uid
10001 ownership of the data directory:

```bash
cd /opt/bridgeguard
sh deploy/oci/preflight.sh
```

It is expected to fail until the data and environment files are installed.

## 3. Install server-only configuration

Keep real secrets outside Git:

```bash
sudo cp backend/.env.example /etc/bridgeguard/bridgeguard.env
sudo cp deploy/oci/cloudflare.env.example /etc/bridgeguard/cloudflare.env
sudo chmod 600 /etc/bridgeguard/bridgeguard.env /etc/bridgeguard/cloudflare.env
```

Set at least:

```env
CEREBRAS_API_KEY=replace-with-real-key
CEREBRAS_MODEL=gemma-4-31b

BRIDGEGUARD_AUTH_MODE=cloudflare
BRIDGEGUARD_SESSION_SECRET=replace-with-at-least-32-random-characters
BRIDGEGUARD_OWNER_EMAIL=bsidhom.ahmed@gmail.com
BRIDGEGUARD_ADMIN_EMAILS=
BRIDGEGUARD_EDITOR_EMAILS=douglas.tintle@dot.nj.gov,vijay.sampat@dot.nj.gov,harjit.bal@dot.nj.gov,mantawy@rowan.edu,bahria76@rowan.edu
BRIDGEGUARD_ALLOWED_EMAILS=
BRIDGEGUARD_ALLOWED_DOMAINS=

CLOUDFLARE_ACCESS_TEAM_DOMAIN=gentle-union-b53c.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUD=1a0a861a7403ac29e00b9a8b4b96527503a879480c502f9943ce93fdaba9f2f7

TEI_EMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
TEI_EMBED_REVISION=20d1e6057a2e1fa08b45123e41ce9381fc338f0e
TEI_EMBED_DIM=768
TEI_RUNTIME_VERSION=1.9.3
TEI_EMBED_BATCH=1
BRIDGEGUARD_REINDEX_STALE_REPORTS=0
```

Keep the existing session secret when restoring the old server. Put only the
Cloudflare Tunnel token in `/etc/bridgeguard/cloudflare.env`:

```env
TUNNEL_TOKEN=replace-with-cloudflare-tunnel-token
```

The Compose file pins TEI 1.9.3 by immutable image digest, the exact Nomic model
revision, mean pooling, float32 output, Nomic query/document prefixes, 768
dimensions, and an 8,192-token batch ceiling sized for the 12 GB VM. It does
not use Ollama or NVIDIA.

## 4. Transfer and merge local processed data

The Git repository and CI/CD workflow intentionally exclude persistent data.
Transfer the local `backend/data` directory separately with a resumable tool.
From WSL or Git Bash on the workstation:

```bash
rsync -a --info=progress2 \
  /mnt/c/test/remote/backend/data/ \
  ubuntu@NEW_INSTANCE_IP:/srv/bridgeguard/import/local-data/
```

The restored boot volume may already contain the former server database. Merge
the local reports, parsed chunks, page images, point clouds, processed point
clouds, and attachment rows without replacing that inventory database:

```bash
cd /opt/bridgeguard
docker compose -f deploy/oci/compose.yaml stop app cloudflared 2>/dev/null || true
python3 backend/scripts/migrate_local_artifacts.py \
  /srv/bridgeguard/import/local-data \
  /srv/bridgeguard/data
sudo chown -R 10001:10001 /srv/bridgeguard/data
sh deploy/oci/preflight.sh
```

The merge now verifies same-size files by SHA-256, copies through atomic
temporary files, understands Windows attachment paths, and makes a complete
SQLite backup before changing attachment rows. Conflicts are reported and are
not overwritten. Do not remove the import directory until post-deployment
verification is complete.

Old Ollama/GGUF or provider-specific vector matrices are deliberately removed
from copied caches. Parsed chunks and image assets remain, so they can be
re-embedded without repeating document parsing.

## 5. Configure GitHub deployment

Create a protected GitHub Environment named `production` and add:

```text
OCI_HOST          new instance public IP
OCI_USER          ubuntu
OCI_SSH_KEY       complete private-key contents
OCI_KNOWN_HOSTS   verified ssh-keyscan line for the new IP
```

Never add an SSH key to the repository. The repository ignores `keys/` and
`*.key`, and the Docker build context excludes them.

The manual deployment workflow:

1. Runs frontend, backend, Compose, script, and container checks.
2. Deploys exactly the Git commit that passed those checks.
3. Runs the hardware and persistent-data preflight.
4. Creates and checksum-verifies a backup when an old app container exists.
5. Pulls pinned dependencies, rebuilds, stops the tunnel until `/ready` passes,
   and verifies the app plus the exact Nomic model/runtime/dimension. The
   container's `/health` probe is liveness-only; production acceptance always
   uses `/ready`.

## 6. Cut over Cloudflare

Before the first workflow run, stop the tunnel connector on the old VM so both
copies cannot serve different data:

```bash
cd /opt/bridgeguard
docker compose -f deploy/oci/compose.yaml stop cloudflared
```

Run **Deploy BridgeGuard to OCI** from GitHub Actions. Then verify:

- `/` is public and shows the minimal logo landing page.
- `/app` redirects an unauthenticated browser to Cloudflare Access.
- The owner and five editors can enter; other email addresses cannot.
- “show me bridge `<name-number>`” opens the bridge card and focuses the map.
- Existing reports answer without reparsing after their one-time re-embedding.
- Existing processed point clouds load, and a new point cloud can be cleaned,
  segmented, saved, and downloaded.
- `sh deploy/oci/verify.sh` passes on the VM.

After this initial validation, change:

```env
BRIDGEGUARD_REINDEX_STALE_REPORTS=1
```

and recreate only the app:

```bash
docker compose -f deploy/oci/compose.yaml up -d --force-recreate app
docker compose -f deploy/oci/compose.yaml logs -f app
```

Wait for the sequential report re-index to complete before removing the staged
local data.

## Backups and rollback

Create a consistent SQLite/file backup:

```bash
cd /opt/bridgeguard/deploy/oci
sh backup.sh
```

Each backup contains online SQLite copies, persistent files, and a
`manifest.json` with SHA-256 checksums and the embedding profile. Verify one
with:

```bash
docker compose exec -T app python -m app.backup \
  --verify /data/backups/BACKUP_TIMESTAMP
```

Copy backups off the VM and retain OCI boot-volume backups. A backup stored only
on the same disk is not disaster recovery.

For rollback, stop `cloudflared` on the new VM, start the old instance, and
restart its tunnel connector. Do not delete the old instance or its boot volume
until the new deployment, data, authentication, map interaction, embeddings,
and point-cloud flows have been observed successfully for at least 24 hours.
