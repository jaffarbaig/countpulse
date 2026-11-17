#!/usr/bin/env bash
# One-command deploy for an Ubuntu/Debian VPS (e.g. Hostinger KVM).
#
# Type this on the VPS:
#   git clone https://github.com/jaffarbaig/countpulse && bash countpulse/deploy.sh
# Then open https://<domain>/setup and paste your YouTube API key with the code it prints.
#
# Two modes, picked automatically:
#   caddy - ports 80/443 are free: Caddy container serves HTTPS.
#   nginx - host nginx already owns 80/443 (other sites): app runs on localhost,
#           a new nginx site is added for DOMAIN only, HTTPS via certbot. Other sites untouched.
#
# Optional env: DOMAIN (default countpulse.oneclikai.in), YT_API_KEY (skips the /setup page).
set -euo pipefail
DOMAIN="${DOMAIN:-countpulse.oneclikai.in}"
REPO="${REPO:-https://github.com/jaffarbaig/countpulse.git}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$HERE/.git" ]; then DIR="$HERE"; else DIR=/opt/countpulse; fi
LOG=/root/deploy.log

# ---- pick mode ---------------------------------------------------------------
PORTS="$(ss -tlnp | grep -E ':(80|443)\s' || true)"
if [ -z "$PORTS" ] || (command -v docker >/dev/null && docker ps --format '{{.Names}}' | grep -q 'countpulse-caddy'); then
  MODE=caddy
elif ! echo "$PORTS" | grep -vq '"nginx"'; then
  MODE=nginx
else
  echo "STOPPED: ports 80/443 are used by a program this script doesn't handle:"
  echo "$PORTS"
  echo "Nothing was changed. Send a screenshot of this to Claude."
  exit 1
fi

# ---- settings kept across re-runs ---------------------------------------------
getenv() { grep -oP "^$1=\K.*" "$DIR/.env" 2>/dev/null || true; }
CODE="$(getenv SETUP_CODE)"; [ -n "$CODE" ] || CODE="$(shuf -i 100000-999999 -n 1)"
APP_PORT="$(getenv APP_PORT)"
if [ -z "$APP_PORT" ]; then
  APP_PORT=8010
  while ss -tln | grep -qE "[:.]$APP_PORT\s"; do APP_PORT=$((APP_PORT+1)); done
fi

install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y && apt-get install -y git curl
  command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
  if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 "$REPO" "$DIR"; fi
  cd "$DIR"
  if [ ! -f .env ]; then
    printf 'DOMAIN=%s\nSETUP_CODE=%s\nYT_API_KEY=%s\nAPP_PORT=%s\nPOLL_SECONDS=30\n' "$DOMAIN" "$CODE" "${YT_API_KEY:-}" "$APP_PORT" > .env
    chmod 600 .env
  fi

  if [ "$MODE" = caddy ]; then
    docker compose up -d --build
  else
    docker compose -f docker-compose.nginx.yml up -d --build
    if [ -d /etc/nginx/sites-available ]; then
      CONF=/etc/nginx/sites-available/countpulse.conf; LINK=/etc/nginx/sites-enabled/countpulse.conf
    else
      CONF=/etc/nginx/conf.d/countpulse.conf; LINK=""
    fi
    if [ ! -f "$CONF" ]; then
      sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$APP_PORT/g" "$DIR/nginx-site.conf" > "$CONF"
      if [ -n "$LINK" ]; then ln -sf "$CONF" "$LINK"; fi
      if ! nginx -t; then
        echo "nginx config test failed - removing CountPulse site, your other sites are unchanged."
        rm -f "$CONF"; if [ -n "$LINK" ]; then rm -f "$LINK"; fi
        exit 1
      fi
      systemctl reload nginx
    fi
    command -v certbot >/dev/null || apt-get install -y certbot python3-certbot-nginx
    if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect --register-unsafely-without-email; then
      echo "HTTPS certificate installed."
    else
      echo "HTTPS not set up yet (is the DNS A record for $DOMAIN pointing at this server?)."
      echo "Fix DNS, then run:  certbot --nginx -d $DOMAIN --redirect"
    fi
  fi
  echo
  echo "DONE. CountPulse is running at https://$DOMAIN"
  echo "First time? Open https://$DOMAIN/setup  (setup code: $CODE)"
}

# Run the slow part in the background so a dropped terminal can't kill it.
nohup bash -c "set -e; $(declare -f install); DIR='$DIR' DOMAIN='$DOMAIN' REPO='$REPO' CODE='$CODE' APP_PORT='$APP_PORT' MODE='$MODE' YT_API_KEY='${YT_API_KEY:-}' install" > "$LOG" 2>&1 &

echo "=============================================="
echo " Installing CountPulse in the background (3-5 min)."
echo " Mode: $MODE"
echo " Your setup code:  $CODE"
echo " When done, open:  https://$DOMAIN/setup"
echo " Check progress:   tail $LOG"
echo "=============================================="
