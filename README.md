# satpi

Autonomous, config-driven satellite reception pipeline for Raspberry Pi.

`satpi` is a headless workflow for automated weather satellite reception. It downloads and filters TLE data, predicts passes, generates per-pass systemd timers, runs SatDump for live reception, decodes CADU data, uploads successful results, and can send notification emails.

## Features

- Headless, autonomous workflow
- Config-driven setup
- Per-satellite configuration
- Skyfield-based pass prediction
- systemd-based scheduling
- SatDump live reception and decode
- Optional upload via `rclone`
- Optional notification via `msmtp`

## Quick start

Clone the repository:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/HorvathAndreas/satpi.git
cd satpi
```

Run the base installation script:

```bash
bash scripts/install_base.sh
```

Create and edit the active configuration:

```bash
cp config/config.example.ini config/config.ini
nano config/config.ini
```

Run the workflow manually once:

```bash
python3 bin/update_tle.py
python3 bin/predict_passes.py
python3 bin/schedule_passes.py
python3 bin/generate_refresh_units.py
```

Check active timers:

```bash
systemctl list-timers --all | grep satpi
```

## Requirements

### Hardware

- Raspberry Pi 4 or Raspberry Pi 5
- RTL-SDR compatible receiver
- Suitable antenna and RF setup for weather satellite reception

### Software

- Raspberry Pi OS Lite 64-bit
- Python 3
- systemd
- SatDump
- `python3-skyfield`
- `python3-numpy`
- `rclone`
- `msmtp`

## Workflow

1. **`update_tle.py`**  
   Downloads fresh TLE data and filters it to the configured satellites.

2. **`predict_passes.py`**  
   Calculates upcoming passes for the configured ground station.

3. **`schedule_passes.py`**  
   Generates per-pass systemd service and timer units for all relevant future passes.

4. **`receive_pass.py`**  
   Executes one scheduled pass, starts SatDump, records data, decodes results, uploads output, and optionally sends a notification email.

5. **`generate_refresh_units.py`**  
   Creates the higher-level refresh service and timer that periodically updates the overall planning state of the system.

## Project structure

```text
satpi/
├── bin/
│   ├── load_config.py
│   ├── update_tle.py
│   ├── predict_passes.py
│   ├── schedule_passes.py
│   ├── receive_pass.py
│   └── generate_refresh_units.py
├── config/
│   ├── config.ini
│   └── config.example.ini
├── docs/
│   ├── INSTALL_FOR_BEGINNERS.md
│   └── images/
├── logs/
├── results/
│   ├── captures/
│   └── passes/
├── scripts/
│   └── install_base.sh
├── systemd/
│   ├── satpi-refresh.service
│   ├── satpi-refresh.timer
│   └── generated/
├── tle/
│   └── weather.tle
└── README.md
```

## File overview

### `bin/load_config.py`
Loads, parses, and validates the central `config.ini` file. It converts configuration values into typed Python data structures and performs consistency checks before the operational scripts start.

### `bin/update_tle.py`
Downloads current TLE data from the configured source and filters it so that only the satellites used by this installation remain in the local TLE file.

### `bin/predict_passes.py`
Calculates upcoming satellite passes for the configured ground station based on the filtered local TLE file.

### `bin/schedule_passes.py`
Reads the predicted pass data and generates one systemd service and one systemd timer for every future pass that should still be received.

### `bin/receive_pass.py`
Executes one scheduled pass from start to finish. It prepares the pass-specific output directory, starts SatDump, monitors the recording, triggers decoding, copies the results, and optionally sends a notification email.

### `bin/generate_refresh_units.py`
Creates and enables the refresh service and timer that periodically run the higher-level planning chain.

### `config/config.example.ini`
Public example configuration file for new installations.

### `config/config.ini`
Active local configuration file used by the satpi scripts on a running system.

### `scripts/install_base.sh`
Interactive base installation script for Raspberry Pi OS. It installs the required packages, applies base operating system settings, prepares the directory structure, and builds the required SatDump binary.

### `systemd/satpi-refresh.service`
Systemd service that executes the periodic satpi refresh workflow.

### `systemd/satpi-refresh.timer`
Systemd timer that periodically triggers `satpi-refresh.service`.

### `systemd/generated/`
Contains the generated per-pass systemd service and timer files created by `schedule_passes.py`.

### `tle/weather.tle`
Filtered local TLE file used as the orbital data source for pass prediction.

### `results/captures/`
Stores pass-specific reception output such as SatDump logs, raw data, decoded files, upload logs, and related artifacts.

### `results/passes/`
Stores generated pass prediction and planning-related output.

### `logs/`
Stores runtime log files written by the satpi scripts.

## Configuration

Create the active configuration file:

```bash
cp config/config.example.ini config/config.ini
nano config/config.ini
```

At minimum, review and adapt:

- station name and timezone
- QTH coordinates
- hardware settings
- satellite definitions
- path settings
- SatDump path
- upload target
- notification settings
- systemd service user

## Installation notes

The base installation script prepares the operating system and builds SatDump:

```bash
bash scripts/install_base.sh
```

After that, two manual integrations are typically still required.

### Configure rclone

```bash
rclone config
```

### Configure msmtp

Create the mail configuration file:

```bash
nano ~/.msmtprc
chmod 600 ~/.msmtprc
```

Example structure:

```ini
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
```

Test mail delivery:

```bash
printf "Subject: satpi test\n\nTest mail.\n" | /usr/bin/msmtp YOUR_GMAIL_ADDRESS
```

## systemd integration

Generate the refresh units:

```bash
python3 bin/generate_refresh_units.py
```

This creates and links:

- `satpi-refresh.service`
- `satpi-refresh.timer`

The refresh timer runs the planning chain:

1. update TLE
2. predict passes
3. schedule pass timers

Check the result:

```bash
systemctl list-timers --all | grep satpi
```

## Typical usage

Update TLE data manually:

```bash
python3 bin/update_tle.py
```

Predict passes manually:

```bash
python3 bin/predict_passes.py
```

Generate per-pass timers manually:

```bash
python3 bin/schedule_passes.py
```

Generate refresh units manually:

```bash
python3 bin/generate_refresh_units.py
```

## Output

### Pass results

Pass-specific reception results are written to:

```text
results/captures/
```

A pass directory may contain:

- SatDump runtime log
- raw intermediate files
- `.soft`
- `.cadu`
- decoded image products
- `MSU-MR/`
- `decode.log`
- `upload.log`

### Planning results

Prediction and planning artifacts are written to:

```text
results/passes/
```

### Runtime logs

General script logs are written to:

```text
logs/
```

## Upload and notifications

If enabled in `config.ini`, satpi can:

- upload results via `rclone`
- create a share link
- send a notification email via `msmtp`

## Documentation

Beginner-oriented setup notes are available here:

```text
docs/INSTALL_FOR_BEGINNERS.md
```

## Author

Andreas Horvath  
info[at]andreas-horvath.ch  
WhatsApp: +41 79 249 57 12

## Project

Autonomous, Config-driven satellite reception pipeline for Raspberry Pi
