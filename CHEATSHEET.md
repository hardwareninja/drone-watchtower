# Drone Watchtower — Lab Cheat Sheet

Sensor box: Kali VM `drone@192.168.50.185` (aarch64) · Dashboard: `http://192.168.50.185:8080`
Dev box: Mac (`~/Projects/drone-watchtower`)

---

## Hardware

| Device | Role | Protocol | Notes |
|---|---|---|---|
| **HackRF Pro** | Wideband SDR front end | DJI DroneID (O2/O3/O4) | The core receiver. Attach to Kali. |
| AR9271 WiFi dongle | Monitor-mode 2.4 GHz | Remote ID WiFi (ch 6) | Works, but USB passthrough is flaky — replug when it drops. |
| Sonoff CC2652P (Sniffle) | BLE5 sniffer `/dev/ttyUSB0` | Remote ID Bluetooth | Flashed with Sniffle firmware. |
| AeroScope dongle | CRYP unwrap → session key | O4 telemetry decrypt | Currently on Windows box (`lishu`); runs `dji_dongle_decrypt.py`. |

**Recommended purchases / upgrades**

| Instead of | Buy | Why |
|---|---|---|
| RTL8188EUS | **anything else** | RX calibration broken on mainline driver — unusable |
| AR9271 | **MT7612U dongle** (e.g. Alfa AWUS036ACM) | mainline `mt76` driver, rock-solid monitor mode, dual-band |
| — | **ESP32 dev board** (~US$8) | standalone Remote ID sensor (WiFi+BLE), streams to webapp |
| — | 2.4/5.8/900 MHz **band-specific antennas** | never use "0–6 GHz wideband" claims |
| — | 5.8 GHz band-pass filter + 10–20 dB attenuator | clean captures, protect front end (max −5 dBm!) |
| — | external SSD | IQ = 40 MB/s ≈ 2.4 GB/min at 20 Msps |
| — | powered USB hub | the VM's USB passthrough drops under load |

**Antenna rule:** one whip per band, swap per session (2.4 = DroneID/ELRS, 5.8 = O4, 900 = Crossfire). A 2.4/5.8 diplexer avoids swapping.

---

## Software on Kali (all systemd, boot-persistent)

| Service | Function |
|---|---|
| `watchtower-web` | FastAPI backend + dashboard (:8080), SQLite history at `/opt/watchtower-web/data/` |
| `watchtower-remoteid` | bkerler WiFi receiver (ch 6, ZMQ :4223) |
| `watchtower-rid-beacon` | our DJI RID beacon decoder (ZMQ :4225) |
| `watchtower-remoteid-ble` | Sniffle BLE receiver (ZMQ :4222) |
| `watchtower-remoteid-refresh.timer` | 5-min receiver restart (USB-bounce self-heal) |
| `hexstrike.service` | HexStrike AI tools (:8888) |

Key paths: scanner `/opt/droneid-repo`, turbo `/opt/droneid-tools/remove_turbo_soft`, receiver `/opt/droneid-receiver`, webapp `/opt/watchtower-web`, captures `~/droneid-results/`.

## Software on Mac

- GNU Radio 3.10 + hackrf + SoapySDR (`brew install gnuradio hackrf soapysdr soapyhackrf`)
- Project venv: `.venv/bin/python` (numpy/scipy/zmq/scapy/crypto)
- MCP control: `kali-security` + `hexstrike-ai-drone` servers configured in `~/.cursor/mcp.json`

---

## Everyday commands

```bash
# --- dashboard & scanning ---
open http://192.168.50.185:8080            # UI auto-reloads itself on deploys
curl -X POST http://192.168.50.185:8080/api/scan/start    # or use UI button
curl -X POST http://192.168.50.185:8080/api/scan/stop
curl -X POST http://192.168.50.185:8080/api/replay/start  # demo the archived flight

# --- capture (on Kali, HackRF) ---
scripts/capture/hackrf_survey.sh 5800                    # band sweep (2400|5800|900|all)
scripts/capture/hackrf_record.sh 5756500000 60 o4       # record 60s of raw IQ
.venv/bin/python services/pipeline/plot_sweep.py captures/sweep.csv --peak

# --- offline decode pipeline ---
.venv/bin/python services/pipeline/resample_cs8.py cap.cs8 out.cs8 20000000 15360000
# copy to Kali, then:
python3 /opt/droneid-repo/src/droneid_hackrf_scanner.py --replay out.cs8 \
  --replay-freq 5756500000 --turbo-binary /opt/droneid-tools/remove_turbo_soft --output results/
.venv/bin/python services/pipeline/parse_infp.py <payload_hex>   # INFP structure

# --- history / data ---
curl http://192.168.50.185:8080/api/history/sessions      # session list (JSON)
sqlite3 /opt/watchtower-web/data/history.db              # raw event DB (on Kali)

# --- deploy webapp changes ---
scp -r webapp/backend webapp/frontend drone@192.168.50.185:/tmp/deploy-webapp/
ssh drone@192.168.50.185 'sudo bash /tmp/deploy-webapp/deploy-kali.sh'
# open tabs self-reload (build number tracks frontend files too)
```

## Troubleshooting quick hits

| Symptom | Fix |
|---|---|
| No DroneID packets | DroneID transmits **only when motors are armed** — Remote ID beacons from power-on |
| `wlan0` missing | `sudo modprobe -r ath9k_htc && sudo modprobe ath9k_htc`; if chip hung, physical replug |
| Scanner dead after USB bounce | `hackrf_set_freq failed` → Stop/Start Scan from UI |
| Zero RID but drone on | Avata 2 uses **WiFi beacon transport on ch 6** (not NAN SD, not BLE) — check `watchtower-rid-beacon` journal |
| Dashboard stale | It self-reloads on deploy; otherwise Cmd+Shift+R once |
| Kali disk full | `sudo apt-get clean` (cache grows GBs) |

## Config that matters

- **Station coords + aircraft registry**: `webapp/backend/config.json` → deploy + restart `watchtower-web`
- **Scanner**: LNA 32 / VGA 24, 5.8G raster 5756.5/5776.5/5796.5/5816.5 MHz
- **Drone behavior**: Avata 2 session hash changes per power cycle; RID positions zero until GPS fix (test outdoors)

## The O4 chain (reference)

```
CRYP (SM2-wrapped session key) ──AeroScope dongle──▶ 16-byte session key
INFP (AES-128-CTR, IV = nonce8‖0×8) ──key──▶ serial, drone/pilot/home position,
                                               velocity, yaw, timestamps
```
Without the dongle: session hash fingerprint only. With it: full telemetry.
