#!/usr/bin/env python3
"""satpi – update_tle

Downloads and filters TLE data for the configured satellites.

Retrieves current orbital data from the configured remote source, verifies that
the download succeeded and writes a filtered local TLE file containing only the
satellites used by this installation. It is the first step in the planning
chain because pass prediction depends on up-to-date orbital data.

Author: Andreas Horvath
Project: Autonomous, config-driven satellite reception pipeline for Raspberry Pi
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import sys
import tempfile
import time
import argparse
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple

import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from read_config import read_config, ConfigError



# --- Constants ---------------------------------------------------------------

CONNECT_TIMEOUT = 5           # seconds
READ_TIMEOUT = 30             # seconds
HTTP_RETRIES = 3
RETRY_BACKOFF = 2             # seconds (exponential: 2, 4, 8, ...)
REACHABILITY_PROBE_URL = "https://www.google.com"
CELESTRAK_PROBE_URL = "https://celestrak.org"
MAX_FALLBACK_AGE_DAYS = 5     # warn-only fallback beyond this is risky
LOG_MAX_BYTES = 1_000_000     # 1 MB per log file
LOG_BACKUP_COUNT = 5

logger = logging.getLogger("satpi.update")


# --- Logging -----------------------------------------------------------------

def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "update_tle.log")

    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


# --- HTTP helpers ------------------------------------------------------------

def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=HTTP_RETRIES,
        connect=HTTP_RETRIES,
        read=HTTP_RETRIES,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def check_url(url: str, timeout: Tuple[int, int] = (CONNECT_TIMEOUT, READ_TIMEOUT)) -> bool:
    """Return True if *url* is reachable with a 2xx/3xx response."""
    try:
        with _build_session() as s:
            # Some servers (including Celestrak at times) don't support HEAD,
            # so fall back to a streamed GET that we close immediately.
            r = s.head(url, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400:
                r = s.get(url, timeout=timeout, stream=True)
                r.close()
            return r.status_code < 400
    except requests.RequestException:
        return False



def download_tle_n2yo_multi(api_key: str, target: str, satellites: List[dict]) -> None:
    """Download TLEs from N2YO API for multiple satellites and combine them."""
    all_tle_lines = []

    for sat in satellites:
        if not sat.get("norad_id"):
            logger.warning(f"Satellite '{sat['name']}' missing norad_id, skipping")
            continue

        norad_id = sat["norad_id"]
        url = f"https://api.n2yo.com/rest/v1/satellite/tle/{norad_id}/?apiKey={api_key}"

        try:
            logger.info(f"Fetching TLE for {sat['name']} (NORAD {norad_id})")
            with _build_session() as s, s.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
                r.raise_for_status()
                data = r.json()
                if "tle" not in data:
                    logger.warning(f"No TLE field for {sat['name']}")
                    continue

                tle_data = data["tle"]
                all_tle_lines.append(sat["name"])
                for line in tle_data.strip().split(chr(10)):
                    all_tle_lines.append(line)
        except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to fetch TLE for {sat['name']}: {e}")
            continue

    if not all_tle_lines:
        raise RuntimeError("No TLEs successfully downloaded from N2YO")

    with open(target, "w") as f:
        f.write("\n".join(all_tle_lines) + "\n")

    if os.path.getsize(target) == 0:
        raise RuntimeError("TLE file is empty")


# ---------------------------------------------------------------------------
# Celestrak GP JSON → classic TLE helpers
# ---------------------------------------------------------------------------

def _tle_checksum(line: str) -> int:
    """TLE line checksum: sum of all digit characters plus 1 for each '-', mod 10."""
    return sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line) % 10


def _format_exp_notation(value: float) -> str:
    """Format a float in TLE exponential notation (8 chars): [sign]NNNNN[sign]N.

    Examples:
        0          →  " 00000+0"
        3.1151e-5  →  " 31151-4"   (0.31151 × 10^-4)
       -1.1606e-5  →  "-11606-4"
    """
    if value == 0.0:
        return " 00000+0"
    sign = " " if value >= 0 else "-"
    abs_val = abs(value)
    exp = math.floor(math.log10(abs_val)) + 1          # 0.1 ≤ abs_val/10^exp < 1
    mantissa_int = round(abs_val / (10.0 ** exp) * 1e5)
    if mantissa_int >= 100000:                          # rounding overflow
        mantissa_int //= 10
        exp += 1
    exp_sign = "+" if exp >= 0 else "-"
    return f"{sign}{mantissa_int:05d}{exp_sign}{abs(exp)}"


def _format_mean_motion_dot(value: float) -> str:
    """Format MEAN_MOTION_DOT for TLE line 1 cols 34-43 (10 chars): [sign].NNNNNNNN.

    GP JSON stores the value directly (same as TLE; no extra division needed).
    """
    sign = " " if value >= 0 else "-"
    return f"{sign}.{round(abs(value) * 1e8):08d}"


def _parse_intl_designator(object_id: str) -> str:
    """Convert OBJECT_ID ('2024-039A') to TLE international designator (8 chars, left-justified).

    '2024-039A' → '24039A  '
    """
    if not object_id:
        return "        "
    try:
        parts = object_id.split("-", 1)
        if len(parts) == 2:
            yy = parts[0][-2:]        # last 2 digits of year
            rest = parts[1]           # e.g. "039A"
            return f"{yy}{rest:<6}"[:8]
    except Exception:
        pass
    return "        "


def _epoch_to_tle(epoch_str: str) -> str:
    """Convert ISO 8601 epoch string to TLE epoch format YYDDD.DDDDDDDD (14 chars)."""
    epoch_clean = epoch_str.rstrip("Z").split("+")[0]
    try:
        dt = datetime.strptime(epoch_clean, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        dt = datetime.strptime(epoch_clean, "%Y-%m-%dT%H:%M:%S")
    yy = dt.year % 100
    day_of_year = dt.timetuple().tm_yday
    frac = (dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6) / 86400
    return f"{yy:02d}{day_of_year + frac:012.8f}"


def gp_json_to_tle_lines(entry: dict, canonical_name: str) -> Tuple[str, str, str]:
    """Convert a Celestrak GP JSON entry to classic 3-line TLE.

    GP JSON field values map directly to TLE values (no extra division needed).
    Returns (name_line, line1, line2), each suitable for writing to a .tle file.

    Verified against live Celestrak data for METEOR-M2 4 (NORAD 59051).
    """
    norad = int(entry["NORAD_CAT_ID"])
    cls = str(entry.get("CLASSIFICATION_TYPE", "U"))[:1]
    intl = _parse_intl_designator(str(entry.get("OBJECT_ID", "")))
    epoch = _epoch_to_tle(str(entry["EPOCH"]))
    mmdot = _format_mean_motion_dot(float(entry.get("MEAN_MOTION_DOT", 0)))
    mmddt = _format_exp_notation(float(entry.get("MEAN_MOTION_DDOT", 0)))
    bstar = _format_exp_notation(float(entry.get("BSTAR", 0)))
    etype = int(entry.get("EPHEMERIS_TYPE", 0))
    elset = int(entry.get("ELEMENT_SET_NO", 0))

    incl = float(entry.get("INCLINATION", 0))
    raan = float(entry.get("RA_OF_ASC_NODE", 0))
    ecco = round(float(entry.get("ECCENTRICITY", 0)) * 1e7)
    argp = float(entry.get("ARG_OF_PERICENTER", 0))
    ma = float(entry.get("MEAN_ANOMALY", 0))
    mm = float(entry.get("MEAN_MOTION", 0))
    rev = int(entry.get("REV_AT_EPOCH", 0))

    # Line 1 (68 chars before checksum digit)
    l1 = (
        f"1 {norad:05d}{cls} {intl:<8} {epoch} "
        f"{mmdot} {mmddt} {bstar} {etype} {elset:4d}"
    )
    line1 = l1 + str(_tle_checksum(l1))

    # Line 2 (68 chars before checksum digit)
    l2 = (
        f"2 {norad:05d} {incl:8.4f} {raan:8.4f} {ecco:07d} "
        f"{argp:8.4f} {ma:8.4f} {mm:11.8f}{rev:5d}"
    )
    line2 = l2 + str(_tle_checksum(l2))

    return canonical_name, line1, line2


def download_tle_celestrak_gp_json(
    url: str, target: str, satellites: List[dict]
) -> None:
    """Download TLEs from Celestrak GP JSON API and write classic 3-line TLE format.

    Celestrak GP JSON (FORMAT=json) returns an array of GP objects with orbital
    elements. TLE lines are reconstructed from these elements.

    Satellites are matched by NORAD_CAT_ID (primary) with name fallback.

    Example URL:
      https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=json
    """
    norad_id_map: dict = {
        s["norad_id"]: s["name"] for s in satellites if s.get("norad_id")
    }
    name_map: dict = {
        normalize_sat_name(s["name"]): s["name"] for s in satellites
    }

    logger.info("Downloading Celestrak GP JSON from %s", url)
    try:
        with _build_session() as sess:
            r = sess.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
            data = r.json()
    except requests.RequestException as e:
        raise RuntimeError(f"Celestrak GP JSON download failed: {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Celestrak GP JSON parse error: {e}") from e

    if not isinstance(data, list):
        raise RuntimeError(
            f"Celestrak GP JSON: expected a JSON array, got {type(data).__name__}. "
            f"Verify FORMAT=json is in the URL."
        )

    logger.info("Celestrak GP JSON: %d entries in response", len(data))

    matched_lines: List[str] = []
    found_norad_ids: Set[int] = set()

    for entry in data:
        norad_id = entry.get("NORAD_CAT_ID")
        obj_name = str(entry.get("OBJECT_NAME", "")).strip()

        # Match by NORAD ID first (most reliable)
        if norad_id in norad_id_map:
            canonical_name = norad_id_map[norad_id]
        elif normalize_sat_name(obj_name) in name_map:
            canonical_name = name_map[normalize_sat_name(obj_name)]
        else:
            continue

        try:
            name_line, line1, line2 = gp_json_to_tle_lines(entry, canonical_name)
        except Exception as exc:
            logger.warning("Skipping %s: TLE construction failed: %s", obj_name, exc)
            continue

        matched_lines.append(f"{name_line}\n{line1}\n{line2}\n")
        found_norad_ids.add(norad_id)
        logger.debug("Matched %s (NORAD %s)", canonical_name, norad_id)

    if not matched_lines:
        raise RuntimeError(
            f"No configured satellites found in Celestrak GP JSON response "
            f"(searched NORAD IDs: {sorted(norad_id_map.keys())}, "
            f"response had {len(data)} entries)."
        )

    with open(target, "w", encoding="utf-8") as f:
        f.writelines(matched_lines)

    missing_ids = set(norad_id_map.keys()) - found_norad_ids
    if missing_ids:
        logger.warning(
            "Satellites not found in GP JSON response: %s",
            [norad_id_map[n] for n in sorted(missing_ids)],
        )
    logger.info("Celestrak GP JSON: wrote %d satellite(s) to %s", len(matched_lines), target)


def download_tle(url: str, target: str, tle_format: str = "TXT") -> None:
    """Download TLE data and save to target. Handles JSON and TXT formats."""
    try:
        with _build_session() as s, s.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True) as r:
            r.raise_for_status()

            if tle_format == "JSON":
                # Parse JSON response and extract TLE field
                data = r.json()
                if "tle" not in data:
                    raise RuntimeError("JSON response missing 'tle' field")
                tle_data = data["tle"]
                with open(target, "w") as f:
                    f.write(tle_data)
            else:
                # Stream TXT format directly
                with open(target, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
    except requests.RequestException as e:
        raise RuntimeError(f"TLE download failed: {e}") from e
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"TLE JSON parsing failed: {e}") from e

    if not os.path.exists(target) or os.path.getsize(target) == 0:
        raise RuntimeError("TLE download failed: empty file")


# --- TLE parsing / filtering -------------------------------------------------

def normalize_sat_name(name: str) -> str:
    return " ".join(name.strip().upper().replace("-", " ").replace("_", " ").split())


def _is_tle_line1(line: str) -> bool:
    return line.startswith("1 ") and len(line) >= 24


def _is_tle_line2(line: str) -> bool:
    return line.startswith("2 ") and len(line) >= 24


def filter_tle(input_file: str, output_file: str, satellite_names: Sequence[str]) -> None:
    """Write the 3-line TLE blocks for satellite_names from input_file to output_file.

    Raises RuntimeError on malformed TLE entries or when no satellites match.
    Logs a warning for any configured satellite that wasn't found.
    """
    normalized_targets: Set[str] = {normalize_sat_name(s) for s in satellite_names}
    found_normalized: Set[str] = set()
    found_names: List[str] = []

    tmp_output = output_file + ".tmp"

    with open(input_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    try:
        with open(tmp_output, "w", encoding="utf-8") as out:
            i = 0
            n = len(lines)
            while i < n:
                raw = lines[i]
                name_line = raw.strip()

                # Skip blank lines and obvious TLE data lines — those can't be names.
                if not name_line or _is_tle_line1(name_line) or _is_tle_line2(name_line):
                    i += 1
                    continue

                if normalize_sat_name(name_line) not in normalized_targets:
                    i += 1
                    continue

                if i + 2 >= n:
                    raise RuntimeError(f"Incomplete TLE entry for satellite: {name_line}")

                l1, l2 = lines[i + 1].rstrip("\n"), lines[i + 2].rstrip("\n")
                if not _is_tle_line1(l1) or not _is_tle_line2(l2):
                    raise RuntimeError(
                        f"Malformed TLE entry for satellite {name_line!r}: "
                        f"expected '1 ...' / '2 ...' lines."
                    )

                out.write(raw)
                out.write(lines[i + 1])
                out.write(lines[i + 2])
                found_names.append(name_line)
                found_normalized.add(normalize_sat_name(name_line))
                i += 3

        if not os.path.exists(tmp_output) or os.path.getsize(tmp_output) == 0:
            raise RuntimeError(
                f"No matching satellites found in TLE. Configured: {list(satellite_names)}"
            )

        os.replace(tmp_output, output_file)
    finally:
        # If os.replace succeeded, the file is gone; otherwise clean it up.
        if os.path.exists(tmp_output):
            try:
                os.remove(tmp_output)
            except OSError:
                pass

    missing = normalized_targets - found_normalized
    if missing:
        logger.warning("Configured satellites NOT found in TLE: %s", sorted(missing))
    logger.info("Matched satellites in TLE: %s", found_names)


# --- Local TLE freshness checks ---------------------------------------------

def has_usable_tle_file(tle_file: str) -> bool:
    if not os.path.exists(tle_file) or os.path.getsize(tle_file) == 0:
        return False
    with open(tle_file, "r", encoding="utf-8") as f:
        non_empty = [ln.strip() for ln in f if ln.strip()]
    return len(non_empty) >= 3


def tle_age_days(tle_file: str) -> float:
    return (time.time() - os.path.getmtime(tle_file)) / 86400.0


def _use_existing_tle_if_possible(tle_file: str, reason: str) -> bool:
    """Log and return True if an existing local TLE can serve as fallback."""
    if not has_usable_tle_file(tle_file):
        return False
    age = tle_age_days(tle_file)
    if age > MAX_FALLBACK_AGE_DAYS:
        logger.error(
            "%s Existing local TLE is %.1f days old (> %d). Refusing stale fallback: %s",
            reason, age, MAX_FALLBACK_AGE_DAYS, tle_file,
        )
        return False
    logger.warning("%s Using existing local TLE (%.1f days old): %s", reason, age, tle_file)
    return True


# --- Main --------------------------------------------------------------------

def _fallback_after_failure(tle_file: str) -> None:
    """Inspect connectivity and either return (fallback accepted) or raise."""
    logger.warning("Direct access to Celestrak failed, checking general internet connectivity...")
    google_ok = check_url(REACHABILITY_PROBE_URL)
    celestrak_ok = check_url(CELESTRAK_PROBE_URL)

    if not google_ok:
        if _use_existing_tle_if_possible(
            tle_file,
            "TLE download failed and general internet connectivity check also failed.",
        ):
            return
        raise RuntimeError(
            "TLE download failed and general internet connectivity check also failed. "
            "The system appears to have no working internet connection."
        )

    if not celestrak_ok:
        if _use_existing_tle_if_possible(
            tle_file,
            "TLE download failed; Celestrak appears unavailable or blocked.",
        ):
            return
        raise RuntimeError(
            "TLE download failed, but general internet connectivity is working. "
            "Access to Celestrak appears to be blocked or unavailable. "
            "Celestrak sometimes blocks IP addresses after too many requests. "
            "If this happens, use a system-wide VPN connection and try again."
        )

    if _use_existing_tle_if_possible(
        tle_file,
        "TLE download failed although general internet connectivity appears to work.",
    ):
        return
    raise RuntimeError(
        "TLE download failed although general internet connectivity appears to work. "
        "Please check the configured TLE URL and remote availability."
    )


def mirror_to_satdump(source_file: str) -> bool:
    """Mirror our local TLE file into SatDump's expected location so SatDump
    does not fall back to fetching from celestrak.org (which often blocks
    the satpi IP). Atomic write via tmp + os.replace.

    Non-fatal: warns on any error and returns False, never aborts the main
    update flow.
    """
    target = Path.home() / ".config" / "satdump" / "satdump_tles.txt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(target) + ".tmp"
        shutil.copyfile(source_file, tmp)
        os.replace(tmp, str(target))
        logger.info("Mirrored TLE to SatDump: %s", target)
        return True
    except OSError as e:
        logger.warning("Could not mirror TLE to SatDump (%s): %s", target, e)
        return False


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download and filter TLE (Two-Line Element) orbital data for configured satellites",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CONFIG.INI REQUIREMENTS:
  [network]
    - tle_url: URL of the TLE source (e.g., Celestrak or N2YO API endpoint)
    - tle_format: Format of TLE data ('TXT', 'JSON', or 'GP_JSON', default: 'TXT')
                TXT     = classic 3-line TLE (Celestrak, most sources)
                JSON    = N2YO per-satellite JSON API
                GP_JSON = Celestrak GP JSON array (requires FORMAT=json in URL)
    - api_key: (Optional) API key for N2YO service

  [paths]
    - tle_file: Output path for filtered TLE data (e.g., results/tle/weather.tle)
    - log_dir: Directory for log files

  [satellites]
    - List of satellites with 'enabled' flag and 'norad_id' (for N2YO format)

OUTPUT FILES:
  - Primary TLE: {tle_file} (from config.ini)
  - SatDump mirror: ~/.config/satdump/satdump_tles.txt (prevents SatDump from fetching from Celestrak)
  - Log file: {log_dir}/update_tle.log

BEHAVIOR:
  - Downloads current TLE data from configured source
  - Filters to include only configured satellites
  - Falls back to existing local TLE if download fails (age < 5 days)
  - Mirrors TLE to SatDump directory (non-fatal if it fails)
  - Checks internet connectivity and Celestrak availability on failure
        """
    )
    return parser.parse_args()


def main() -> int:
    parse_args()  # Process --help / -h if requested
    base_dir = Path(__file__).resolve().parent.parent
    config_path = base_dir / "config" / "config.ini"

    try:
        config = read_config(str(config_path))
    except ConfigError as e:
        print(f"[update] CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    setup_logging(config["paths"]["log_dir"])
    # ... rest of function (unchanged)

    tle_url = config["network"]["tle_url"]
    api_key = config["network"].get("api_key", "")
    tle_format = config["network"].get("tle_format", "TXT").upper()
    tle_file = config["paths"]["tle_file"]
    satellites = [s for s in config["satellites"] if s["enabled"]]

    # Append api_key to N2YO URL if available
    if api_key and "n2yo.com" in tle_url:
        separator = "&" if "?" in tle_url else "?"
        tle_url = f"{tle_url}{separator}apiKey={api_key}"

    if not satellites:
        logger.error("No enabled satellites in config – nothing to do.")
        return 2

    tle_dir = os.path.dirname(tle_file)
    if tle_dir:
        os.makedirs(tle_dir, exist_ok=True)

    fd, tmp_file = tempfile.mkstemp(prefix="satpi_tle_", suffix=".tmp")
    os.close(fd)

    try:
        logger.info("Downloading TLE (format: %s)", tle_format)
        try:
            if tle_format == "GP_JSON":
                # Celestrak GP JSON array — TLE lines are reconstructed from orbital elements
                download_tle_celestrak_gp_json(tle_url, tmp_file, satellites)
            elif tle_format == "JSON" and api_key:
                # N2YO per-satellite JSON API
                download_tle_n2yo_multi(api_key, tmp_file, satellites)
            else:
                # Classic 3-line TLE text (Celestrak FORMAT=tle, or any other TXT source)
                logger.info("Downloading from %s", tle_url)
                download_tle(tle_url, tmp_file, tle_format)

            sat_names = [s["name"] for s in satellites]
            logger.info("Filtering satellites: %s", sat_names)
            filter_tle(tmp_file, tle_file, sat_names)

            logger.info("TLE update successful: %s", tle_file)
            mirror_to_satdump(tle_file)
            return 0

        except RuntimeError:
            _fallback_after_failure(tle_file)
            mirror_to_satdump(tle_file)
            return 0  # fallback accepted

    except Exception as e:
        logger.exception("update_tle failed: %s", e)
        return 1

    finally:
        if os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
