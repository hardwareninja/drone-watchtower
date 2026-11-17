#!/usr/bin/env bash
# Deploy the Drone Watchtower webapp on the Kali sensor box. Run ON KALI as root.
set -euo pipefail

DEST=/opt/watchtower-web
# Replace only the code; data/ holds history.db and must survive a deploy.
SRC=/tmp/deploy-webapp
# Capture demods must live next to app.py (uvicorn WorkingDirectory=backend).
# scripts/capture/ is the single source of truth for these; the copies under
# webapp/backend/ are kept in sync in the repo so a plain backend/ deploy
# still has them.
if [ -f "$SRC/../scripts/capture/elrs_demod.py" ]; then
  cp -f "$SRC/../scripts/capture/elrs_demod.py" "$SRC/backend/elrs_demod.py"
fi
if [ -f "$SRC/../scripts/capture/mavlink_demod.py" ]; then
  cp -f "$SRC/../scripts/capture/mavlink_demod.py" "$SRC/backend/mavlink_demod.py"
fi
mkdir -p "$DEST"
# Keep the live station config (HackRF serials, FR24 token, aircraft map).
# A repo config.json with empty serials must never wipe the box.
KEEP_CFG=""
if [ -f "$DEST/backend/config.json" ]; then
  KEEP_CFG=$(mktemp)
  cp -a "$DEST/backend/config.json" "$KEEP_CFG"
fi
rm -rf "$DEST/backend" "$DEST/frontend"
cp -r "$SRC/backend" "$SRC/frontend" "$DEST/"
if [ -n "$KEEP_CFG" ]; then
  cp -a "$KEEP_CFG" "$DEST/backend/config.json"
  rm -f "$KEEP_CFG"
  echo "[*] Preserved existing backend/config.json"
fi
for req in app.py elrs_demod.py mavlink_demod.py c5_rid_bridge.py c5_rid.py \
           usb_locate.py adsb_network.py adsb_classify.py aircraft_registry.py \
           track_export.py textsan.py zc_gate.py; do
  if [ ! -f "$DEST/backend/$req" ]; then
    echo "FATAL: missing $DEST/backend/$req — refuse to restart a half deploy"
    exit 1
  fi
done
mkdir -p "$DEST/data"
chown -R drone:drone "$DEST"

echo "[*] Installing Python deps..."
pip3 install --break-system-packages -q fastapi "uvicorn[standard]"

# --- API token (optional) --------------------------------------------------
# The dashboard listens on the LAN, so it is a control plane on a network
# the operator does not own. A token closes that, at the cost of one
# paste in the browser.
#
# It is off by default: a station that needs a token on every page load
# is a station people end up leaving unlocked. Set WATCHTOWER_REQUIRE_TOKEN=1
# to turn it on, or bind the service to 127.0.0.1 and use an SSH tunnel.
TOKEN_FILE=/etc/watchtower/token
TOKEN=""
if [[ "${WATCHTOWER_REQUIRE_TOKEN:-0}" == "1" ]]; then
  if [[ -f "$TOKEN_FILE" ]]; then
    TOKEN=$(cat "$TOKEN_FILE")
    echo "[*] Keeping the existing API token."
  else
    TOKEN=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
    mkdir -p /etc/watchtower
    echo "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "[*] Generated a new API token."
  fi
else
  echo "[*] No API token (set WATCHTOWER_REQUIRE_TOKEN=1 to require one)."
  sudo rm -f "$TOKEN_FILE" 2>/dev/null || true
fi

echo "[*] Creating systemd service..."
cat > /etc/systemd/system/watchtower-web.service <<EOF
[Unit]
Description=Drone Watchtower detection webapp
After=network.target

[Service]
Type=simple
User=drone
Group=drone
WorkingDirectory=/opt/watchtower-web/backend
EnvironmentFile=-/etc/watchtower/env
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# The token is read from the environment by the service, not from a file
# the webapp user can read. The file always exists: the unit points at
# it. Preserve HACKRF_* (and any other operator env) across deploys —
# rewriting the file to empty used to unassign both radios.
sudo mkdir -p /etc/watchtower
ENV_FILE=/etc/watchtower/env
KEEP_ENV=$(mktemp)
if [[ -f "$ENV_FILE" ]]; then
  grep -v '^WATCHTOWER_TOKEN=' "$ENV_FILE" > "$KEEP_ENV" || true
fi
{
  cat "$KEEP_ENV"
  if [[ -n "$TOKEN" ]]; then
    printf 'WATCHTOWER_TOKEN=%s\n' "$TOKEN"
  fi
} | sudo tee "$ENV_FILE" >/dev/null
rm -f "$KEEP_ENV"
sudo chmod 640 "$ENV_FILE"
sudo chown root:drone "$ENV_FILE"

systemctl daemon-reload
systemctl enable watchtower-web.service >/dev/null 2>&1 || true
systemctl restart watchtower-web.service
# C5 bridge loads c5_rid_bridge.py from the same tree — bounce it too.
if systemctl list-unit-files watchtower-c5-rid.service >/dev/null 2>&1; then
  systemctl restart watchtower-c5-rid.service || true
fi
sleep 2
systemctl --no-pager status watchtower-web.service | head -4
curl -s -o /dev/null -w "[*] Webapp HTTP status: %{http_code}\n" http://localhost:8080/
echo "[*] Done: http://$(hostname -I | awk '{print $1}'):8080"
if [[ -n "$TOKEN" ]]; then
  echo
  echo "[*] API token (needed by the dashboard and by any curl to /api/*):"
  echo "    $TOKEN"
  echo "[*] It is also stored on the box at $TOKEN_FILE."
fi