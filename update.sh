#!/usr/bin/env bash
# Update CountPulse to the latest code. Type on the VPS:
#   git -C countpulse pull && bash countpulse/update.sh
# Keeps your API key, setup, nginx site and HTTPS certificate as they are.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
if [ -f /etc/nginx/sites-available/countpulse.conf ] || [ -f /etc/nginx/conf.d/countpulse.conf ]; then
  FILE=docker-compose.nginx.yml
else
  FILE=docker-compose.yml
fi
nohup docker compose -f "$FILE" up -d --build > /root/update.log 2>&1 &
echo "Updating CountPulse in the background (about 1 minute)."
echo "Check progress: tail /root/update.log"
