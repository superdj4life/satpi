# satpi

Autonomous, config-driven weather satellite reception pipeline for Raspberry Pi.

satpi is a headless workflow for automated weather satellite reception. It downloads and filters TLE data, predicts passes, generates per-pass systemd timers, runs SatDump for live reception, stores structured reception metadata, renders reception plots, imports pass metrics into SQLite, uploads successful results, and can send notification emails.

Features
 • headless, autonomous workflow
 • config-driven setup
 • per-satellite configuration
 • Skyfield-based pass prediction
 • systemd-based scheduling
 • SatDump live reception and decode
 • structured reception.json output per pass
 • automatic skyplot and timechart rendering
 • SQLite-based reception database
 • reception analysis and optimization tools
 • optional upload via rclone
 • optional notification via msmtp

Quick start

Clone the repository:

sudo apt update
sudo apt install -y git
git clone <https://github.com/HorvathAndreas/satpi.git>
cd satpi

Run the base installation script:

bash scripts/install_base.sh

Copy the example configuration and create the active local config:

cp config/config.example.ini config/config.ini
nano config/config.ini

Run the workflow manually once:

python3 bin/update_tle.py
python3 bin/predict_passes.py
python3 bin/schedule_passes.py
python3 bin/generate_refresh_units.py

Check active timers:

systemctl list-timers --all | grep satpi

Requirements

Hardware
 • Raspberry Pi 4 or Raspberry Pi 5
 • RTL-SDR compatible receiver
 • suitable antenna and RF setup for weather satellite reception

Software
 • Raspberry Pi OS Lite 64-bit
 • Python 3
 • systemd
 • SatDump
 • python3-skyfield
 • python3-numpy
 • python3-matplotlib
 • sqlite3
 • rclone
 • msmtp

Workflow

 1. update_tle.py
Downloads fresh TLE data and filters it to the configured satellites.
 2. predict_passes.py
Calculates upcoming passes for the configured ground station.
 3. schedule_passes.py
Generates per-pass systemd service and timer units for all relevant future passes.
 4. receive_pass.py
Executes one scheduled pass, starts SatDump, records structured reception data, imports metrics into SQLite, renders plots, decodes results, uploads output, and optionally sends a notification email.
 5. generate_refresh_units.py
Creates the higher-level refresh service and timer that periodically updates the overall planning state of the system.

Project structure

satpi/
├── bin/
│   ├── export_reception_report_pdf.py
│   ├── generate_refresh_units.py
│   ├── import_reception_to_db.py
│   ├── init_reception_db.py
│   ├── load_config.py
│   ├── optimize_reception.py
│   ├── optimize_reception_ai.py
│   ├── plot_receptions.py
│   ├── predict_passes.py
│   ├── query_reception_db.py
│   ├── receive_pass.py
│   ├── schedule_passes.py
│   └── update_tle.py
├── config/
│   ├── config.ini
│   └── config.example.ini
├── docs/
│   ├── INSTALL_FOR_BEGINNERS.md
│   └── images/
├── logs/
├── results/
│   ├── captures/
│   ├── database/
│   ├── optimization/
│   ├── passes/
│   ├── reports/
│   └── tle/
├── scripts/
│   └── install_base.sh
├── systemd/
│   ├── satpi-refresh.service
│   ├── satpi-refresh.timer
│   └── generated/
└── README.md

File overview

### `bin/load_config.py`

Loads, parses, and validates the central `config.ini` file. It resolves relative project paths against `paths.base_dir` and converts configuration values into typed Python data structures.

### `bin/update_tle.py`

Downloads current TLE data from the configured source and filters it so that only the satellites used by this installation remain in the local TLE file.

### `bin/predict_passes.py`

Calculates upcoming satellite passes for the configured ground station based on the filtered local TLE file.

### `bin/schedule_passes.py`

Reads the predicted pass data and generates one systemd service and one systemd timer for every future pass that should still be received.

### `bin/receive_pass.py`

Executes one scheduled pass from start to finish. It prepares the pass-specific output directory, starts SatDump, records structured reception data into `reception.json`, imports metrics into SQLite, renders plots via `plot_receptions.py`, triggers decode, copies the results, and optionally sends a notification email.

### `bin/plot_receptions.py`

Creates plots directly from the SQLite reception database.

- With `--pass-id`, it generates a single-pass skyplot and timechart
- Without `--pass-id`, it generates a combined skyplot across all passes matching the selected filters
- Filtering supports satellites and the reception setup fields stored in the database

### `bin/init_reception_db.py`

Initializes the SQLite database schema used for reception history and analysis.

### `bin/import_reception_to_db.py`

Imports pass-level metrics and setup information from `reception.json` into the SQLite reception database.

### `bin/query_reception_db.py`

Queries the SQLite reception database for analysis and reporting.

### `bin/optimize_reception.py`

Analyzes recorded reception data from SQLite and compares geometrically similar passes to evaluate reception performance.

### `bin/optimize_reception_ai.py`

Builds on the optimizer output and produces an AI-assisted interpretation of reception quality trends using either OpenAI or an Ollama server.

### `bin/export_reception_report_pdf.py`

Exports reception analysis results into PDF format.

### `bin/generate_refresh_units.py`

Creates and enables the refresh service and timer that periodically run the higher-level planning chain.

### `config/config.example.ini`

Public example configuration file for new installations. Copy this file to `config/config.ini` before first use.

### `config/config.ini`

Active local configuration file used by the satpi scripts on a running system. This file is intentionally local and should not be committed.

### `scripts/install_base.sh`

Interactive base installation script for Raspberry Pi OS. It installs required packages, applies base operating system settings, prepares the directory structure, and builds the required SatDump binary.

### `systemd/satpi-refresh.service`

Systemd service that executes the periodic satpi refresh workflow.

### `systemd/satpi-refresh.timer`

Systemd timer that periodically triggers `satpi-refresh.service`.

### `systemd/generated/`

Contains the generated per-pass systemd service and timer files created by `schedule_passes.py`.

Configuration

Create the active configuration file from the example:

cp config/config.example.ini config/config.ini
nano config/config.ini

Minimum required adjustments

Before running satpi, at minimum you should review and adapt these settings in `config/config.ini`:

- **`[station]`**
  - `name`
  - `timezone`

- **`[qth]`**
  - `latitude`
  - `longitude`
  - `altitude_m`

- **`[paths]`**
  - `base_dir` if your installation path differs
  - `satdump_bin` if SatDump is installed elsewhere
  - `mail_bin` if `msmtp` is installed elsewhere
  - `python_bin` if your Python path differs

- **`[hardware]`**
  - `source_id`
  - `gain`
  - `sample_rate`
  - `bias_t`

- **`[satellite.*]`**
  - `enabled`
  - `min_elevation_deg`
  - `frequency_hz`
  - `bandwidth_hz`
  - `pipeline`

- **`[copytarget]`**
  - `enabled`
  - `rclone_remote`
  - `rclone_path`

- **`[notify]`**
  - `enabled`
  - `mail_to`
  - `mail_subject_prefix`

- **`[systemd]`**
  - `service_user`

- **`[reception_setup]`**
  - antenna, SDR, feedline, host, and power-supply description fields should match your real setup

- **`[optimize_reception_ai]`**
  - `enabled`
  - `provider` (`openai` or `ollama`)
  - `model`
  - `base_url` for remote Ollama or custom API endpoints
  - `api_key` for OpenAI, or optional auth in front of Ollama

If you only want the basic reception pipeline first, the most important items are:
 • station and QTH
 • paths
 • hardware
 • satellite definitions
 • systemd service user

Path handling

All local project paths are configured in the [paths] section.

base_dir is the project root. Most project-specific paths in config.ini are stored relative to base_dir, for example:
 • results/passes/passes.json
 • results/captures
 • results/tle/weather.tle
 • results/database/reception.db

System binaries remain absolute paths:
 • satdump_bin
 • mail_bin
 • python_bin

Installation notes

The base installation script prepares the operating system and builds SatDump:

bash scripts/install_base.sh

After that, two manual integrations are typically still required.

Configure rclone

rclone config

Configure msmtp

Create the mail configuration file:

nano ~/.msmtprc
chmod 600 ~/.msmtprc

Example structure:

defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        ~/.msmtp.log

account gmail
host smtp.gmail.com
port 587
from YOUR_GMAIL_ADDRESS
user YOUR_GMAIL_ADDRESS
password YOUR_APP_PASSWORD

account default : gmail

Test mail delivery:

printf "Subject: satpi test\n\nTest mail.\n" | /usr/bin/msmtp YOUR_GMAIL_ADDRESS

When the blue Configuring msmtp dialog appears during installation, choose No for AppArmor support.

systemd integration

Generate the refresh units:

python3 bin/generate_refresh_units.py

This creates and links:
 • satpi-refresh.service
 • satpi-refresh.timer

The refresh timer runs the planning chain:

 1. update TLE
 2. predict passes
 3. schedule pass timers

Check the result:

systemctl list-timers --all | grep satpi

Typical usage

Update TLE data manually:

python3 bin/update_tle.py

Predict passes manually:

python3 bin/predict_passes.py

Generate per-pass timers manually:

python3 bin/schedule_passes.py

Generate refresh units manually:

python3 bin/generate_refresh_units.py

Import all reception JSON files into SQLite:

python3 bin/import_reception_to_db.py --all

Create a combined skyplot from all recorded passes:

python3 bin/plot_receptions.py

Create a combined skyplot for a specific satellite:

python3 bin/plot_receptions.py --satellite "METEOR-M2 4"

Create plots for one specific pass:

python3 bin/plot_receptions.py --pass-id "2026-04-10_16-07-30_METEOR-M2_4"

Run reception optimizer manually:

python3 bin/optimize_reception.py --config config/config.ini

Run AI-based optimizer analysis manually:

python3 bin/optimize_reception_ai.py --config config/config.ini

Example for a remote Ollama host in `config.ini`:

```ini
[optimize_reception_ai]
enabled = true
provider = ollama
model = llama3.1:8b
base_url = http://YOUR-OLLAMA-SERVER:11434
request_timeout_seconds = 120
api_key =
```

## Output

Pass results

Pass-specific reception results are written to:

results/captures/

Each pass gets its own directory, for example:

results/captures/YYYY-MM-DD_HH-MM-SS_SATAME_01/

A pass directory may contain:
 • reception.json
 • skyplot_<pass_id>.png
 • timeseries_<pass_id>.png
 • .cadu
 • decoded image products
 • MSU-MR/
 • dataset.json
 • telemetry.json

Planning results

Prediction and planning artifacts are written to:

results/passes/

This directory is intended only for planning-related files such as:
 • passes.json

Database

Reception history and derived pass metrics are stored in:

results/database/reception.db

Reports and combined plots

Combined plots and reports are written to:

results/reports/

Typical examples:
 • skyplot_METEOR-M2_4.png
 • skyplot_METEOR-M2_4_and_others.png
 • skyplot_filtered.png

Optimization output

Optimizer reports are written to:

results/optimization/

Runtime logs

General script logs are written to:

logs/

Structured reception data

For every recorded pass, receive_pass.py writes a reception.json file into the pass directory under results/captures/.

This file contains:
 • pass identifiers and timing
 • RF settings
 • reception setup metadata
 • time-stamped SNR / BER / sync-state samples
 • azimuth and elevation samples

This JSON file is the basis for:
 • database import
 • traceable per-pass metadata storage
 • later re-import if needed

Plots themselves are generated from the SQLite database, not directly from the JSON files.

Plotting logic

plot_receptions.py uses the SQLite database as the source of truth.

Single-pass mode

If --pass-id is given, the script creates:
 • one skyplot
 • one timechart

for exactly that pass.

Combined mode

If --pass-id is not given, the script creates one combined skyplot across all passes matching the selected filters.

Filtering supports:
 • --satellite
 • reception setup fields such as:
 • --antenna-type
 • --antenna-location
 • --antenna-orientation
 • --lna
 • --rf-filter
 • --feedline
 • --sdr
 • --raspberry-pi
 • --power-supply
 • --additional-info

Repeated use of the same filter option works as OR within that parameter. Different parameters are combined as AND.

Upload and notifications

If enabled in config.ini, satpi can:
 • upload results via rclone
 • create a share link
 • send a notification email via msmtp

Documentation

Beginner-oriented setup notes are available here:

docs/INSTALL_FOR_BEGINNERS.md

Version

Current documented release: v1.3.0

Highlights of v1.3.0:
 • unified plotting in plot_receptions.py
 • single-pass plots now read from SQLite instead of directly from JSON
 • combined plots and single-pass plots use the same database-backed workflow
 • reception setup data now includes sdr in the database schema and import path

Author

Andreas Horvath
info[at]andreas-horvath.ch
WhatsApp: +41 79 249 57 12

Project

Autonomous, config-driven weather satellite reception pipeline for Raspberry Pi
