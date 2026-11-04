#!/usr/bin/env bash
# Drone Watchtower — full Kali deployment. Run ON KALI as root.
# Installs: SDR stack (GNU Radio, HackRF, SoapySDR), kali-security MCP server,
#           HexStrike AI (systemd service).
set -euo pipefail

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ---------------------------------------------------------------- disk housekeeping
log "Cleaning apt cache..."
apt-get clean
rm -rf /var/cache/apt/archives/*.deb 2>/dev/null || true
df -h / | tail -1

# ---------------------------------------------------------------- SDR stack
log "Installing SDR stack (GNU Radio, gr-osmosdr, SoapySDR HackRF module)..."
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  gnuradio gr-osmosdr soapysdr-module-hackrf hackrf \
  python3-pip python3-venv
log "SDR stack installed. HackRF test:"
hackrf_info || echo "  (no HackRF attached to this machine right now — fine)"

# ---------------------------------------------------------------- kali-security MCP
log "Deploying kali-security MCP server to /opt/kali-mcp..."
mkdir -p /opt/kali-mcp
cp /tmp/deploy/server.py /opt/kali-mcp/server.py
pip3 install --break-system-packages mcp 2>/dev/null || pip3 install mcp
# smoke test: server starts and exits on stdin close
timeout 5 python3 /opt/kali-mcp/server.py </dev/null >/dev/null 2>&1 && log "kali-security server OK" \
  || log "kali-security smoke test: exited (check manually)"

# ---------------------------------------------------------------- HexStrike AI
log "Installing HexStrike AI to /opt/hexstrike-ai..."
apt-get install -y git
rm -rf /opt/hexstrike-ai
git clone https://github.com/0x4m4/hexstrike-ai.git /opt/hexstrike-ai
cd /opt/hexstrike-ai
python3 -m venv hexstrike-env
./hexstrike-env/bin/pip install --upgrade pip -q
./hexstrike-env/bin/pip install -r requirements.txt -q

cat > /etc/systemd/system/hexstrike.service <<'EOF'
[Unit]
Description=HexStrike AI tool server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/hexstrike-ai
ExecStart=/opt/hexstrike-ai/hexstrike-env/bin/python3 /opt/hexstrike-ai/hexstrike_server.py --port 8888
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now hexstrike.service
sleep 3
systemctl --no-pager status hexstrike.service | head -6 || true
curl -s -o /dev/null -w "hexstrike HTTP status: %{http_code}\n" http://localhost:8888/ || true

# ---------------------------------------------------------------- done
log "Deployment complete."
df -h / | tail -1
