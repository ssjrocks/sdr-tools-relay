#!/usr/bin/env bash
# Installs or updates the relay list on the ssjrocks.com VPS (needs the VPS's root key).
# Adds the nginx location once, after /api/reports/, keeping a backup and undoing it if
# `nginx -t` fails.
set -euo pipefail
cd "$(dirname "$0")"
VPS="${SDR_RELAY_VPS:-root@130.185.249.109}"
KEY=(-o IdentitiesOnly=yes -i "$HOME/.ssh/id_ed25519")
ssh "${KEY[@]}" "$VPS" 'mkdir -p /opt/sdr-relay-directory'
scp -q "${KEY[@]}" relay_directory.py "$VPS:/opt/sdr-relay-directory/relay_directory.py"
scp -q "${KEY[@]}" sdr-relay-directory.service "$VPS:/etc/systemd/system/sdr-relay-directory.service"
scp -q "${KEY[@]}" nginx-location.conf "$VPS:/opt/sdr-relay-directory/nginx-location.conf"
ssh "${KEY[@]}" "$VPS" 'set -e
chmod 644 /opt/sdr-relay-directory/relay_directory.py
systemctl daemon-reload
systemctl enable -q sdr-relay-directory
systemctl restart sdr-relay-directory
sleep 1
systemctl is-active sdr-relay-directory
curl -sf http://127.0.0.1:8793/api/sdr-relay/v1/health; echo
conf=/etc/nginx/sites-available/ssjrocks.com
if ! grep -q "location /api/sdr-relay/" "$conf"; then
  backup="/root/ssjrocks.com.nginx.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$conf" "$backup"
  python3 - "$conf" /opt/sdr-relay-directory/nginx-location.conf <<PY
import sys
conf, add = sys.argv[1], open(sys.argv[2]).read()
s = open(conf).read()
i = s.index("location /api/reports/")
end = s.index("\n    }\n", i) + len("\n    }\n")
block = "".join(l + "\n" for l in add.splitlines() if not l.startswith("#"))
open(conf, "w").write(s[:end] + "\n" + block + s[end:])
PY
  if nginx -t 2>&1; then
    systemctl reload nginx
    echo "nginx location added (backup $backup)"
  else
    cp "$backup" "$conf"
    echo "nginx -t failed: config restored from $backup" >&2
    exit 1
  fi
fi'
curl -sf https://ssjrocks.com/api/sdr-relay/v1/health && echo && curl -sf https://ssjrocks.com/api/sdr-relay/v1/relays && echo
