#!/bin/sh
# Live Levels Socials provider stats from the official API. The API only answers an allowlisted server,
# so the call runs there over SSH (the server keeps the integration key in ~/.levels-provider-key) and
# the answer is saved next to this script as providers-api.json. Exit 0 = fresh file; on failure the old file stays.
# Settings (in .env or the environment): PIP_SSH_KEY (default ~/.ssh/LevelsNew2026.pem), PIP_SSH_HOST.
cd "$(dirname "$0")" || exit 1
[ -f ../.env ] && . ../.env 2>/dev/null
KEY="${PIP_SSH_KEY:-$HOME/.ssh/LevelsNew2026.pem}"
HOST="${PIP_SSH_HOST:?Set PIP_SSH_HOST in .env}"
tmp=$(mktemp) || exit 1
if ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$HOST" ./provider-stats.sh > "$tmp" 2>/dev/null \
   && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['data']['items']" "$tmp"; then
  mv "$tmp" providers-api.json && echo "ok $(python3 -c "import json; d=json.load(open('providers-api.json'))['data']; print(len(d['items']), 'providers as of', d['asOf'])")"
else
  rm -f "$tmp"; echo "failed: kept the old providers-api.json" >&2; exit 1
fi
