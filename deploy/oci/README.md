# BridgeGuard on OCI Always Free

Use Ubuntu ARM64 on `VM.Standard.A1.Flex`. The minimum configuration is 2 OCPUs,
12 GB RAM, and a 50 GB boot volume. For this deployment's local Nomic embedding
service plus single-job point-cloud processing, the recommended paid shape is
**8 OCPUs and 36 GB RAM** with **150–200 GB** of total boot/block storage.
At current A1 list rates and the published paid-tenancy A1 allowance, this is
about $40–43/month for compute in a 30–31 day month, leaving a small buffer
under a $50 budget. Confirm the estimate for the tenancy and home region before
saving the shape.

Cloudflare Tunnel connects outbound from the VM, so only SSH needs to be
temporarily reachable while provisioning. Do not expose the application port
to the Internet.

## Upgrade an existing OCI instance

1. Upgrade the tenancy to Pay As You Go; this removes the Always Free shape cap
   while keeping eligible free usage allowances.
2. Create and verify an OCI boot-volume backup.
3. Stop the instance.
4. Open **Compute > Instances > your instance > More actions > Edit shape**.
5. Select **Ampere > VM.Standard.A1.Flex**, then set **8 OCPUs / 36 GB RAM**.
6. Save the shape, start the instance, and verify `docker compose ps`, `/ready`,
   free disk space, and the Cloudflare tunnel.
7. Create a $45 budget alert and a second alert at $50.

If the console estimate exceeds $50 for this tenancy, use **6 OCPUs / 32 GB**
instead. Do not buy a GPU: Cerebras handles generation/vision remotely, while
the server needs general-purpose CPU and memory for TEI embeddings, SQLite, and
point-cloud geometry.

## Server preparation

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo install -d -o "$USER" -g "$USER" /srv/bridgeguard/data
sudo install -d -o "$USER" -g "$USER" /srv/bridgeguard/models
sudo install -d -m 700 /etc/bridgeguard
git clone <repository-url> /opt/bridgeguard
cd /opt/bridgeguard
```

Copy the untracked databases and persistent folders into
`/srv/bridgeguard/data`. Create `/etc/bridgeguard/bridgeguard.env` from
`backend/.env.example`, replacing every placeholder. For single sign-on, set
`BRIDGEGUARD_AUTH_MODE=cloudflare`, `BRIDGEGUARD_OWNER_EMAIL` to the owner's
actual email, the Cloudflare team domain, application AUD, and the server-side
role lists. Keep `BRIDGEGUARD_ALLOWED_EMAILS` and
`BRIDGEGUARD_ALLOWED_DOMAINS` empty while the workspace is limited to the owner
and five editors. Copy `cloudflare.env.example` to
`/etc/bridgeguard/cloudflare.env` and insert the tunnel token.

After copying the data, grant the fixed container user ownership:

```bash
sudo chown -R 10001:10001 /srv/bridgeguard/data
sudo chmod 600 /etc/bridgeguard/bridgeguard.env /etc/bridgeguard/cloudflare.env
```

Start the deployment:

```bash
cd /opt/bridgeguard/deploy/oci
docker compose up -d --build
docker compose ps
docker compose exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready').read().decode())"
```

In Cloudflare, publish the application hostname to `http://app:8000`. Leave the
hostname root (`/`) and `/bridgeguard.png` public so anyone can see the minimal
landing page. In one self-hosted Access application, add protected
public-hostname path entries for:

```text
your-hostname.example/app
your-hostname.example/app/*
your-hostname.example/api/*
```

Attach one exact-email Allow policy containing the owner plus these five
editors:

```text
bsidhom.ahmed@gmail.com
douglas.tintle@dot.nj.gov
vijay.sampat@dot.nj.gov
harjit.bal@dot.nj.gov
mantawy@rowan.edu
bahria76@rowan.edu
```

Do not use an email-domain rule: it would allow every account in that domain.
Use the AUD tag from this Access application for
`CLOUDFLARE_ACCESS_AUD`. Cloudflare supplies the login/SSO screen when an
approved user opens `/app`; BridgeGuard intentionally does not show its shared
password form in Cloudflare mode. Docker's `restart: unless-stopped` policy
brings the application, TEI embedding service, and tunnel back after a reboot.

## Embeddings and point clouds

Compose starts the ARM64 TEI image and pins the exact Nomic v1.5 Hub revision:

```env
TEI_EMBED_BASE_URL=http://embeddings:80
TEI_EMBED_MODEL=nomic-ai/nomic-embed-text-v1.5
TEI_EMBED_REVISION=e9b6763023c676ca8431644204f50c2b100d9aab
TEI_EMBED_DIM=768
```

There is no Ollama or NVIDIA dependency. Copying parsed chunks and image assets
is safe, but old vector matrices must be rebuilt because the pinned TEI runtime
uses Nomic query/document prefixes and normalized outputs. `/ready` stays at 503
if TEI is unavailable, serves the wrong model revision, or returns the wrong
dimension.

Point-cloud processing uses bounded CPU geometry and the configured hosted
multimodal provider; MobileSAM/SAM3 and local vision inference are omitted.
Linux ARM64 wheels are not currently published for `pye57`, so convert E57
scans to validated NPZ on a supported workstation before upload, or maintain
and test a separate ARM source-build toolchain. Store both source and processed
artifacts under `/srv/bridgeguard/data` so they are covered by the normal backup
flow.

## Backups

`backup.sh` uses SQLite's online backup API, so it is safe while the app is
running. Schedule it daily and copy `/srv/bridgeguard/data/backups` off the VM.
Also configure OCI volume backups; a backup stored only on the same disk is not
disaster recovery.

```bash
0 3 * * * /opt/bridgeguard/deploy/oci/backup.sh >> /var/log/bridgeguard-backup.log 2>&1
```

Keep one Gunicorn worker until SQLite, RAG state, and background work have moved
to external services. Also serialize point-cloud jobs explicitly: one worker
can still launch overlapping thread jobs. Large analyses can exceed the
300-second application timeout or an upstream proxy timeout, so use bounded
background jobs with progress/status polling rather than increasing worker
count.
