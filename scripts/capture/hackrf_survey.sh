#!/usr/bin/env bash
# Spectrum survey for drone links: 2.4 GHz (ELRS/DroneID), 5.8 GHz (O4/OcuSync),
# 900 MHz (Crossfire). Requires hackrf tools (hackrf_sweep).
#
# Usage: ./hackrf_survey.sh [band]   band = 2400 | 5800 | 900 | all
set -euo pipefail

BAND="${1:-all}"
OUT_DIR="${OUT_DIR:-./captures}"
mkdir -p "$OUT_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)

# Safety defaults: modest gains, amp off unless signal is weak.
COMMON_ARGS=(-1 10)  # exit after ~10 sweeps worth of time; adjust for longer sessions

sweep_2400() {
  echo "[*] Sweeping 2400–2483 MHz (ELRS 2.4G, DJI DroneID 2.4G, WiFi-band Remote ID)..."
  hackrf_sweep -f 2400:2483 -w 100000 -l 32 -g 24 "${COMMON_ARGS[@]}" \
    | tee "$OUT_DIR/sweep-2400-$STAMP.csv"
}

sweep_5800() {
  echo "[*] Sweeping 5725–5875 MHz (DJI O4 / OcuSync high band)..."
  hackrf_sweep -f 5725:5875 -w 100000 -l 32 -g 24 "${COMMON_ARGS[@]}" \
    | tee "$OUT_DIR/sweep-5800-$STAMP.csv"
}

sweep_900() {
  echo "[*] Sweeping 902–928 MHz (TBS Crossfire)..."
  hackrf_sweep -f 902:928 -w 100000 -l 32 -g 24 "${COMMON_ARGS[@]}" \
    | tee "$OUT_DIR/sweep-900-$STAMP.csv"
}

case "$BAND" in
  2400) sweep_2400 ;;
  5800) sweep_5800 ;;
  900)  sweep_900 ;;
  all)  sweep_2400; sweep_5800; sweep_900 ;;
  *) echo "Usage: $0 [2400|5800|900|all]"; exit 1 ;;
esac

echo "[*] Sweep CSVs saved in $OUT_DIR — inspect with gnuplot or services/pipeline/plot_sweep.py"
