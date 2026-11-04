#!/usr/bin/env bash
# Install HexStrike AI on the Kali box. Run ON KALI as root (or with sudo).
set -euo pipefail

INSTALL_DIR=/opt/hexstrike-ai

echo "[*] Installing prerequisites..."
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip

echo "[*] Cloning hexstrike-ai into $INSTALL_DIR..."
rm -rf "$INSTALL_DIR"
git clone https://github.com/0x4m4/hexstrike-ai.git "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "[*] Creating venv and installing dependencies..."
python3 -m venv hexstrike-env
./hexstrike-env/bin/pip install --upgrade pip
./hexstrike-env/bin/pip install -r requirements.txt

echo "[*] Creating systemd service for the HexStrike server..."
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
echo "[*] Server status:"
systemctl --no-pager -l status hexstrike.service | head -12

echo
echo "[*] Done. The tool server is listening on 127.0.0.1:8888 (localhost only)."
echo "    Cursor connects by running the MCP client over SSH:"
echo "    ssh -T user@kali /opt/hexstrike-ai/hexstrike-env/bin/python3 /opt/hexstrike-ai/hexstrike_mcp.py --server http://localhost:8888"