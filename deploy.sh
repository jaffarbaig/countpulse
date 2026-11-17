#!/usr/bin/env bash
# One-command deploy for a fresh Ubuntu/Debian VPS (e.g. Hostinger KVM).
# Usage:  DOMAIN=countpulse.example.com YT_API_KEY=xxxx bash deploy.sh
# Point the domain's A record at this VPS's IP first; Caddy then gets HTTPS automatically.
set -euo pipefail
: "${DOMAIN:?Set DOMAIN, e.g. DOMAIN=countpulse.oneclikai.in}"
: "${YT_API_KEY:?Set YT_API_KEY (YouTube Data API v3 key)}"
REPO="${REPO:-https://github.com/jaffarbaig/countpulse.git}"
DIR=/opt/countpulse

apt-get update -y && apt-get install -y git curl
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 "$REPO" "$DIR"; fi
cd "$DIR"
printf 'DOMAIN=%s\nYT_API_KEY=%s\nPOLL_SECONDS=30\n' "$DOMAIN" "$YT_API_KEY" > .env
chmod 600 .env
docker compose up -d --build

echo
echo "CountPulse is starting at https://$DOMAIN (HTTPS can take a minute on first run)."
echo "Logs: cd $DIR && docker compose logs -f"
