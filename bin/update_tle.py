#!/usr/bin/env python3
# satpi
# Downloads and filters TLE data for the configured satellites.
# Author: Andreas Horvath
# Project: Autonomous, Config-driven satellite reception pipeline for Raspberry Pi

#!/usr/bin/env python3

import os
import subprocess
import tempfile
import logging
from load_config import load_config, ConfigError


logger = logging.getLogger("satpi.update")


def setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "update_tle.log")

    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def run(cmd):
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}")


def is_interface_up(interface):
    result = subprocess.run(
        ["ip", "link", "show", interface],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def download_tle(url, target):
    run(f"curl --retry 3 --retry-delay 2 --retry-all-errors -fsSL '{url}' -o '{target}'")
    if not os.path.exists(target) or os.path.getsize(target) == 0:
        raise RuntimeError("TLE download failed")


def normalize_sat_name(name):
    return " ".join(name.strip().upper().replace("-", " ").replace("_", " ").split())


def filter_tle(input_file, output_file, satellite_names):
    tmp_output = output_file + ".tmp"
    normalized_targets = {normalize_sat_name(s) for s in satellite_names}
    found = []

    with open(input_file, "r") as f:
        lines = f.readlines()

    with open(tmp_output, "w") as out:
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            normalized_line = normalize_sat_name(line)

            if normalized_line in normalized_targets:
                if i + 2 >= len(lines):
                    raise RuntimeError(f"Incomplete TLE entry for satellite: {line}")
                out.write(lines[i])
                out.write(lines[i + 1])
                out.write(lines[i + 2])
                found.append(line)
                i += 3
            else:
                i += 1

    if not os.path.exists(tmp_output) or os.path.getsize(tmp_output) == 0:
        raise RuntimeError(f"No matching satellites found in TLE. Configured: {satellite_names}")

    os.replace(tmp_output, output_file)
    logger.info("Matched satellites in TLE: %s", found)


def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "config", "config.ini")

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"[update] CONFIG ERROR: {e}")
        return

    setup_logging(config["paths"]["log_dir"])

    network = config["network"]
    satellites = [s for s in config["satellites"] if s["enabled"]]

    tle_url = network["tle_url"]
    tle_file = network["tle_file"]

    tle_dir = os.path.dirname(tle_file)
    if tle_dir:
        os.makedirs(tle_dir, exist_ok=True)

    fd, tmp_file = tempfile.mkstemp(prefix="satpi_tle_", suffix=".tmp")
    os.close(fd)

    vpn_started = False

    try:
        if network["use_vpn"]:
            vpn_command = network["vpn_start"]
            vpn_interface = vpn_command.strip().split()[-1]

            if is_interface_up(vpn_interface):
                logger.info("VPN already running")
            else:
                logger.info("Starting VPN...")
                run(vpn_command)
                vpn_started = True

        logger.info("Downloading TLE...")
        download_tle(tle_url, tmp_file)

        sat_names = [s["name"] for s in satellites]
        logger.info("Filtering satellites: %s", sat_names)
        filter_tle(tmp_file, tle_file, sat_names)

        logger.info("TLE update successful: %s", tle_file)

    except Exception as e:
        logger.exception("update_tle failed: %s", e)
        raise

    finally:
        if os.path.exists(tmp_file):
            os.remove(tmp_file)

        if vpn_started:
            logger.info("Stopping VPN...")
            run(network["vpn_stop"])


if __name__ == "__main__":
    main()
