#!/usr/bin/env bash
# Deploy the Drone Watchtower webapp on the Kali sensor box. Run ON KALI as root.
set -euo pipefail

DEST=/opt/watchtower-web
# Replace only the code; data/ holds history.db and must survive a deploy.
SRC=/tmp/deploy-webapp
# Capture demods must live next to app.py (uvicorn WorkingDirectory=backend).
if [ -f "$SRC/../scripts/capture/elrs_demod.py" ]; then
  cp -f "$SRC/../scripts/capture/elrs_demod.py" "$SRC/backend/elrs_demod.py"
fi
if [ -f "$SRC/../scripts/capture/mavlink_demod.py" ]; then
  cp -f "$SRC/../scripts/capture/mavlink_demod.py" "$SRC/backend/mavlink_demod.py"
fi
mkdir -p "$DEST"
rm -rf "$DEST/backend" "$DEST/frontend"
cp -r "$SRC/backend" "$SRC/frontend" "$DEST/"
for req in app.py elrs_demod.py mavlink_demod.py c5_rid_bridge.py usb_locate.py; do
  if [ ! -f "$DEST/backend/$req" ]; then
    echo "FATAL: missing $DEST/backend/$req — refuse to restart a half deploy"
    exit 1
  fi
done
mkdir -p "$DEST/data"
chown -R drone:drone "$DEST"

echo "[*] Installing Python deps..."
pip3 install --break-system-packages -q fastapi "uvicorn[standard]"

echo "[*] Creating systemd service..."
cat > /etc/systemd/system/watchtower-web.service <<'EOF'
[Unit]
Description=Drone Watchtower detection webapp
After=network.target

[Service]
Type=simple
User=drone
Group=drone
WorkingDirectory=/opt/watchtower-web/backend
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable watchtower-web.service >/dev/null 2>&1 || true
systemctl restart watchtower-web.service
sleep 2
systemctl --no-pager status watchtower-web.service | head -4
curl -s -o /dev/null -w "[*] Webapp HTTP status: %{http_code}\n" http://localhost:8080/
echo "[*] Done: http://$(hostname -I | awk '{print $1}'):8080"
