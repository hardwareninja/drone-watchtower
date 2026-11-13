#!/usr/bin/env bash
# Find the Drone Watchtower sensor on the current LAN.
# DHCP IP changes with every router — prefer drone.local / this helper.
set -euo pipefail

PORT="${WATCHTOWER_PORT:-8080}"
HOSTS=()

add() {
  local h="${1:-}"
  [[ -n "$h" ]] || return 0
  local x
  for x in "${HOSTS[@]:-}"; do
    [[ "$x" == "$h" ]] && return 0
  done
  HOSTS+=("$h")
}

add "drone.local"
add "drone"
add "watchtower.local"
add "watchtower"

if command -v avahi-resolve >/dev/null 2>&1; then
  for name in drone.local watchtower.local; do
    ip=$(avahi-resolve -n "$name" 2>/dev/null | awk '{print $2; exit}' || true)
    add "$ip"
  done
fi

local_ip() {
  ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}'
}

is_watchtower() {
  curl -s -m "$1" "http://$2:${PORT}/api/status" 2>/dev/null | grep -q '"dongles"'
}

probe() {
  local host="$1"
  if ! is_watchtower 2 "$host"; then
    return 1
  fi
  echo "FOUND  http://${host}:${PORT}"
  echo "       ssh drone@${host}"
  curl -s -m 2 "http://${host}:${PORT}/api/status" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print('       armed',d.get('armed'),'phase',d.get('phase'),'ready',d.get('ready')); p=d.get('problems') or [];
print('       problems','; '.join(p) if p else '-')" 2>/dev/null || true
  return 0
}

sweep_subnet() {
  local iface_ip net i cand
  iface_ip=$(local_ip || true)
  [[ -n "${iface_ip:-}" ]] || return 0
  net=$(echo "$iface_ip" | awk -F. '{print $1"."$2"."$3}')
  echo "scanning ${net}.0/24 :${PORT} …" >&2
  for i in $(seq 1 254); do
    cand="${net}.${i}"
    if is_watchtower 0.2 "$cand"; then
      add "$cand"
    fi
  done
}

found=0
for h in "${HOSTS[@]}"; do
  if probe "$h"; then
    found=1
    break
  fi
done

if [[ "$found" -eq 0 ]]; then
  sweep_subnet
  for h in "${HOSTS[@]}"; do
    if probe "$h"; then
      found=1
      break
    fi
  done
fi

if [[ "$found" -eq 0 ]]; then
  echo "No Watchtower found on this LAN." >&2
  echo "Join the same Wi-Fi as the sensor, then retry." >&2
  echo "Prefer: http://drone.local:${PORT}" >&2
  exit 1
fi
