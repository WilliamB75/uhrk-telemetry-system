# UHRK Telemetry System

Ground-station dashboard and LoRa telemetry node software for UHRK rocket flight testing.

## Project Layout

- `remote/uhrkgc/uhrk_site/` - ground-station backend and web dashboard
- `remote/uhrkboo/zenith_node/` - telemetry node code
- `remote/systemd/` - service files used on the Raspberry Pis
- `remote/scripts/` - helper scripts for GPS checks

## Working From Another PC

Clone the repository, then open the cloned folder in Codex:

```powershell
git clone https://github.com/WilliamB75/uhrk-telemetry-system.git
cd uhrk-telemetry-system
```

Pi passwords, private keys, and live flight logs are intentionally not stored in
this repository. Ask the team lead for the relevant device access details when
deployment or Pi inspection is required.

## Current Dashboard

When the ground station is running, the dashboard is served from:

```text
http://10.42.0.1:8000/
```

## Notes

The repository stores deployable source snapshots. Runtime files such as
`telemetry_latest.json`, event settings, altitude zero calibration, and flight
logs are generated on the Pi and ignored by Git.
