"""Backfill real ELRS captures from tonight into the Watchtower history DB.

One row per capture session: session elrs-2g4, source elrs, marker with the
rate, best verified SNR, and the capture window's center frequency.
"""
import datetime
import glob
import json
import sqlite3

DB = "/opt/watchtower-web/data/history.db"

rows = []
for d in sorted(glob.glob("/home/drone/elrs-captures/2*")):
    try:
        pkts = [json.loads(l) for l in open(d + "/detections.jsonl")]
    except OSError:
        continue
    ok = [p for p in pkts if p.get("sync") == [8, 16] and p.get("downchirps_ok")]
    if not ok:
        continue
    ts = d.rsplit("/", 1)[-1]                      # local Hong Kong time
    t = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S") - datetime.timedelta(hours=8)
    snr = max(p["snr_db"] for p in ok)
    sf = max({p["sf"] for p in ok})
    rate = {5: "500 Hz", 6: "250 Hz", 7: "150/100 Hz", 8: "50 Hz"}.get(sf, "ELRS")
    rows.append((t.isoformat() + "+00:00", "elrs-2g4", "elrs",
                 f"ELRS {rate}", 2420.2, snr, None, None, None, 1))

conn = sqlite3.connect(DB)
conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
conn.commit()
print("backfilled", len(rows), "ELRS history rows")