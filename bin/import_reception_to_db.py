#!/usr/bin/env python3
# satpi
# Import one or more reception JSON files into the SQLite reception database.

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from load_config import load_config, ConfigError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def parse_args():
    parser = argparse.ArgumentParser(description="Import reception JSON data into reception.db")
    parser.add_argument(
        "input",
        nargs="?",
        help="Path to one *-reception.json file. If omitted with --all, imports all files.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Import all *-reception.json files from results/passes",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.ini (default: ../config/config.ini relative to this script)",
    )
    return parser.parse_args()


def get_config_path(cli_value: str | None) -> str:
    if cli_value:
        return os.path.abspath(cli_value)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "config", "config.ini")


def open_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_setup_payload(data: dict[str, Any]) -> dict[str, str]:
    s = data.get("reception_setup", {})
    return {
        "antenna_type": str(s.get("antenna_type", "")),
        "antenna_location": str(s.get("antenna_location", "")),
        "antenna_orientation": str(s.get("antenna_orientation", "")),
        "lna": str(s.get("lna", "")),
        "rf_filter": str(s.get("rf_filter", "")),
        "feedline": str(s.get("feedline", "")),
        "raspberry_pi": str(s.get("raspberry_pi", "")),
        "power_supply": str(s.get("power_supply", "")),
        "additional_info": str(s.get("additional_info", "")),
    }


def build_setup_key(setup: dict[str, str]) -> str:
    canonical = json.dumps(setup, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_or_create_setup_id(conn: sqlite3.Connection, data: dict[str, Any]) -> int:
    setup = build_setup_payload(data)
    setup_key = build_setup_key(setup)

    row = conn.execute(
        "SELECT setup_id FROM setup WHERE setup_key = ?",
        (setup_key,),
    ).fetchone()
    if row:
        return int(row[0])

    conn.execute(
        """
        INSERT INTO setup (
            setup_key, antenna_type, antenna_location, antenna_orientation,
            lna, rf_filter, feedline, raspberry_pi, power_supply, additional_info
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            setup_key,
            setup["antenna_type"],
            setup["antenna_location"],
            setup["antenna_orientation"],
            setup["lna"],
            setup["rf_filter"],
            setup["feedline"],
            setup["raspberry_pi"],
            setup["power_supply"],
            setup["additional_info"],
        ),
    )

    row = conn.execute(
        "SELECT setup_id FROM setup WHERE setup_key = ?",
        (setup_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Failed to create or retrieve setup_id")
    return int(row[0])


def derive_sync_state(viterbi_state: str, deframer_state: str) -> str:
    if deframer_state == "SYNCED":
        return "SYNCED"
    if viterbi_state == "SYNCED":
        return "SYNCING"
    return "NOSYNC"


def compute_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "visible_sample_count": 0,
            "start_azimuth_deg": None,
            "mid_azimuth_deg": None,
            "end_azimuth_deg": None,
            "max_elevation_deg": None,
            "direction": "unknown",
            "first_deframer_sync_delay_seconds": None,
            "total_deframer_synced_seconds": 0.0,
            "sync_drop_count": 0,
            "median_snr_synced": None,
            "median_ber_synced": None,
            "peak_snr_db": None,
        }

    samples_sorted = sorted(samples, key=lambda s: parse_ts(str(s["timestamp"])))
    visible = [s for s in samples_sorted if float(s["elevation_deg"]) >= 0.0]

    sample_count = len(samples_sorted)
    visible_sample_count = len(visible)
    start_azimuth_deg = float(visible[0]["azimuth_deg"]) if visible else None
    mid_azimuth_deg = float(visible[len(visible) // 2]["azimuth_deg"]) if visible else None
    end_azimuth_deg = float(visible[-1]["azimuth_deg"]) if visible else None
    max_elevation_deg = max(float(s["elevation_deg"]) for s in samples_sorted)

    if len(visible) >= 2:
        direction = (
            "increasing_azimuth"
            if float(visible[-1]["azimuth_deg"]) >= float(visible[0]["azimuth_deg"])
            else "decreasing_azimuth"
        )
    else:
        direction = "unknown"

    first_ts = parse_ts(str(samples_sorted[0]["timestamp"]))
    first_sync_delay = None
    total_deframer_synced_seconds = 0.0
    sync_drop_count = 0
    synced_snrs: list[float] = []
    synced_bers: list[float] = []
    peak_snr_db = None

    prev_ts = None
    prev_sync = False

    for s in samples_sorted:
        ts = parse_ts(str(s["timestamp"]))
        snr = float(s["snr_db"])
        peak_snr = float(s["peak_snr_db"])
        ber = float(s["ber"])
        state = derive_sync_state(
            str(s.get("viterbi_state", "NOSYNC")),
            str(s.get("deframer_state", "NOSYNC")),
        )
        is_synced = state == "SYNCED"

        peak_snr_db = peak_snr if peak_snr_db is None else max(peak_snr_db, peak_snr)

        if is_synced and first_sync_delay is None:
            first_sync_delay = (ts - first_ts).total_seconds()

        if is_synced:
            synced_snrs.append(snr)
            synced_bers.append(ber)

        if prev_ts is not None and prev_sync:
            dt = (ts - prev_ts).total_seconds()
            if dt > 0:
                total_deframer_synced_seconds += dt

        if prev_sync and not is_synced:
            sync_drop_count += 1

        prev_ts = ts
        prev_sync = is_synced

    return {
        "sample_count": sample_count,
        "visible_sample_count": visible_sample_count,
        "start_azimuth_deg": start_azimuth_deg,
        "mid_azimuth_deg": mid_azimuth_deg,
        "end_azimuth_deg": end_azimuth_deg,
        "max_elevation_deg": max_elevation_deg,
        "direction": direction,
        "first_deframer_sync_delay_seconds": first_sync_delay,
        "total_deframer_synced_seconds": total_deframer_synced_seconds,
        "sync_drop_count": sync_drop_count,
        "median_snr_synced": median(synced_snrs) if synced_snrs else None,
        "median_ber_synced": median(synced_bers) if synced_bers else None,
        "peak_snr_db": peak_snr_db,
    }


def upsert_pass(conn: sqlite3.Connection, source_file: str, data: dict[str, Any]) -> None:
    pass_id = str(data["pass_id"])
    setup_id = get_or_create_setup_id(conn, data)
    samples = list(data.get("samples", []))
    metrics = compute_metrics(samples)
    imported_at = utc_now_iso()

    conn.execute("DELETE FROM pass_detail WHERE pass_id = ?", (pass_id,))
    conn.execute("DELETE FROM pass_header WHERE pass_id = ?", (pass_id,))

    conn.execute(
        """
        INSERT INTO pass_header (
            pass_id, setup_id, source_file, satellite, pipeline,
            frequency_hz, bandwidth_hz, gain, source_id, bias_t,
            pass_start, pass_end, scheduled_start, scheduled_end,
            sample_count, visible_sample_count,
            start_azimuth_deg, mid_azimuth_deg, end_azimuth_deg, max_elevation_deg,
            direction, first_deframer_sync_delay_seconds, total_deframer_synced_seconds,
            sync_drop_count, median_snr_synced, median_ber_synced, peak_snr_db,
            imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pass_id,
            setup_id,
            source_file,
            str(data["satellite"]),
            str(data["pipeline"]),
            int(data["frequency_hz"]),
            int(data["bandwidth_hz"]),
            float(data["gain"]),
            str(data.get("source_id", "")),
            1 if bool(data.get("bias_t", False)) else 0,
            str(data["pass_start"]),
            str(data["pass_end"]),
            str(data["scheduled_start"]),
            str(data["scheduled_end"]),
            int(metrics["sample_count"]),
            int(metrics["visible_sample_count"]),
            metrics["start_azimuth_deg"],
            metrics["mid_azimuth_deg"],
            metrics["end_azimuth_deg"],
            metrics["max_elevation_deg"],
            metrics["direction"],
            metrics["first_deframer_sync_delay_seconds"],
            metrics["total_deframer_synced_seconds"],
            int(metrics["sync_drop_count"]),
            metrics["median_snr_synced"],
            metrics["median_ber_synced"],
            metrics["peak_snr_db"],
            imported_at,
        ),
    )

    if samples:
        detail_rows = []
        for s in samples:
            detail_rows.append(
                (
                    pass_id,
                    str(s["timestamp"]),
                    float(s["snr_db"]),
                    float(s["peak_snr_db"]),
                    float(s["ber"]),
                    str(s.get("viterbi_state", "NOSYNC")),
                    str(s.get("deframer_state", "NOSYNC")),
                    float(s["azimuth_deg"]),
                    float(s["elevation_deg"]),
                )
            )

        conn.executemany(
            """
            INSERT INTO pass_detail (
                pass_id, timestamp, snr_db, peak_snr_db, ber,
                viterbi_state, deframer_state, azimuth_deg, elevation_deg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            detail_rows,
        )


def resolve_input_files(args, base_dir: str) -> list[str]:
    if args.all:
        passes_dir = os.path.join(base_dir, "results", "passes")
        return sorted(str(p) for p in Path(passes_dir).glob("*-reception.json"))

    if args.input:
        return [os.path.abspath(args.input)]

    raise SystemExit("Provide one input JSON file or use --all")


def main() -> int:
    args = parse_args()
    config_path = get_config_path(args.config)

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"[import_reception_to_db] CONFIG ERROR: {e}")
        return 1

    if not config["reception_db"]["enabled"]:
        print("[import_reception_to_db] reception_db disabled in config")
        return 0

    db_path = str(config["reception_db"]["db_path"])
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_files = resolve_input_files(args, base_dir)

    if not input_files:
        print("[import_reception_to_db] no input files found")
        return 1

    conn = open_db(db_path)
    imported = 0

    try:
        for path in input_files:
            if not os.path.exists(path):
                print(f"[import_reception_to_db] missing file: {path}")
                continue

            data = load_json(path)
            upsert_pass(conn, path, data)
            imported += 1
            print(f"[import_reception_to_db] imported: {path}")

        conn.commit()
    finally:
        conn.close()

    print(f"[import_reception_to_db] database: {db_path}")
    print(f"[import_reception_to_db] imported count: {imported}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
