#!/usr/bin/env bash
# Drone Watchtower — Raspberry Pi sensor installer
# Run AS ROOT, FROM the drone-watchtower repo checkout, on a 64-bit OS
# (Raspberry Pi OS Bookworm 64-bit or Kali ARM64). Idempotent-ish: safe to re-run.
#
#   sudo bash scripts/deploy/raspi-install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
echo "[*] repo at $REPO"
command -v git >/dev/null || { echo "git missing"; exit 1; }
[ -f "$REPO/webapp/backend/app.py" ] || { echo "run from the drone-watchtower repo root"; exit 1; }

export DEBIAN_FRONTEND=noninteractive

echo "[1/8] System packages (this takes a few minutes)..."
apt-get update -y
apt-get install -y --no-install-recommends \
  git build-essential autoconf automake libtool pkg-config \
  hackrf libhackrf-dev gnuradio gr-osmosdr soapysdr-module-hackrf \
  python3 python3-pip python3-numpy python3-scipy python3-venv \
  libtalloc-dev libnl-3-dev libnl-genl-3-dev libzmq5 libusb-1.0-0-dev \
  sqlite3 || true

echo "[2/8] luyii DroneID scanner repo..."
[ -d /opt/droneid-repo ] || git clone -q --depth 1 \
  https://github.com/luyii-code-1/dji-ocusync-droneid-research.git /opt/droneid-repo

echo "[3/8] TurboFEC (ARM/NEON) + turbo adapter..."
if [ ! -f /opt/droneid-tools/remove_turbo_soft ]; then
  rm -rf /opt/turbofec /opt/droneid-tools
  git clone -q https://github.com/udevlog/turbofec-arm.git /opt/turbofec
  (cd /opt/turbofec && autoreconf -i >/dev/null 2>&1 && ./configure >/dev/null 2>&1 \
     && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1 && ldconfig)
  mkdir -p /opt/droneid-tools
  cc -O2 -o /opt/droneid-tools/remove_turbo_soft \
     /opt/droneid-repo/tools/remove_turbo_soft.c -lturbofec
fi

echo "[4/8] Python packages for the dashboard and the C5 bridge..."
pip3 install --break-system-packages -q numpy pyzmq fastapi "uvicorn[standard]" 2>/dev/null \
  || pip3 install -q numpy pyzmq fastapi "uvicorn[standard]"

echo "[5/8] Webapp deploy to /opt/watchtower-web..."
mkdir -p /opt/watchtower-web/data
rm -rf /opt/watchtower-web/backend /opt/watchtower-web/frontend
cp -r "$REPO/webapp/backend" "$REPO/webapp/frontend" /opt/watchtower-web/
sed -i 's|/home/drone/droneid-results|/root/droneid-results|' /opt/watchtower-web/backend/app.py
mkdir -p /root/droneid-results

echo "[6/8] systemd units..."
install_unit() { cat > "/etc/systemd/system/$1.service" <<< "$2"; }
install_unit watchtower-web "[Unit]
Description=Drone Watchtower webapp
After=network.target
[Service]
Type=simple
WorkingDirectory=/opt/watchtower-web/backend
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target"
install_unit watchtower-c5-rid "[Unit]
Description=ESP32-C5 Remote ID serial bridge
After=network.target
[Service]
Type=simple
User=root
Group=dialout
WorkingDirectory=/opt/watchtower-web/backend
ExecStart=/usr/bin/python3 -u /opt/watchtower-web/backend/c5_rid_bridge.py
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target"
systemctl daemon-reload
systemctl enable --now watchtower-web watchtower-c5-rid

echo "[7/8] Device checks..."
hackrf_info 2>/dev/null | head -4 || echo "    (no HackRF attached yet)"

echo "[8/8] Done."
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

================================================================
 Drone Watchtower sensor is installed.
 Dashboard : http://${IP:-<pi-ip>}:8080
 Station   : edit /opt/watchtower-web/backend/config.json
             (lat/lon + aircraft registry), then:
             systemctl restart watchtower-web
 Hardware  : plug HackRF + ESP32-C5 Remote ID + RTL-SDR + u-blox
             use a POWERED USB hub — HackRF is hungry
 Next      : arm the drone, open the dashboard, Start Scan
================================================================
EOF
