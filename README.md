# Drone Watchtower

Passive receiver for nearby drones and crewed aircraft. It listens, it does not transmit, jam, or take control of a link. The dashboard is a browser on port 8080.

## Hardware

### Required

| Piece | Role |
|---|---|
| Raspberry Pi 5, 8 GB, or a 64-bit Linux machine with a powered USB hub | Runs the receivers and the dashboard. Pi 4 works, but DroneID correlation is slow. |
| HackRF One | Hops between ELRS, Crossfire, SiK telemetry, and 5.8 GHz video carriers. |
| ESP32-C5 flashed with `firmware/esp32c5-rid` | Remote ID. Wi-Fi beacons and Bluetooth, any USB port. |
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
| Remote ID (ASTM F3411 and DJI beacon) | 2.4 GHz Wi-Fi beacons and BLE advertisements | Identity, and position when the aircraft sends one | Nothing from these open beacons is decrypted; they are already in the clear |
| DJI DroneID O2/O3/O4 | 2.4 GHz and 5.8 GHz | Packet presence, INFP structure when the scanner decodes it | O4 CRYP session contents. That unwrap is not in this build |
| ExpressLRS | 2.4 GHz FHSS | Link present, packet rate | Airframe, sticks, and telemetry. Frames are encrypted with the binding phrase |
| TBS Crossfire | 868 MHz and 915 MHz FHSS | Link present when the hop pattern is there | Payload |
| SiK / RFD900-style MAVLink | 433 MHz and 902–928 MHz GFSK, default air rate 64 kbit/s, sync `2D D4`, PN9 whitening | Radio presence and netid. Plain MAVLink (heartbeat, GPS, attitude, battery) when the payload is not encrypted | HUD fields when the radio uses an encrypted or non-MAVLink payload |
| Analog FPV video | 5.8 GHz, 40-channel table | Carrier, channel name, and the USB grabber picture | — |
| ADS-B | 1090 MHz | Hex, callsign, position, altitude, speed | Aircraft with no position yet are omitted |
| Station GPS | u-blox NMEA | Station latitude and longitude after a fix | — |

The HackRF One does not listen to all of those at once. While scanning, it cycles ELRS, Crossfire, SiK, then the 5.8 GHz video survey. The HackRF Pro, when attached, stays on DroneID.

## Limitations

- No transmit, no jamming, no injection, no stick decode, and no recovery of an unknown binding phrase.
- ExpressLRS telemetry is not decoded. Knowing the binding phrase is necessary and not yet sufficient: that decrypt path is not wired in.
- A SiK hop can be strong on the spectrum and still produce no vehicle row when the bytes after the sync word are not plain MAVLink.
- DJI DroneID is sent when the motors are armed. Remote ID is sent from power-on. A disarmed DJI aircraft can appear as Remote ID only.
- O4 telemetry that is wrapped as CRYP is not turned into a position here.
- One HackRF cannot watch 2.4, 915, and 5.8 at the same time. Dwell time is shared.
- Remote ID positions are empty until the aircraft has its own GPS fix.
- The station dot uses this machine's network location until the u-blox has a fix. `config.json` does not store coordinates.
- ADS-B needs a view of the sky. Indoor, the RTL-SDR can be healthy and still report no aircraft.
- Analog picture requires the goggle receiver and the USB grabber. The HackRF only reports the video carrier.
- The aircraft registry in `config.json` is a display name for a serial you already know. It does not filter other aircraft out.

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

8. Open `http://<pi-address>:8080`. Start Scan. Arm the aircraft you expect to hear on DroneID.

History is SQLite at `/opt/watchtower-web/data/history.db`.

## Research enhancements (`src/`)

Offline / research modules from `drone-watchtower-enhancement-tasks.md`. The live sensor box still runs via `webapp/` + the luyii HackRF scanner; these modules are the ZC / channel / fusion / ML experiments.

```bash
pip install -r requirements.txt
pytest tests/ -v
python scripts/train_classifier.py --demo   # synthetic IQ → models/signal_classifier.pkl
PYTHONPATH=src python -c "from main import EnhancedDroneWatchtower; ..."
```

Live fusion (Task 2.1) is also wired into the dashboard: RID + DroneID sessions with the same serial merge in `webapp/frontend/app.js`, and the C5 firmware joins BLE Basic ID ↔ Location via a per-MAC cache.
