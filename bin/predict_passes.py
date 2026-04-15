#!/usr/bin/env python3
# satpi
# Predicts upcoming satellite passes for the configured ground station.
# This script uses the local filtered TLE file together with the configured
# station position, elevation limits and scheduling window to calculate which
# future passes are relevant for reception. The resulting pass data is written
# in a structured form so that later steps can generate concrete jobs from it.
# Author: Andreas Horvath
# Project: Autonomous, Config-driven satellite reception pipeline for Raspberry Pi

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from load_config import load_config, ConfigError
from skyfield.api import load, wgs84


logger = logging.getLogger("satpi.predict")


def setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "predict_passes.log")

    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def normalize_sat_name(name):
    return " ".join(name.strip().upper().replace("-", " ").replace("_", " ").split())


def isoformat_utc(dt):
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_satellite_map(sat_objects):
    return {normalize_sat_name(sat.name): sat for sat in sat_objects}


def load_satellites_from_tle(tle_file):
    return load.tle_file(tle_file)

def azimuth_to_cardinal(az_deg):
    az = az_deg % 360.0

    if az >= 337.5 or az < 22.5:
        return "north"
    if az < 67.5:
        return "northeast"
    if az < 112.5:
        return "east"
    if az < 157.5:
        return "southeast"
    if az < 202.5:
        return "south"
    if az < 247.5:
        return "southwest"
    if az < 292.5:
        return "west"
    return "northwest"


def derive_pass_direction(aos_azimuth_deg, los_azimuth_deg):
    start_dir = azimuth_to_cardinal(aos_azimuth_deg)
    end_dir = azimuth_to_cardinal(los_azimuth_deg)

    allowed_directions = {
        ("north", "south"): "north_to_south",
        ("south", "north"): "south_to_north",
        ("west", "east"): "west_to_east",
        ("east", "west"): "east_to_west",
        ("southwest", "northeast"): "southwest_to_northeast",
        ("southeast", "northwest"): "southeast_to_northwest",
        ("northwest", "southeast"): "northwest_to_southeast",
        ("northeast", "southwest"): "northeast_to_southwest",
    }

    if (start_dir, end_dir) in allowed_directions:
        return allowed_directions[(start_dir, end_dir)]

    start_x = 1 if "east" in start_dir else -1 if "west" in start_dir else 0
    start_y = 1 if "north" in start_dir else -1 if "south" in start_dir else 0
    end_x = 1 if "east" in end_dir else -1 if "west" in end_dir else 0
    end_y = 1 if "north" in end_dir else -1 if "south" in end_dir else 0

    dx = end_x - start_x
    dy = end_y - start_y

    if abs(dx) > abs(dy):
        return "west_to_east" if dx > 0 else "east_to_west"
    if abs(dy) > abs(dx):
        return "south_to_north" if dy > 0 else "north_to_south"

    if dx > 0 and dy > 0:
        return "southwest_to_northeast"
    if dx < 0 and dy > 0:
        return "southeast_to_northwest"
    if dx > 0 and dy < 0:
        return "northwest_to_southeast"
    if dx < 0 and dy < 0:
        return "northeast_to_southwest"

    if dy > 0:
        return "south_to_north"
    if dy < 0:
        return "north_to_south"
    if dx > 0:
        return "west_to_east"
    return "east_to_west"

def compute_passes_for_satellite(ts, observer, sat_obj, sat_cfg, start_dt, end_dt):
    t0 = ts.from_datetime(start_dt)
    t1 = ts.from_datetime(end_dt)

    # Use true AOS/LOS at horizon for scheduling.
    # Keep min_elevation only as a quality filter via max_elevation check below.
    t_events, events = sat_obj.find_events(
        observer,
        t0,
        t1,
        altitude_degrees=0.0,
    )

    passes = []
    current_pass = None

    for t, event in zip(t_events, events):
        dt = t.utc_datetime().replace(tzinfo=timezone.utc)

        difference = sat_obj - observer
        topocentric = difference.at(t)
        alt, az, distance = topocentric.altaz()

        if event == 0:
            current_pass = {
                "satellite": sat_cfg["name"],
                "start": dt,
                "end": None,
                "max_elevation": None,
                "max_elevation_time": None,
                "aos_azimuth_deg": round(az.degrees, 2),
                "los_azimuth_deg": None,
                "direction": None,
                "frequency_hz": sat_cfg["frequency"],
                "bandwidth_hz": sat_cfg["bandwidth"],
                "pipeline": sat_cfg["pipeline"],
            }

        elif event == 1:
            if current_pass is not None:
                current_pass["max_elevation"] = round(alt.degrees, 2)
                current_pass["max_elevation_time"] = dt

        elif event == 2:
            if current_pass is not None:
                current_pass["end"] = dt
                current_pass["los_azimuth_deg"] = round(az.degrees, 2)
                current_pass["direction"] = derive_pass_direction(
                    current_pass["aos_azimuth_deg"],
                    current_pass["los_azimuth_deg"],
                )

                if (
                    current_pass["start"] is not None
                    and current_pass["end"] is not None
                    and current_pass["max_elevation"] is not None
                    and current_pass["max_elevation"] >= sat_cfg["min_elevation"]
                ):
                    passes.append({
                        "satellite": current_pass["satellite"],
                        "start": isoformat_utc(current_pass["start"]),
                        "end": isoformat_utc(current_pass["end"]),
                        "max_elevation": current_pass["max_elevation"],
                        "max_elevation_time": isoformat_utc(current_pass["max_elevation_time"]),
                        "aos_azimuth_deg": current_pass["aos_azimuth_deg"],
                        "los_azimuth_deg": current_pass["los_azimuth_deg"],
                        "direction": current_pass["direction"],
                        "frequency_hz": current_pass["frequency_hz"],
                        "bandwidth_hz": current_pass["bandwidth_hz"],
                        "pipeline": current_pass["pipeline"],
                    })

                current_pass = None

    return passes

def write_passes_json(pass_file, passes):
    os.makedirs(os.path.dirname(pass_file), exist_ok=True)
    tmp_file = pass_file + ".tmp"

    payload = {
        "generated_at": isoformat_utc(datetime.now(timezone.utc)),
        "pass_count": len(passes),
        "passes": sorted(passes, key=lambda p: p["start"]),
    }

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    os.replace(tmp_file, pass_file)


def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "config", "config.ini")

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"[predict] CONFIG ERROR: {e}")
        return

    setup_logging(config["paths"]["log_dir"])

    qth = config["qth"]
    scheduling = config["scheduling"]
    paths = config["paths"]
    satellites = [s for s in config["satellites"] if s["enabled"]]

    tle_file = paths["tle_file"]
    pass_file = paths["pass_file"]

    if not os.path.exists(tle_file):
        raise FileNotFoundError(f"TLE file not found: {tle_file}")

    logger.info("Loading TLE file: %s", tle_file)

    ts = load.timescale()
    sat_objects = load_satellites_from_tle(tle_file)
    sat_map = build_satellite_map(sat_objects)

    observer = wgs84.latlon(
        qth["latitude"],
        qth["longitude"],
        elevation_m=qth["altitude"],
    )

    start_dt = datetime.now(timezone.utc)
    end_dt = start_dt + timedelta(hours=scheduling["max_pass_age_hours"])

    logger.info(
        "Computing passes from %s until %s",
        isoformat_utc(start_dt),
        isoformat_utc(end_dt),
    )

    all_passes = []

    for sat_cfg in satellites:
        sat_key = normalize_sat_name(sat_cfg["name"])
        sat_obj = sat_map.get(sat_key)

        if sat_obj is None:
            logger.warning("Satellite not found in TLE: %s", sat_cfg["name"])
            continue

        logger.info(
            "Computing passes for %s (min_elevation=%s)",
            sat_cfg["name"],
            sat_cfg["min_elevation"],
        )

        sat_passes = compute_passes_for_satellite(
            ts,
            observer,
            sat_obj,
            sat_cfg,
            start_dt,
            end_dt,
        )

        logger.info("Found %d passes for %s", len(sat_passes), sat_cfg["name"])
        all_passes.extend(sat_passes)

    write_passes_json(pass_file, all_passes)
    logger.info("Wrote %d total passes to %s", len(all_passes), pass_file)


if __name__ == "__main__":
    main()
