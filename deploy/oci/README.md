# BridgeGuard on OCI Always Free

Use an Ubuntu ARM64 `VM.Standard.A1.Flex` instance with 2 OCPUs, 12 GB RAM, and
at least a 50 GB boot volume. Cloudflare Tunnel connects outbound from the VM,
so only SSH needs to be temporarily reachable while provisioning. Do not expose
the application port to the Internet.

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
`backend/.env.example`, replacing every placeholder. For single sign-on, set
`BRIDGEGUARD_AUTH_MODE=cloudflare`, the Cloudflare team domain, application AUD,
and the server-side role lists. Copy `cloudflare.env.example` to
`/etc/bridgeguard/cloudflare.env` and insert the tunnel token.

Start the deployment:

```bash
cd /opt/bridgeguard/deploy/oci
docker compose up -d --build
docker compose ps
docker compose exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready').read().decode())"
```

In Cloudflare, publish the application hostname to `http://app:8000`, then add a
self-hosted Access application for the same hostname. Docker's `restart:
unless-stopped` policy brings both services back after a reboot.

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
