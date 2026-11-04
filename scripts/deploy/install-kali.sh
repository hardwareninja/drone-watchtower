#!/usr/bin/env bash
# Run this ON THE KALI BOX as root (or with sudo) to install dependencies.
set -euo pipefail

echo "[*] Installing security tools..."
apt-get update -y
apt-get install -y nmap whois dnsutils exploitdb python3-pip

echo "[*] Installing Python MCP SDK..."
pip3 install --break-system-packages mcp

echo "[*] Done. Test the server manually with:"
echo "    python3 /opt/kali-mcp/server.py"