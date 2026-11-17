#!/usr/bin/env bash
# One-command deploy for a fresh Ubuntu/Debian VPS (e.g. Hostinger KVM).
#
# Easiest (type this on the VPS):
#   git clone https://github.com/jaffarbaig/countpulse && bash countpulse/deploy.sh
# Then open https://<domain>/setup and paste your YouTube API key with the code it prints.
#
# Optional env: DOMAIN (default countpulse.oneclikai.in), YT_API_KEY (skips the /setup page).
# Point the domain's A record at this VPS's IP first; Caddy then gets HTTPS automatically.
set -euo pipefail
DOMAIN="${DOMAIN:-countpulse.oneclikai.in}"
REPO="${REPO:-https://github.com/jaffarbaig/countpulse.git}"

# Use the folder this script lives in if it's a git checkout, else clone to /opt/countpulse.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$HERE/.git" ]; then DIR="$HERE"; else DIR=/opt/countpulse; fi
LOG=/root/deploy.log

# Safety check: don't fight an existing web server for ports 80/443.
if ss -tlnp | grep -qE ':(80|443)\s'; then
  if ! (command -v docker >/dev/null && docker ps --format '{{.Names}}' | grep -q 'countpulse-caddy'); then
    echo "STOPPED: ports 80/443 are already used by another program:"
    ss -tlnp | grep -E ':(80|443)\s'
    echo "Nothing was changed. Send a screenshot of this to Claude."
    exit 1
  fi
fi

# Keep an existing .env (and its key/code) on re-runs.
if [ -f "$DIR/.env" ]; then
  CODE="$(grep -oP '^SETUP_CODE=\K.*' "$DIR/.env" || true)"
else
  CODE="$(shuf -i 100000-999999 -n 1)"
fi

install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y && apt-get install -y git curl
  command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
  if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 "$REPO" "$DIR"; fi
  cd "$DIR"
  if [ ! -f .env ]; then
    printf 'DOMAIN=%s\nSETUP_CODE=%s\nYT_API_KEY=%s\nPOLL_SECONDS=30\n' "$DOMAIN" "$CODE" "${YT_API_KEY:-}" > .env
    chmod 600 .env
  fi
  docker compose up -d --build
  echo
  echo "DONE. CountPulse is running at https://$DOMAIN"
  echo "First time? Open https://$DOMAIN/setup  (setup code: $CODE)"
}

# Run the slow part in the background so a dropped terminal can't kill it.
nohup bash -c "set -e; $(declare -f install); DIR='$DIR' DOMAIN='$DOMAIN' REPO='$REPO' CODE='$CODE' YT_API_KEY='${YT_API_KEY:-}' install" > "$LOG" 2>&1 &

echo "=============================================="
echo " Installing CountPulse in the background (3-5 min)."
echo " Your setup code:  $CODE"
echo " When done, open:  https://$DOMAIN/setup"
echo " Check progress:   tail $LOG"
echo "=============================================="
