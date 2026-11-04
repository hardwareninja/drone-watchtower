#!/usr/bin/env bash
# Record raw IQ for offline demodulation.
# Default target: DJI O4 5.8 GHz channels, 20 Msps 8-bit (40 MB/s ≈ 2.4 GB/min).
#
# Usage: ./hackrf_record.sh <freq_hz> [seconds] [out_prefix]
#   e.g. ./hackrf_record.sh 5740000000 60 o4-test
#
# GAIN SAFETY: keep the source at distance or use an attenuator. HackRF max
# input is -5 dBm. Start with lna=32 vga=24, amp off (-a 0); enable amp (-a 1)
# only if SNR is insufficient.
set -euo pipefail

FREQ="${1:?usage: $0 <freq_hz> [seconds] [out_prefix]}"
SECONDS_CAP="${2:-30}"
PREFIX="${3:-capture}"
RATE="${RATE:-20000000}"      # 20 Msps covers a 20 MHz O4 channel
LNA="${LNA:-32}"
VGA="${VGA:-24}"
AMP="${AMP:-0}"               # 1 enables the 14 dB IF amp — only if needed

OUT_DIR="${OUT_DIR:-./captures}"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/${PREFIX}-$(date +%Y%m%d-%H%M%S).cs8"

echo "[*] Recording $SECONDS_CAP s @ ${RATE} sps, freq $FREQ Hz -> $OUT"
echo "[*] 8-bit I/Q stream = $((RATE * 2 / 1000000)) MB/s on disk"

hackrf_transfer \
  -r "$OUT" \
  -f "$FREQ" \
  -s "$RATE" \
  -l "$LNA" \
  -g "$VGA" \
  -a "$AMP" \
  -b 17500000 \
  -n $(( RATE * SECONDS_CAP ))   # stop after N samples (no manual Ctrl-C timing)

echo "[*] Done: $OUT"
ls -lh "$OUT"
