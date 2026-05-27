#!/usr/bin/env python3
"""satpi – receive_pass

Receive a single satellite pass and save raw data.

Receives the specified satellite pass (identified by pass-id) using SatDump
and saves the demodulated CADU stream to the configured output directory.
While SatDump is running, a parallel reader thread parses its INFO-level
status lines and a PassSampler accumulates per-sample telemetry (SNR / BER /
sync state / az / el) into reception.json["samples"]. Offline decoding of
the CADU into image products is the responsibility of post_processing.py.

Called by receive_orchestrator.py or systemd units.

Author: Andreas Horvath
Project: Autonomous, config-driven satellite reception pipeline for Raspberry Pi
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from read_config import read_config, ConfigError
from pass_sampler import PassSampler

logger = logging.getLogger("satpi.receive_pass")


def setup_logger() -> None:
    """Setup logging to stderr."""
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)


@contextmanager
def _pass_log_file(pass_dir: str) -> Iterator[str]:
    """Attach a per-pass FileHandler that writes to <pass_dir>/receive_pass.log
    for the duration of the with-block. Co-locates the Python log with the
    captured data so each pass directory becomes self-contained forensics."""
    log_path = os.path.join(pass_dir, "receive_pass.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    try:
        yield log_path
    finally:
        logger.removeHandler(fh)
        fh.close()


def find_pass_in_json(
    pass_id: str, pass_file: str
) -> Optional[Dict[str, Any]]:
    """Find a pass by pass-id in passes.json.

    Args:
        pass_id: The pass identifier (format: YYYY-MM-DD_HH-MM-SS_SATELLITE)
        pass_file: Path to passes.json

    Returns:
        Pass data dict if found, None otherwise
    """
    if not os.path.exists(pass_file):
        logger.error("Passes file not found: %s", pass_file)
        return None

    try:
        with open(pass_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load passes file: %s", e)
        return None

    passes = data.get("passes", [])
    for pass_record in passes:
        if pass_record.get("pass-id") == pass_id:
            return pass_record

    logger.error("Pass not found in passes.json: %s", pass_id)
    return None


def calculate_duration_sec(start_iso: str, end_iso: str) -> int:
    """Calculate duration in seconds from ISO format timestamps.

    Args:
        start_iso: Start time in ISO format (e.g., "2026-05-13T12:00:00Z")
        end_iso: End time in ISO format (e.g., "2026-05-13T12:15:30Z")

    Returns:
        Duration in seconds
    """
    try:
        start_str = start_iso.replace("Z", "+00:00")
        end_str = end_iso.replace("Z", "+00:00")

        start_dt = datetime.fromisoformat(start_str)
        end_dt = datetime.fromisoformat(end_str)

        duration = int((end_dt - start_dt).total_seconds())
        return max(duration, 60)
    except (ValueError, TypeError) as e:
        logger.warning("Failed to parse timestamps: %s", e)
        return 900


def _resolve_tle_file(config: Dict[str, Any]) -> str:
    """Resolve absolute path to the TLE file from config.

    Defaults to <repo>/results/tle/weather.tle if [paths] tle_file isn't set.
    """
    base_dir = str(Path(__file__).resolve().parent.parent)
    paths = config.get("paths", {})
    tle_file = paths.get("tle_file", "results/tle/weather.tle")
    tle_file = os.path.expanduser(tle_file)
    if not os.path.isabs(tle_file):
        tle_file = os.path.join(base_dir, tle_file)
    return tle_file


def _make_sampler(
    config: Dict[str, Any],
    satellite: str,
) -> Optional[PassSampler]:
    """Build a PassSampler from config + satellite name. Returns None on error
    so reception can proceed without telemetry collection."""
    try:
        qth = config.get("qth", {})
        qth_lat = float(qth.get("latitude", 0.0))
        qth_lon = float(qth.get("longitude", 0.0))
        qth_alt = float(qth.get("altitude_m", 0.0))
        tle_file = _resolve_tle_file(config)
        return PassSampler(satellite, tle_file, qth_lat, qth_lon, qth_alt)
    except Exception as e:
        logger.warning("Could not initialize PassSampler: %s — proceeding without samples", e)
        return None


def receive_satellite_pass(
    config: Dict[str, Any],
    pass_data: Dict[str, Any],
    pass_id: str,
    testmode: bool = False,
) -> tuple[bool, Optional[str]]:
    """Receive a satellite pass via SatDump and collect per-sample telemetry.

    Performs *only* the live demodulation step (no offline decode — that
    runs in post_processing.py). While SatDump is running, a reader thread
    mirrors its stdout/stderr to <pass_dir>/satdump_live.log and feeds each
    line into a PassSampler for SNR/BER/sync-state extraction. Az/el is
    computed from TLE + QTH per sample. The final samples array lands in
    reception.json so import_to_db.py and plot_reception.py can use it.

    Returns:
        (success: bool, output_dir: Optional[str])
    """
    satellite = pass_data.get("satellite", "UNKNOWN")
    frequency_hz = pass_data.get("frequency_hz")
    bandwidth_hz = pass_data.get("bandwidth_hz")
    pipeline = pass_data.get("pipeline", "lrpt")
    start_iso = pass_data.get("start")
    end_iso = pass_data.get("end")

    if not frequency_hz:
        logger.error("No frequency specified for %s", satellite)
        return False, None

    if testmode:
        duration_sec = config.get("testing", {}).get("duration_seconds", 60)
        logger.info("TEST MODE: Using configured duration of %d seconds", duration_sec)
    else:
        duration_sec = calculate_duration_sec(start_iso, end_iso)

    # Create output directory
    output_dir = config.get("paths", {}).get("output_dir", "/tmp/satpi_output")
    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # Create pass-specific subdirectory using pass_id
    pass_dir = os.path.join(output_dir, pass_id)
    os.makedirs(pass_dir, exist_ok=True)

    # From here on out, mirror all log output into the pass directory.
    with _pass_log_file(pass_dir):
        logger.info("Receiving %s (%s) for %d seconds", satellite, pass_id, duration_sec)
        logger.info("Output directory: %s", pass_dir)

        hardware = config.get("hardware", {})
        source_id = hardware.get("source_id", "00000001")
        gain = hardware.get("gain", 38.6)
        sample_rate = hardware.get("sample_rate", 2.4e6)
        bias_t = hardware.get("bias_t", True)

        # Live demod only — no --finish_processing, no decode in this process.
        cmd = [
            "satdump",
            "live",
            pipeline,
            pass_dir,
            "--source", "rtlsdr",
            "--device-id", str(source_id),
            "--samplerate", str(int(sample_rate)),
            "--frequency", str(int(frequency_hz)),
            "--gain", str(float(gain)),
            "--timeout", str(duration_sec),
        ]

        if bias_t:
            cmd.append("--bias-t")

        if bandwidth_hz:
            cmd.extend(["--bandwidth", str(int(bandwidth_hz))])

        logger.info("Command: %s", " ".join(cmd))

        sampler = _make_sampler(config, satellite)
        samples: List[Dict[str, Any]] = []
        satdump_log = os.path.join(pass_dir, "satdump_live.log")
        rc: Optional[int] = None
        timed_out = False

        # We use Popen + a reader thread (rather than subprocess.run) so we
        # can stream SatDump's output to BOTH the log file AND the sampler
        # at the same time. capture_output=True would buffer everything
        # in memory and we'd see the lines only after SatDump exits.
        with open(satdump_log, "w", encoding="utf-8") as lf:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                logger.error("satdump not found. Is it installed?")
                return False, None
            except Exception as e:
                logger.error("Failed to launch satdump: %s", e)
                return False, None

            def _reader() -> None:
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        lf.write(line)
                        lf.flush()
                        if sampler is not None:
                            sampler.feed_satdump_line(line)
                except Exception as ex:
                    logger.warning("Reader thread error: %s", ex)

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            # SatDump's own --timeout exits the pipeline cleanly. We add a
            # generous wall-clock safety net on top in case it hangs.
            try:
                rc = proc.wait(timeout=duration_sec + 30)
            except subprocess.TimeoutExpired:
                logger.error("Reception exceeded wall-clock budget, terminating satdump")
                proc.terminate()
                try:
                    rc = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.error("SatDump did not respond to SIGTERM; sending SIGKILL")
                    proc.kill()
                    rc = proc.wait()
                timed_out = True

            reader_thread.join(timeout=5)

        if sampler is not None:
            sampler.stop()
            samples = sampler.samples

        if timed_out or (rc is not None and rc != 0):
            logger.error(
                "satdump exited rc=%s (see %s for full output)",
                rc, satdump_log,
            )
            return False, None

        # Capture CADU size for downstream consumers (post_processing decode).
        cadu_file = os.path.join(pass_dir, f"{pipeline}.cadu")
        cadu_size_bytes = os.path.getsize(cadu_file) if os.path.exists(cadu_file) else 0

        reception_data = {
            "pass_id": pass_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "satellite": satellite,
            "pipeline": pipeline,
            "frequency_hz": frequency_hz,
            "bandwidth_hz": bandwidth_hz,
            "duration_sec": duration_sec,
            "output_dir": pass_dir,
            "gain": gain,
            "bias_t": bias_t,
            "sample_rate": sample_rate,
            "source_id": source_id,
            "cadu_file": cadu_file if cadu_size_bytes > 0 else None,
            "cadu_size_bytes": cadu_size_bytes,
            # Pass geometry and timing from passes.json (needed by import_to_db.py)
            "pass_start": pass_data.get("start", ""),
            "pass_end": pass_data.get("end", ""),
            "scheduled_start": pass_data.get("start", ""),
            "scheduled_end": pass_data.get("end", ""),
            "aos_azimuth_deg": pass_data.get("aos_azimuth_deg"),
            "los_azimuth_deg": pass_data.get("los_azimuth_deg"),
            "direction": pass_data.get("direction", ""),
            "samples": samples,
        }

        reception_json = os.path.join(pass_dir, "reception.json")
        try:
            with open(reception_json, "w", encoding="utf-8") as f:
                json.dump(reception_data, f, indent=2)
            logger.info(
                "Reception metadata saved: %s (%d samples)",
                reception_json, len(samples),
            )
        except (OSError, IOError) as e:
            logger.warning("Could not save reception.json: %s", e)

        if os.path.isdir(pass_dir):
            files = os.listdir(pass_dir)
            logger.info("Generated %d file(s) in pass directory", len(files))
            for f in sorted(files):
                fpath = os.path.join(pass_dir, f)
                if os.path.isfile(fpath):
                    size_mb = os.path.getsize(fpath) / (1024 * 1024)
                    if size_mb > 0.1:
                        logger.info("  %s: %.2f MB", f, size_mb)

        if cadu_size_bytes == 0:
            logger.warning(
                "No CADU file was produced (%s.cadu missing or empty). "
                "Reception probably failed or the pipeline name is wrong.",
                pipeline,
            )

        logger.info("Pass reception completed successfully")
        return True, pass_dir


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Receive a satellite pass and save raw data + telemetry",
        epilog="""
USAGE:
  python3 receive_pass.py --pass-id 2026-05-13_12-00-00_METEOR-M2-4

CONFIGURATION:
  Reads config.ini from the default location (../config/config.ini).
  Loads pass metadata from passes.json (configured path).
  Output directory specified in config [paths] section.

This script only performs live demodulation (CADU output) and per-sample
telemetry collection (SNR/BER/sync/az/el → reception.json["samples"]).
Decoding of the CADU stream into images is done by post_processing.py --decode.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--pass-id",
        required=True,
        help="Pass identifier (format: YYYY-MM-DD_HH-MM-SS_SATELLITE)",
    )

    parser.add_argument(
        "--testmode",
        action="store_true",
        help="Use test mode with configured duration instead of actual pass duration",
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()

    base_dir = str(Path(__file__).resolve().parent.parent)
    config_path = os.path.join(base_dir, "config", "config.ini")

    try:
        config = read_config(config_path)
    except ConfigError as e:
        print(f"[receive_pass] CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    setup_logger()
    logger.info("receive_pass started with pass-id=%s", args.pass_id)

    pass_file = config.get("paths", {}).get("pass_file", "results/passes.json")
    pass_file = os.path.expanduser(pass_file)
    if not os.path.isabs(pass_file):
        pass_file = os.path.join(base_dir, pass_file)

    pass_data = find_pass_in_json(args.pass_id, pass_file)
    if not pass_data:
        logger.error("Could not find pass: %s", args.pass_id)
        return 1

    success, output_dir = receive_satellite_pass(
        config, pass_data, args.pass_id, testmode=args.testmode,
    )

    if not success:
        logger.error("Pass reception failed")
        return 1

    if output_dir:
        print(output_dir)

    logger.info("receive_pass completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
