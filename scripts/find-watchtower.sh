#!/usr/bin/env bash
# Find the Drone Watchtower sensor on the current LAN.
# The sensor is a VMware guest with a fixed address on the host-only/NAT
# network, so it does not move when the physical LAN changes. Prefer
# drone.local / this helper over a hard-coded IP.
set -euo pipefail

PORT="${WATCHTOWER_PORT:-8080}"
# A station with WATCHTOWER_TOKEN set answers 401 without it. Export the
# token to probe the API; the dashboard prompts for it in the browser.
TOKEN_HEADER=()
if [[ -n "${WATCHTOWER_TOKEN:-}" ]]; then
  TOKEN_HEADER=(-H "X-Watchtower-Token: ${WATCHTOWER_TOKEN}")
fi
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

# The sensor's fixed address on the VMware NAT network (vmnet8).
add "192.168.252.134"

if command -v avahi-resolve >/dev/null 2>&1; then
  for name in drone.local watchtower.local; do
    ip=$(avahi-resolve -n "$name" 2>/dev/null | awk '{print $2; exit}' || true)
    add "$ip"
  done
fi

local_ip() {
  for i in en0 en1 en2 en3 en4 en5 en6 en12; do
    a=$(ipconfig getifaddr "$i" 2>/dev/null || true)
    [[ -n "$a" ]] && { echo "$a"; return 0; }
  done
  hostname -I 2>/dev/null | awk '{print $1}'
}

is_watchtower() {
  curl -s -m "$1" ${TOKEN_HEADER[@]+"${TOKEN_HEADER[@]}"} "http://$2:${PORT}/api/status" 2>/dev/null | grep -q '"dongles"'
}

probe() {
  local host="$1"
  if ! is_watchtower 2 "$host"; then
    # A station that requires a token answers 401 to the probe, so it is
    # there but hidden. Say so instead of reporting "not found".
    if curl -s -m 2 -o /dev/null -w '%{http_code}' "http://${host}:${PORT}/api/status" 2>/dev/null | grep -q '^401$'; then
      echo "FOUND  http://${host}:${PORT}  (needs a token - set WATCHTOWER_TOKEN)"
      echo "       ssh drone@${host}"
      return 0
    fi
    return 1
  fi
  echo "FOUND  http://${host}:${PORT}"
  echo "       ssh drone@${host}"
  curl -s -m 2 ${TOKEN_HEADER[@]+"${TOKEN_HEADER[@]}"} "http://${host}:${PORT}/api/status" \
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
  # The sensor lives on the VMware NAT network (vmnet8). Sweep it too,
  # because the guest may be given a different address by hand.
  if [[ "$net" != "192.168.252" ]]; then
    echo "scanning 192.168.252.0/24 :${PORT} …" >&2
    for i in $(seq 1 254); do
      cand="192.168.252.${i}"
      if is_watchtower 0.2 "$cand"; then
        add "$cand"
      fi
    done
  fi
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
  echo "The sensor is a VMware guest on the host-only network — check that" >&2
  echo "the VM is running, then retry." >&2
  echo "Prefer: http://drone.local:${PORT}" >&2
  exit 1
fi