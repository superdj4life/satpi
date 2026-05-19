# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

satpi is a headless, config-driven weather satellite reception pipeline for Raspberry Pi. It orchestrates the full cycle: TLE download → pass prediction → systemd timer generation → live SatDump reception → SQLite import → plotting → rclone upload → email/MQTT notification.

This repo is a **personal fork** of [HorvathAndreas/satpi](https://github.com/HorvathAndreas/satpi). Custom additions live on top of upstream commits; upstream changes are merged in periodically.

Remotes:

- `origin` → `superdj4life/satpi` (our fork)
- `upstream` → `HorvathAndreas/satpi` (source project)

## Deployment environment

The code runs on a Raspberry Pi 4. Development happens in VSCode; changes are deployed to the Pi manually via SSH/SCP or by running `bin/update_satpi.py` on the Pi, which does a `git pull` while stashing and restoring `config/config.ini`.

## Running the pipeline manually

```bash
python3 bin/update_tle.py                           # refresh TLE data
python3 bin/predict_passes.py                       # compute upcoming passes
python3 bin/schedule_passes.py                      # generate per-pass systemd units
python3 bin/generate_refresh_units.py               # create/reload the refresh timer
python3 bin/post_processor.py --copy --notify --db --plots <pass_dir>
python3 bin/import_to_db.py --all                   # import all reception.json files to SQLite
python3 bin/optimize_reception.py --config config/config.ini
python3 bin/optimize_reception_ai.py --config config/config.ini
python3 bin/homeassistant_notification.py register  # push HA MQTT discovery config
python3 bin/homeassistant_notification.py scheduled # publish upcoming pass schedule to HA
```

Validate the config without running anything:

```bash
python3 lib/read_config.py --verbose
python3 lib/read_config.py --section hardware
python3 lib/read_config.py --section ha_mqtt --key host
```

Check active timers on the Pi:

```bash
systemctl list-timers --all | grep satpi
```

Update satpi from GitHub (run on the Pi):

```bash
python3 bin/update_satpi.py
```

## Configuration

`config/config.ini` is the single runtime config file. It is **not committed** (local only). `config/config.example.ini` is the committed template — keep it in sync when adding new config keys.

All project-relative paths in `[paths]` resolve against `base_dir` (typically `/home/pi/satpi`). Binary paths (`satdump_bin`, `mail_bin`, `python_bin`) are absolute.

Key sections and what controls them:

| Section | Controls |
|---|---|
| `[satellite.<name>]` | Per-satellite enable, frequency, bandwidth, SatDump pipeline, min elevation |
| `[scheduling]` | Refresh cadence, pre/post pass buffers |
| `[hardware]` | RTL-SDR device ID, gain, sample rate, bias-T |
| `[processing_thresholds]` | Score/channel thresholds for LRPT/HRPT pass quality scoring |
| `[ha_mqtt]` | Home Assistant MQTT discovery — broker, topics, device identity |
| `[noise_floor]` | Optional background spectrum measurement |
| `[copytarget]` | rclone remote upload |
| `[notify]` | msmtp email notifications |

N2YO API key can also be set via the `SATPI_N2YO_API_KEY` environment variable instead of the config file.

## Code architecture

### Config loading (`lib/read_config.py`)

Central entry point for all scripts. `read_config(path)` returns a typed `dict` with all sections parsed and validated. It raises `ConfigError` on missing required keys, invalid values, or unknown keys (to surface config drift).

`KNOWN_KEYS` and `SATELLITE_KEYS` define the accepted key set per section — add new config keys there when extending the schema.

Frequency values throughout the config accept human units (`137.9 MHz`, `1 MHz`, `10 kHz`) via `lib/parse_frequency.py`.

### Pipeline execution flow

```
update_tle.py
    → downloads TLE, filters to configured NORAD IDs → results/tle/weather.tle

predict_passes.py
    → Skyfield pass prediction → results/passes/passes.json

schedule_passes.py
    → reads passes.json
    → writes systemd/<generated>/<pass-id>.service + .timer + .pass.json sidecar
    → enables/disables units via systemctl

[systemd timer fires at AOS time]
    → receive_pass.py --pass-file <sidecar.pass.json>
        → starts SatDump subprocess
        → streams stdout: parses SNR/BER/sync lines into reception.json (written every 10s)
        → stops SatDump at LOS
        → calls post_processor.py: copy → notify → db import → plots
        → calls homeassistant_notification.py pass_start / pass_done

generate_refresh_units.py
    → installs satpi-refresh.service + satpi-refresh.timer
    → timer runs: update_tle → predict_passes → schedule_passes
```

### Per-pass data model

Each pass creates `results/passes/<pass-id>/reception.json` containing:

- Pass identifiers, timing, RF settings, reception setup metadata
- Time-stamped SNR / BER / sync-state samples
- Azimuth and elevation trajectory samples

This file is the primary input to `import_to_db.py`. Plots are generated from SQLite, not directly from JSON.

### Home Assistant integration (`bin/homeassistant_notification.py`)

Uses MQTT auto-discovery. Subcommands: `register`, `scheduled`, `pass_start`, `pass_done`, `status`. The script has its own lightweight config loader (reads only `[ha_mqtt]`, `[station]`, `[paths]`) rather than using the full `read_config()`, intentionally to avoid pulling in unnecessary dependencies.

### Fork-specific additions

Custom work on top of upstream:

- `lib/read_config.py` — replaces upstream `bin/load_config.py`; all scripts import from here
- `bin/homeassistant_notification.py` — MQTT discovery integration with HA
- `bin/update_satpi.py` — self-update script (git pull with stash/unstash around config.ini)
- `[ha_mqtt]` and `[processing_thresholds]` config sections

When merging upstream, watch for conflicts in `config/config.example.ini`, `bin/schedule_passes.py`, and `lib/read_config.py` — these files diverge most frequently.
