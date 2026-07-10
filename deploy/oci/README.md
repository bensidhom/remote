# BridgeGuard on OCI Always Free

Use an Ubuntu ARM64 `VM.Standard.A1.Flex` instance with 2 OCPUs, 12 GB RAM, and
at least a 50 GB boot volume. Open inbound TCP 80 and 443 in the OCI security
list. Point a DNS `A` record at the instance before starting Caddy.

## Server preparation

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo install -d -o "$USER" -g "$USER" /srv/bridgeguard/data
sudo install -d -m 700 /etc/bridgeguard
git clone <repository-url> /opt/bridgeguard
cd /opt/bridgeguard
```

Copy the untracked databases and persistent folders into
`/srv/bridgeguard/data`. Create `/etc/bridgeguard/bridgeguard.env` from
`backend/.env.example`, replacing every placeholder. Generate the session secret
with `openssl rand -base64 48`.

Start the deployment:

```bash
cd /opt/bridgeguard/deploy/oci
export DOMAIN=bridgeguard.example.com
docker compose up -d --build
docker compose ps
curl -fsS https://bridgeguard.example.com/ready
```

Persist `DOMAIN` in `/opt/bridgeguard/deploy/oci/.env`. Docker's
`restart: unless-stopped` policy brings both services back after a reboot.

## Backups

`backup.sh` uses SQLite's online backup API, so it is safe while the app is
running. Schedule it daily and copy `/srv/bridgeguard/data/backups` off the VM.
Also configure OCI volume backups; a backup stored only on the same disk is not
disaster recovery.

```bash
0 3 * * * /opt/bridgeguard/deploy/oci/backup.sh >> /var/log/bridgeguard-backup.log 2>&1
```

Keep one Gunicorn worker until SQLite, RAG state, and background work have moved
to external services.

