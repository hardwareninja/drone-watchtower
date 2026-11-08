"""Offline test of the analog VTX video demodulator against a live capture."""
import base64
import sys

import numpy as np
from PIL import Image

FS = 12_000_000
LINE = int(FS * 63.556e-6)          # 762
SYNC_MIN = int(FS * 3.0e-6)
ACTIVE_OFF = int(FS * 10.5e-6)
ACTIVE = 576

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/vtx.iq"
raw = np.fromfile(path, dtype=np.int8)
print("samples", len(raw) // 2, "seconds", round(len(raw) / 2 / FS, 2))

a = raw.astype(np.float32) / 127.0
iq = a[0::2] + 1j * a[1::2]
print("rms", round(float(np.sqrt((np.abs(iq) ** 2).mean())), 3))

d = np.angle(iq[1:] * np.conj(iq[:-1])) * (FS / (2 * np.pi))
print("deviation p1/p99 MHz:", round(float(np.percentile(d, 1)) / 1e6, 2),
      round(float(np.percentile(d, 99)) / 1e6, 2))

sync_level = float(np.percentile(d, 1))
below = d < sync_level + 1.2e5
idx = np.flatnonzero(below.astype(np.int8))
starts = []
run_start = idx[0]
prev = idx[0]
for k in idx[1:]:
    if k - prev > 2:
        if prev - run_start >= SYNC_MIN:
            starts.append(run_start)
        run_start = k
    prev = k
if prev - run_start >= SYNC_MIN:
    starts.append(run_start)
print("sync candidates", len(starts))

lines = []
for i, s0 in enumerate(starts):
    nxt = starts[i + 1] if i + 1 < len(starts) else None
    if nxt and not (LINE * 90 // 100 < nxt - s0 < LINE * 115 // 100):
        continue
    if s0 + ACTIVE_OFF + ACTIVE + 8 > len(d):
        continue
    lines.append(d[s0: s0 + ACTIVE_OFF + ACTIVE + 8])
print("valid lines", len(lines))

rows = [ln[ACTIVE_OFF: ACTIVE_OFF + ACTIVE] for ln in lines if len(ln) >= ACTIVE_OFF + ACTIVE]
if len(rows) < 50:
    print("TOO FEW LINES")
    sys.exit(1)
img = np.vstack(rows)
black = float(np.percentile(img, 15))
white = float(np.percentile(img, 95))
print("black/white kHz:", round(black / 1e3, 1), round(white / 1e3, 1))
out = np.clip((img - black) / max(white - black, 1e-6), 0, 1)
frame = (out * 255).astype(np.uint8)
print("frame", frame.shape)
Image.fromarray(frame, mode="L").save("/tmp/vtx-frame.jpg", quality=70)
with open("/tmp/vtx-frame.b64", "w") as f:
    f.write(base64.b64encode(open("/tmp/vtx-frame.jpg", "rb").read()).decode())
print("saved /tmp/vtx-frame.jpg")