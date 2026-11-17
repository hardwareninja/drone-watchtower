# Drone Watchtower

Passive receiver for nearby drones and crewed aircraft. It listens, it does not transmit, jam, or take control of a link. The dashboard is a browser on port 8080.

## Hardware

### Required

| Piece | Role |
|---|---|
| Raspberry Pi 5, 8 GB, or a 64-bit Linux machine with a powered USB hub | Runs the receivers and the dashboard. Pi 4 works, but DroneID correlation is slow. |
| HackRF One | Hops between ELRS, Crossfire, SiK telemetry, and 5.8 GHz video carriers. |
| ESP32-C5 flashed with `firmware/esp32c5-rid` | Remote ID. 2.4 GHz Wi-Fi beacons and Bluetooth, one band at a time, any USB port. |
| One antenna per band you care about | 2.4 GHz, 915 MHz or 868 MHz, 5.8 GHz, 1090 MHz. A single "0–6 GHz" whip is a poor substitute. |

### Recommended

| Piece | Why |
|---|---|
| HackRF Pro | Stays on DJI DroneID while the One keeps hopping. With only one HackRF, DroneID waits. |
| RTL-SDR (RTL2832/2838) | ADS-B on 1090 MHz. The dashboard reads `http://127.0.0.1:8091/api/aircraft`. |
| u-blox 7 USB (`1546:01a7`) | Moves the station marker onto a real GPS fix. Until then the map uses this machine's network location. |
| USB UVC capture dongle | Relays the analog 5.8 GHz goggle receiver's picture into the video panel. |
| 5.8 GHz band-pass filter and a 10–20 dB attenuator | Protects the HackRF. Keep the input below −5 dBm. |
| Powered USB hub | The radios brown out on an unpowered hub or a flaky VM passthrough. |

Dongles are recognized by USB identity (HackRF serial, vendor/product for the C5, GPS, RTL-SDR, and the video grabber). Moving a radio to another port does not change which service owns it.

The Wi-Fi monitor dongle and the Sonoff Sniffle stick are not part of this build. Remote ID is the ESP32-C5.

## Signal specification

Receive only. Ranges are the ones the station tunes; a transmitter outside them is invisible.

| Signal | Band | What is shown | What stays hidden |
|---|---|---|---|
| Remote ID (ASTM F3411 and DJI beacon) | 2.4 GHz Wi-Fi beacons and BLE, one at a time. Optional 5 GHz beacon/NAN when `c5_wifi5` is true | Identity, and position when the aircraft sends one | Nothing from these open beacons is decrypted; they are already in the clear |
| DJI DroneID O2/O3/O4 | 2.4 GHz and 5.8 GHz | Packet presence, INFP structure when the scanner decodes it | O4 CRYP session contents. That unwrap is not in this build |
| ExpressLRS | 2.4 GHz FHSS | Link present, packet rate | Airframe, sticks, and telemetry. Frames are encrypted with the binding phrase |
| TBS Crossfire | 868 MHz and 915 MHz FHSS | Link present when the hop pattern is there | Payload |
| SiK / RFD900-style MAVLink | 433 MHz and 902–928 MHz GFSK, default air rate 64 kbit/s, sync `2D D4`, PN9 whitening | Radio presence and netid. Plain MAVLink (heartbeat, GPS, attitude, battery) when the payload is not encrypted | HUD fields when the radio uses an encrypted or non-MAVLink payload |
| Analog FPV video | 5.8 GHz, 40-channel table | Carrier, channel name, and the USB grabber picture | — |
| ADS-B | 1090 MHz | Hex, callsign, position, altitude, speed | Aircraft with no position yet are omitted |
| ADS-B (network) | Aggregator feeds | The same fields, tagged `NET` | Nothing is decrypted; these are tracks an aggregator heard, not this box |
| Station GPS | u-blox NMEA | Station latitude and longitude after a fix | — |

The HackRF One does not listen to all of those at once. While scanning, it cycles ELRS, Crossfire, SiK, then the 5.8 GHz video survey. The HackRF Pro, when attached, stays on DroneID.

The ESP32-C5 is dual-band and not dual-concurrent. By default it alternates 2.4 GHz Wi-Fi beacons (and NAN action frames on the tuned channel) with BLE; it is never on both. Set `c5_wifi5` to `true` in `config.json` and restart `watchtower-c5-rid` to also hop 5 GHz Wi-Fi beacon/NAN, including channel 149 and DFS channels the country code allows the chip to tune. While it is on 5 GHz, 2.4 GHz Remote ID is missed, and the other way around. That hop is not HackRF 5.8 GHz DroneID or analog FPV. The dashboard dot is green only for the band the radio is on now.

## Limitations

- No transmit, no jamming, no injection, no stick decode, and no recovery of an unknown binding phrase.
- ExpressLRS telemetry is not decoded. Knowing the binding phrase is necessary and not yet sufficient: that decrypt path is not wired in.
- A SiK hop can be strong on the spectrum and still produce no vehicle row when the bytes after the sync word are not plain MAVLink.
- DJI DroneID is sent when the motors are armed. Remote ID is sent from power-on. A disarmed DJI aircraft can appear as Remote ID only.
- O4 telemetry that is wrapped as CRYP is not turned into a position here.
- One HackRF cannot watch 2.4, 915, and 5.8 at the same time. Dwell time is shared.
- Remote ID positions are empty until the aircraft has its own GPS fix.
- The C5 cannot receive 2.4 GHz and 5 GHz together. `c5_wifi5` defaults to false. Turning it on spends dwell on 5 GHz and drops 2.4 GHz packets during those dwells.
- Broadcast fields (Remote ID serials and self-id text, DroneID markers, ADS-B callsigns, operator text) are untrusted. They are charset-constrained before they are stored, HTML-escaped in the dashboard, and encoded again for CSV, KML, and GPX.
- The station dot uses this machine's network location until the u-blox has a fix. `config.json` does not store coordinates.
- ADS-B needs a view of the sky. Indoor, the RTL-SDR can be healthy and still report no aircraft.
- Analog picture requires the goggle receiver and the USB grabber. The HackRF only reports the video carrier.
- The aircraft registry in `config.json` is a display name for a serial you already know. It does not filter other aircraft out. A newly seen serial is resolved against that local list once and then cached, including when the list has no name for it. There is no network lookup.
- ADS-B tracks tagged `NET` came from a public aggregator, not from this station's receiver. They are listed so the panel is useful when no RTL-SDR is attached; they are never presented as local reception.
- The `UAS?` badge is a heuristic over ADS-B fields (no callsign, no registration, no ICAO identity, low and slow, hovering). It marks a candidate worth looking at, not a confirmed drone.

## Running without a radio

The box stays useful with no SDR attached:

- **Network ADS-B.** `adsb_network_feeds` in `config.json` lists aggregator feeds to poll. Set it to `[]` to turn them off.
- **Offline replay.** `POST /api/replay/start?path=<file>&freq_hz=<hz>` runs a recorded IQ file through the same detector and decoder as live air, and pushes the frames to the dashboard as they are decoded. Threshold overrides (`root600`, `root147`) let the same capture be judged against several gates.

  The path must sit inside a capture directory: the DroneID results tree, the replay inbox, the ELRS capture directory, or the station user's home. Add more with `replay_roots` in `config.json`. A path outside those roots is refused, and the error never echoes a resolved path back.

  ```bash
  curl -X POST "http://<host>:8080/api/replay/start?path=/home/drone/replay.cs8&freq_hz=5756500000"
  curl -X POST "http://<host>:8080/api/replay/start?path=/home/drone/replay.cs8&freq_hz=5756500000&root600=0.45&root147=0.35"
  ```

- **MAVLink over UDP.** Frames sent to port 14550 or 14555 appear as vehicles. This is how a ground station or a companion computer feeds the dashboard.

## Access control

The dashboard listens on the LAN, so it is a control plane on a network the
drone operator does not own: any neighbour on the segment can read the
detection history — a geolocated log of every drone and crewed aircraft seen
near the station — or stop the scan.

**No token is required by default.** A station that asks for a token on every
page load is a station people end up leaving unlocked, and the operator
reading the dashboard is the whole point.

Two ways to close the exposure, in order of preference:

**Bind loopback and use an SSH tunnel.** Change the `--host` in the systemd
unit to `127.0.0.1` and reach the dashboard over the tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 drone@drone.local
```

**Or require a token.** `webapp/deploy-kali.sh` generates one when
`WATCHTOWER_REQUIRE_TOKEN=1` is set, stores it in `/etc/watchtower/env`
(readable by the service, not by the webapp user), and prints it once. Put
it in `WATCHTOWER_TOKEN` in the environment, or `api_token` in `config.json`.

The browser asks for the token on the first `401` and remembers it; `curl`
calls send it as `X-Watchtower-Token:`, `Authorization: Bearer <token>`, or
the `watchtower_token` cookie. The websocket takes it from the cookie,
because a browser cannot set headers on a WebSocket and a URL would write it
into the server's access log.

## Radios

The HackRF serials are not in the repository — they identify the operator's
own hardware. Set them once per box, either in the environment or in
`config.json`:

```json
"hackrf_serials": {
  "droneid": "<the DroneID tuner serial>",
  "hop": "<the hopping tuner serial>"
}
```

`HACKRF_DRONEID` and `HACKRF_HOP` override the config. The dashboard reports
the radios as unassigned until they are set. `hackrf_roles` maps a serial to a
role if the Pro and the One swap jobs.

## Detection tuning

The DroneID decoder is the expensive stage, so candidates are gated on their ZC correlation scores before it runs. The gate is tuned against a labeled corpus of 162 archived detector candidates (111 genuine frames, 51 CRC failures) kept in `tests/data/zc_score_corpus.json`.

| Gate (root600 / root147) | Genuine frames kept | CRC failures reaching the decoder |
|---|---|---|
| 0.45 / 0.35 (previous default) | 111/111 | 51/51 — none rejected |
| 0.55 / 0.50 (current default) | 109/111 | 6/51 |

Rejected candidates never reach the decoder, so the CPU they would have cost goes into dwell time on real signals. Set `scanner_root600_threshold` and `scanner_root147_threshold` in `config.json` to change the gate; `pytest tests/test_zc_gate.py` re-checks the numbers above.

## Installation

Tested path: Raspberry Pi OS Bookworm 64-bit, or Kali ARM64, on the machine that will hold the radios.

1. Flash the OS. Enable SSH. Use a 32 GB card or larger. Boot, then:

   ```bash
   sudo apt update && sudo apt full-upgrade
   ```

2. Clone this repository on that machine:

   ```bash
   git clone https://github.com/hardwareninja/drone-watchtower.git
   cd drone-watchtower
   ```

3. Plug the radios into a powered hub, then into the Pi. Any USB socket is fine.

4. Install the scanner, the dashboard, and the C5 bridge:

   ```bash
   sudo bash scripts/deploy/raspi-install.sh
   ```

   This installs HackRF tools, the DroneID scanner under `/opt/droneid-repo`, and copies the web app to `/opt/watchtower-web`. It enables `watchtower-web` (port 8080) and `watchtower-c5-rid`.

5. Flash the ESP32-C5 from `firmware/esp32c5-rid` with ESP-IDF before expecting Remote ID. The bridge opens whichever `ttyACM` the board enumerates as (`303a:1001`).

6. Set the station name and, if you want a friendly name, the aircraft registry. The map finds the station from the network, then from the u-blox:

   ```bash
   sudo nano /opt/watchtower-web/backend/config.json
   sudo systemctl restart watchtower-web
   ```

7. For ADS-B, run an RTL-SDR 1090 decoder that serves `/api/aircraft` on `127.0.0.1:8091`. The dashboard polls that URL. The installer does not start that decoder.

8. Open `http://drone.local:8080`. Start Scan. Arm the aircraft you expect to hear on DroneID.

### Finding the sensor box

The address moves when the sensor is a virtual machine on a changing
network, so do not hard-code it. From your laptop:

```bash
bash scripts/find-watchtower.sh
```

It prints the dashboard URL and the SSH command. When the sensor is a
VMware guest on the host's NAT network the address is fixed at
`192.168.252.134`, and the installer sets that address so it survives
reboots and network changes.

History is SQLite at `/opt/watchtower-web/data/history.db`.

Event timestamps are stored in UTC and displayed in the browser's own timezone, so a machine set to UTC+8 sees UTC+8. The Events tab date pickers work in local days as well, and the CSV export writes local time with its offset. The same filter accepts `fmt=kml` or `fmt=gpx`. Text cells are encoded for that format, and a CSV cell that would be a spreadsheet formula is prefixed so it stays text.

## Companion runner

`scripts/watchtower_enhancer.py` sits beside a running dashboard. It does not change the receivers, and it does not start unless you run it. CoT, ZeroMQ, and proximity output stay off when the matching flag is omitted.

```bash
python scripts/watchtower_enhancer.py --demo
python scripts/watchtower_enhancer.py --bench
python scripts/watchtower_enhancer.py --ws-url ws://127.0.0.1:8080/ws --token "$WATCHTOWER_TOKEN"
python scripts/watchtower_enhancer.py --cot-host 239.2.3.1 --cot-port 6969
python scripts/watchtower_enhancer.py --zmq-bind tcp://127.0.0.1:4224
```

* **Proximity.** Live DroneID and Remote ID positions are compared with 1090 and network ADS-B. `--horiz-warn-m` (default 3000) and `--vert-warn-m` (default 304.8, about 1000 ft) set the thresholds. Remote ID altitude and ADS-B `alt_ft` are not the same reference: one may be above the ground and the other barometric or geometric. The 3D figure is a warning aid, not certified TCAS.
* **CoT.** UDP only, and only when `--cot-host` is set. Unicast or multicast. This is the only Cursor-on-Target emitter; the web app does not open another.
* **ZeroMQ.** A PUB socket only when `--zmq-bind` is set. Use `tcp://127.0.0.1:4224`. The events include positions, so a bind on every interface (`tcp://*:4224`) is refused.
* **Token.** `--token` or `WATCHTOWER_TOKEN` is sent as `X-Watchtower-Token`, `Authorization: Bearer`, and the `watchtower_token` cookie. It is not put in the WebSocket URL.

`--bench` runs the offline ZC detector and channel estimator under `src/`. It does not open a radio.

## Research enhancements (`src/`)

Offline / research modules from `drone-watchtower-enhancement-tasks.md`. The live sensor box still runs via `webapp/` + the luyii HackRF scanner; these modules are the ZC / channel / fusion / ML experiments.

```bash
pip install -r requirements.txt
pytest tests/ -v
python scripts/train_classifier.py --demo   # synthetic IQ → models/signal_classifier.pkl
PYTHONPATH=src python -c "from main import EnhancedDroneWatchtower; ..."
```

Live fusion (Task 2.1) is also wired into the dashboard: RID + DroneID sessions with the same serial merge in `webapp/frontend/app.js`, and the C5 firmware joins BLE Basic ID ↔ Location via a per-MAC cache.

## Testing

```bash
pip install -r requirements.txt
pytest tests/ -v
```

The suite covers the ZC gate against the labeled corpus (`tests/test_zc_gate.py`), the network ADS-B mapping and distance maths (`tests/test_adsb_network.py`), the small-UAS heuristic (`tests/test_adsb_classify.py`), MAVLink payload decoding (`tests/test_mavlink_decode.py`), and station positioning (`tests/test_station_location.py`).

Backend code is importable in tests: `WATCHTOWER_DB` overrides the history database path, and the database directory is created on startup rather than at import.
