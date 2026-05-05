# UHRK Telemetry System Current Functionality

Last updated: 2026-05-04

This document describes what the UHRK telemetry node/data logger and ground
station currently do. It reflects the current source in this repository and the
deployed Booster/GC setup.

## System Overview

The system has two main parts:

- Telemetry node/data logger: Raspberry Pi node mounted on a rocket stage. It
  reads GPS, IMU, barometer, and gyro data, logs locally, detects simple flight
  events, transmits compact telemetry over LoRa, and listens for LoRa commands.
- Ground station: Raspberry Pi 5 with an SX1303 LoRa gateway HAT. It receives
  LoRa packets through the Semtech UDP packet forwarder, writes GC-side logs,
  produces the web dashboard, performs GC-side filtering/fusion, and sends LoRa
  downlink commands.

The dashboard is normally served at:

```text
http://10.42.0.1:8000/
```

The GC control API listens on port `8090`. The node still has a WiFi HTTP
control API on port `8091` for bench/service functions, but flight-relevant pad
state and shutdown commands now use LoRa.

## Telemetry Node / Data Logger

### Sensors Read

The node reads:

- GPS latitude and longitude
- GPS altitude
- GPS fix status
- Satellites used in the fix
- Satellites in view
- GPS DOP values for local logs
- Barometric altitude
- IMU-derived altitude
- Raw acceleration: `ax`, `ay`, `az` in `m/s^2`
- Raw gyroscope: `gx`, `gy`, `gz` in `deg/s`

The node code is currently set up as one deployable snapshot under:

```text
remote/uhrkboo/zenith_node/
```

The configured default device ID is `0`, which maps to Booster. The packet and
dashboard also support device IDs `1` and `2` for Sustainer and Payload Bay.

### LoRa Telemetry Transmission

The node transmits one compact binary telemetry packet over LoRa on each
cadence cycle.

Current default LoRa settings:

- Frequency: `868.1 MHz`
- Bandwidth: `125 kHz`
- Spreading factor: `10`
- Coding rate: `4/5`
- Sync word: `0x34`
- Preamble length: `8`
- Node TX power: `20 dBm`

Normal cadence is `1.0 s`.

When the node receives the `On Pad Launch Ready` LoRa command, it switches to a
faster cadence:

- Base launch-ready cadence: `0.45 s`
- Per-device slot offset: `0.12 s * device_id`

That slot offset is intended to leave airtime for multiple nodes.

### Telemetry Packet Contents

Each LoRa telemetry payload is 39 bytes, big-endian.

Fields:

- `device_id`
- `seq`
- latitude scaled by `1e7`
- longitude scaled by `1e7`
- GPS altitude scaled by `100`
- barometric altitude scaled by `100`
- IMU altitude scaled by `100`
- GPS status
- satellites used/in-view packed into one byte
- `ax`, `ay`, `az` scaled by `10`
- `gx`, `gy`, `gz` scaled by `10`
- event flags

Satellite counts use one byte:

- Upper nibble: satellites in view
- Lower nibble: satellites used

### Local Node Flight Log

The node writes its own JSONL flight log on the node Pi.

Default path:

```text
/home/uhrkboo/flight_logs/node_<hostname>_<timestamp>.jsonl
```

The node log records:

- logger start/stop events
- every telemetry packet sent
- raw sensor values used in each packet
- GPS status, sats used, sats in view, and DOP values
- pad state
- event flags
- base64 copy of the transmitted payload
- LoRa commands received
- shutdown dry-run and shutdown commit records
- time sync requests over the node HTTP API

Logs are flushed and fsynced on every write. During a real LoRa shutdown command
the node logs the shutdown request and commit, stops the telemetry loop, closes
the log file, and then schedules the Pi shutdown.

### Node Flight Event Logic

The node derives one current flight event flag at a time.

Supported event states:

- Burn active
- Burnout
- Stage separation
- Drogue deployed
- Main deployed
- Landed
- On Pad Idle
- On Pad Launch Ready

Important behavior:

- Flight event progression is gated behind `On Pad Launch Ready`.
- Launch requires sustained boost-like acceleration for multiple samples.
- Recovery events are gated behind burnout and a minimum altitude change.
- If no flight event is active, the node transmits the current pad state.
- If a flight event is active, flight state takes priority over pad state.

The current detector is intentionally conservative. It is useful for development
and early tests, but recovery-event thresholds still need real flight validation.

### LoRa Commands Received By Node

The node listens for GC LoRa commands between telemetry transmissions. Commands
use a short packet with:

- magic: `UHRKC1`
- target device
- command ID
- command value
- nonce
- checksum

Supported LoRa commands:

- Command `1`: pad state
  - value `0`: On Pad Idle
  - value `1`: On Pad Launch Ready
- Command `2`: shutdown
  - value `0`: dry-run/test only, logs but does not shut down
  - value `1`: real shutdown, logs and powers the node down

Commands may target a specific node or broadcast to `0xFF`. The GC currently
broadcasts pad state and shutdown commands. The node tracks the last nonce and
ignores duplicate repeats.

### Node WiFi HTTP API

The node still exposes a local HTTP API on port `8091`.

Current endpoints:

- `GET /api/pad-state`
- `POST /api/pad-state`
- `GET /api/time-sync/status`
- `POST /api/time-sync`

This API is still useful on the bench, especially for time sync and pad-state
service testing. Shutdown is intentionally LoRa-only from the GC dashboard.

## Ground Station

### LoRa Packet Reception

The GC uses an SX1303 gateway HAT through the Semtech UDP packet forwarder.

The backend:

- Binds to the packet-forwarder UDP port
- Handles packet-forwarder `PUSH_DATA`, `PULL_DATA`, `TX_ACK`, and ACK messages
- Decodes UHRK 39-byte telemetry payloads
- Captures LoRa metadata such as RSSI, SNR, frequency, data rate, and timestamp
- Updates per-stage state for Booster, Sustainer, and Payload Bay
- Writes dashboard JSON to `telemetry_latest.json`

### GC Flight Log

The GC writes its own JSONL log.

Default path:

```text
/home/uhrkgc/uhrk_site/flight_logs/gc_<timestamp>.jsonl
```

The GC log records:

- backend start/stop events
- received LoRa packets
- decoded telemetry
- LoRa receive metadata
- altitude-zero actions
- settings updates
- pad-state LoRa command attempts
- shutdown requests and commits
- time sync operations

Like the node logger, GC log writes are flushed and fsynced.

### Dashboard Views

The dashboard has four main tabs:

- General
- Booster
- Sustainer
- Payload

The General tab shows all three stages together. Each stage tab shows a detailed
view for that individual node.

Current displayed node information includes:

- Connection status
- Device ID
- Sequence number
- Last seen time
- Last update UTC
- GPS status
- Satellites used | in view
- GPS quality
- Position
- Relative fused altitude
- Relative GPS altitude
- Relative barometric altitude
- GPS altitude
- Barometric altitude
- IMU altitude
- Fused altitude
- Kalman altitude
- Kalman velocity
- IMU-derived velocity
- Kalman acceleration
- Raw linear acceleration
- Acceleration magnitude
- Raw acceleration axes
- Raw gyro axes
- RSSI | SNR
- Frequency
- Data rate
- Current event
- Readiness status

The dashboard also shows GC GPS status, GC satellites, GC position, and GC
altitude when the ground-station GPS has usable data.

### Charts

The dashboard currently charts:

- Relative altitude
- Velocity
- IMU velocity
- Acceleration
- Gyroscope

General view plots each stage as one line per chart.

Individual stage views show more detailed datasets, including:

- Kalman fused relative altitude
- GPS relative altitude
- Barometric relative altitude
- IMU relative altitude
- Fused absolute altitude
- Kalman vertical velocity
- IMU vertical velocity
- Kalman linear acceleration
- Raw `|a| - g`
- Raw acceleration magnitude
- Raw acceleration axes
- Raw gyro axes

The barometric altitude line is intentionally made visually prominent because
baro is currently the most trusted altitude source.

### GC-Side Altitude, Velocity, And Acceleration Processing

GPS is intentionally not used for altitude or velocity estimation right now.
It is treated as a separate positioning system.

The GC currently uses:

- Barometric altitude
- Gravity-corrected acceleration magnitude, `|a| - g`
- A 3-state vertical Kalman filter

Kalman state:

- altitude
- vertical velocity
- linear acceleration

Current GC-side outputs include:

- `fusedAlt`: Kalman fused altitude
- `fusedRelAlt`: relative fused altitude after altitude zero
- `kalmanAlt`
- `kalmanRelAlt`
- `kalmanVelocity`
- `kalmanAccel`
- `rawLinearAccel`

Stationary deadbands are used so a still node can report zero acceleration and
zero velocity even when raw acceleration magnitude is slightly offset.

Important limitation: the current acceleration measurement is based on
acceleration magnitude minus gravity, not a full attitude-resolved vertical
acceleration. It is a practical improvement over raw baro differencing, but it
is not yet a full inertial navigation solution.

### GPS Handling

For nodes:

- GPS status is displayed separately.
- Satellites used and satellites in view are split.
- GPS position is quality-gated before being accepted for display.
- GPS altitude is displayed but not used in fused altitude or velocity.
- GPS position/altitude jumps can be rejected by GC-side settings.

For the ground station:

- GC GPS is read independently from the GC serial GPS.
- GC position and altitude are displayed when available.
- GC GPS time can be used as a time source when the antenna has valid time.

### Altitude Zero

Altitude zero is currently GC-side only.

The dashboard calls:

```text
POST /api/altitude-zero
```

The GC stores offsets in:

```text
/home/uhrkgc/uhrk_site/altitude_zero.json
```

The node keeps transmitting its normal altitude values. The GC applies the zero
offset to produce relative altitude displays and logs.

This means altitude zero does not depend on WiFi to the node and does not need a
LoRa command, because it is applied to GC-side processing.

### Settings Page

The dashboard settings drawer currently supports:

- Pad telemetry state controls
- Altitude zero controls
- Clock sync controls
- Sensor processing parameters
- Flight event parameter storage
- Recovery chute parameter storage

Sensor processing settings are active GC-side settings. They affect filtering,
deadbands, and outlier rejection on the GC backend.

Current active sensor settings include:

- gravity reference
- stationary acceleration deadband
- velocity smoothing alpha
- altitude noise deadband
- stationary velocity deadband
- max baro step
- max GPS position step
- max GPS altitude step
- Kalman baro variance
- Kalman acceleration variance
- Kalman altitude process noise
- Kalman velocity process noise
- Kalman acceleration process noise

Flight event and chute settings are currently stored on the GC and displayed in
the settings UI. The node event detector still uses the node-side constants in
`remote/uhrkboo/zenith_node/config.py`. Those event/chute settings are not yet
sent to nodes over LoRa.

### LoRa Downlink Commands From GC

The GC currently sends these LoRa downlink commands:

- Pad state broadcast
- Shutdown broadcast

The GC sends downlinks through the SX1303 packet forwarder using `PULL_RESP`.
It repeats each command several times and includes a nonce so nodes can ignore
duplicate command repeats.

Current default downlink settings:

- Frequency: `868.1 MHz`
- Power: `14 dBm`
- Data rate: `SF10BW125`
- Coding rate: `4/5`
- Preamble: `8`
- Repeats: `3`
- Repeat delay: `1.0 s`

### Pad State Control

The dashboard can send:

- On Pad Idle
- On Pad Launch Ready

Transport: LoRa broadcast.

When the node receives `On Pad Launch Ready`, it:

- Sets its pad state flag
- Transmits the launch-ready event when no flight event is active
- Switches to the faster launch-ready telemetry cadence
- Enables flight event progression

### Shutdown Control

The dashboard shutdown panel has multiple safety layers:

- Arm checkbox
- Exact confirmation phrase: `SHUTDOWN UHRK`
- Hold button for at least 3 seconds
- Separate Test button for dry-run

Transport to nodes: LoRa broadcast.

Current behavior:

- Test sends LoRa shutdown command value `0`.
- Nodes log the dry-run command but do not shut down.
- Real shutdown sends LoRa shutdown command value `1`.
- Nodes log the command, log shutdown commit, stop telemetry, close logs, and
  shut down.
- The GC schedules its own shutdown only if the LoRa command was successfully
  queued to the gateway.

Because the command is a broadcast, a real shutdown is intended to shut down all
nodes that hear it, plus the GC.

### Clock Sync

Clock sync currently uses WiFi/HTTP for nodes.

Supported GC behavior:

- If GC GPS time is available, the backend can use it as the authoritative time
  source.
- The dashboard can manually sync from the browser clock.
- The GC can set its own system time.
- The GC can call node `/api/time-sync` endpoints over WiFi.

Clock sync is not currently sent to nodes over LoRa.

### Web UI Theme And Layout

The dashboard supports:

- Dark mode
- Light mode
- Four-tab layout
- All-stage general overview
- Detailed individual stage tabs
- Settings drawer
- Shutdown safety panel
- System status panel
- Active warning summaries
- Packet-rate display per node
- Data export links for telemetry JSON, current GC log, CSV, KML, and health JSON

The current layout is intentionally kept stable: controls and data presentation
should not be moved casually because the dashboard is becoming an operational
interface, not a mock-up.

### Diagnostics And Exports

The GC control API exposes read-only diagnostics and export endpoints:

```text
GET /api/version
GET /api/health
GET /api/logs
GET /api/logs/current
GET /api/export/csv
GET /api/export/kml
```

`/api/health` reports backend version, uptime, service states, important file
paths, downlink status, ground-station GPS state, stage snapshots, and active
warnings.

`/api/export/csv` and `/api/export/kml` are generated from the current GC JSONL
flight log. The KML includes any valid non-zero node GPS tracks and the ground
station track, so GPS outages appear as missing segments rather than false
`0,0` lines.

### Warning Logic

The backend adds warning summaries to telemetry output and health output.
Current warnings cover:

- missing or stale telemetry
- nodes missing from the expected three-stage set
- GC GPS not fixed or not yet streaming
- node GPS seeing satellites without a usable 3D fix
- rejected GPS/baro measurements
- missing altitude zero
- low GC log storage
- weak LoRa SNR
- possible gyro bias while stationary

These warnings are advisory. They do not currently gate launch events or change
node-side flight logic.

## Service Files

Systemd service snapshots are stored under:

```text
remote/systemd/
```

Current service files include:

- `uhrk-node.service`
- `uhrk-backend.service`
- `uhrk-web.service`
- `lora-pkt-fwd.service`

## Helper Scripts

Helper scripts are stored under:

```text
remote/scripts/
```

Current scripts include:

- `check_berry_gps.sh`
- `check_gc_gps.sh`
- `read_gc_gps_once.py`

These are mainly for bench checks and GPS debugging.

## Known Current Limitations

- Only the Booster node snapshot is currently represented in the repo under
  `remote/uhrkboo/zenith_node/`; Sustainer and Payload will need device IDs and
  deployments adjusted from the same pattern.
- GPS has been unreliable in recent tests and is not trusted for vertical
  estimation.
- GPS altitude is display-only.
- The Kalman filter uses `|a| - g`, not full orientation-corrected vertical
  acceleration.
- Gyro bias calibration is not yet solved.
- Event/chute settings in the dashboard are not yet pushed to node-side event
  logic.
- Clock sync to nodes is still WiFi/HTTP, not LoRa.
- LoRa shutdown is broadcast and has no per-node acknowledgement in telemetry
  yet. The GC can confirm the gateway queued the command; the node logs confirm
  receipt after the fact.
- The system is promising for controlled drone/drop/walking tests, but event
  logic and GPS behavior should still be treated as test-phase rather than
  flight-certified.

## Current Flight-Test Readiness Summary

Strongest current areas:

- LoRa telemetry reception
- GC dashboard visibility
- Local logs on both GC and node
- LoRa pad-state control
- LoRa shutdown control
- Baro-led altitude tracking
- GC-side Kalman velocity/acceleration smoothing

Areas needing more validation before serious rocket flight reliance:

- GPS lock behavior and antenna setup
- Gyro/IMU calibration
- Event detection thresholds
- Recovery-event logic
- Multi-node airtime behavior with all three nodes active
- Per-node command acknowledgement over LoRa
- LoRa-based settings/config sync
