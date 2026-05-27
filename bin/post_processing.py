#!/usr/bin/env python3
"""satpi – post_processing

Postprocessing nach Pass-Aufzeichnung: Decode, Copy, Notify, DB-Import, Plotting

Ein oder mehrere bereits aufgezeichnete Pässe verarbeiten.
Abhängig von Execution-Flags:
  --decode → SatDump offline CADU-Decode (Bilder erzeugen)
  --copy   → rclone copy zum Remote
  --notify → Mail-Benachrichtigung senden
  --db     → Import zu SQLite reception.db
  --plots  → Erzeuge Plots via plot_receptions.py

Kann manuell aufgerufen oder vom receive_orchestrator.py gesteuert werden.

Author: Andreas Horvath
Project: satpi
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from read_config import read_config, ConfigError


logger = logging.getLogger("satpi.post_processing")


def str2bool(v: Optional[str]) -> bool:
    """Convert string to boolean. Used for --copy true/false arguments."""
    if v is None:
        return True  # --flag without argument defaults to True
    if isinstance(v, bool):
        return v
    if v.lower() in ('true', '1', 'yes', 'y'):
        return True
    elif v.lower() in ('false', '0', 'no', 'n'):
        return False
    else:
        raise argparse.ArgumentTypeError(f"Boolean value expected, got: {v}")


# --- Constants ---------------------------------------------------------------

DECODE_TIMEOUT_SECONDS = 30 * 60
COPY_TIMEOUT_SECONDS = 30 * 60
MAIL_TIMEOUT_SECONDS = 60
DB_IMPORT_TIMEOUT_SECONDS = 120
PLOT_TIMEOUT_SECONDS = 180
RCLONE_LINK_TIMEOUT_SECONDS = 60

# A pass is treated as "already decoded" if the subdirectory named by
# [decode] success_dir_relpath (or the per-satellite override) exists.
# Example: for METEOR LRPT this is "MSU-MR".


# --- Helpers -----------------------------------------------------------------

def setup_logger(log_file: Optional[str] = None) -> None:
    """Setup logging to stderr and optionally to file."""
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)


@contextmanager
def _pass_log_file(pass_dir: str) -> Iterator[str]:
    """Attach a per-pass FileHandler that writes to <pass_dir>/post_processing.log
    for the duration of the with-block. Lets us co-locate logs with the data
    so each pass directory becomes self-contained forensics."""
    log_path = os.path.join(pass_dir, "post_processing.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    try:
        yield log_path
    finally:
        logger.removeHandler(fh)
        fh.close()


def _run_with_timeout(
    cmd: List[str],
    *,
    timeout: int,
    log_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> int:
    """Run a command with timeout, log output if requested."""
    logger.info("Running: %s", " ".join(cmd))

    try:
        with open(log_path, "w", encoding="utf-8") if log_path else open(os.devnull, "w") as lf:
            proc = subprocess.run(
                cmd,
                stdout=lf if log_path else subprocess.PIPE,
                stderr=subprocess.STDOUT if log_path else subprocess.PIPE,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
    except subprocess.TimeoutExpired:
        logger.error("Command timed out after %ds: %s", timeout, " ".join(cmd))
        return 124

    if proc.returncode != 0:
        logger.error("Command failed with rc=%s", proc.returncode)
        if proc.stderr and proc.stderr.strip():
            logger.error("stderr: %s", proc.stderr.strip())
    else:
        logger.info("Command succeeded")

    return proc.returncode


# --- Pass Finding ------------------------------------------------------------

def find_pass_dirs(output_dir: str, count: int) -> List[Tuple[str, str]]:
    """Find the last N pass directories sorted by timestamp.

    Returns: [(pass_name, pass_dir), ...]
    """
    if not os.path.isdir(output_dir):
        logger.error("output_dir does not exist: %s", output_dir)
        return []

    entries = []
    for entry in os.listdir(output_dir):
        entry_path = os.path.join(output_dir, entry)
        if os.path.isdir(entry_path):
            entries.append((entry, entry_path))

    # Sort by name (contains timestamp: YYYY-MM-DD_HH-MM-SS_...)
    entries.sort(reverse=True)
    return entries[:count]


def load_pass_file(pass_dir: str) -> Optional[Dict[str, Any]]:
    """Load reception.json from pass directory."""
    reception_json = os.path.join(pass_dir, "reception.json")
    if not os.path.exists(reception_json):
        logger.error("reception.json not found: %s", reception_json)
        return None

    try:
        with open(reception_json, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load reception.json: %s", e)
        return None


# --- Postprocessing Steps ---------------------------------------------------

def _resolve_decoder_pipeline(
    config: Dict[str, Any],
    reception_payload: Dict[str, Any],
) -> str:
    """Resolve the offline decoder pipeline ID for a recorded pass.

    SatDump 1.2.x re-enters the SAME pipeline at a different input level for
    offline replay (e.g. `satdump meteor_m2-x_lrpt cadu file.cadu out/`), so
    the default is to reuse the live pipeline name. A per-satellite override
    is still honored for unusual setups.

    Order of precedence:
      1. [satellite.<name>] decoder_pipeline = ...   (explicit override)
      2. live pipeline name from reception.json      (SatDump's normal pattern)
    """
    live_pipeline = reception_payload.get("pipeline", "")
    satellite = reception_payload.get("satellite", "")

    sat_cfg = next(
        (s for s in config.get("satellites", []) if s.get("name") == satellite),
        {},
    )
    override = sat_cfg.get("decoder_pipeline")
    if override:
        return str(override)
    return str(live_pipeline)


def _resolve_decode_input_level(
    config: Dict[str, Any],
    reception_payload: Dict[str, Any],
) -> str:
    """Resolve the input_level argument for SatDump's offline replay.

    For LRPT/HRPT-style pipelines the intermediate SatDump writes during live
    capture is `cadu`. Other pipelines may use `frames`, `soft_symbols`, etc.

    Order of precedence:
      1. [satellite.<name>] decode_input_level = ...
      2. [decode] input_level = ...
      3. "cadu"
    """
    satellite = reception_payload.get("satellite", "")
    sat_cfg = next(
        (s for s in config.get("satellites", []) if s.get("name") == satellite),
        {},
    )
    return str(
        sat_cfg.get("decode_input_level")
        or config.get("decode", {}).get("input_level", "cadu")
    )


def _resolve_success_dir_relpath(
    config: Dict[str, Any],
    reception_payload: Dict[str, Any],
) -> str:
    """Resolve the subdirectory name SatDump creates on a successful decode.

    For METEOR LRPT this is `MSU-MR`; for NOAA HRPT it'd be `AVHRR`, etc. Used
    to detect 'already decoded' so we can skip without --force-decode.

    Order of precedence:
      1. [satellite.<name>] success_dir_relpath = ...
      2. [decode] success_dir_relpath = ...
      3. ""  (no check; we then never auto-skip)
    """
    satellite = reception_payload.get("satellite", "")
    sat_cfg = next(
        (s for s in config.get("satellites", []) if s.get("name") == satellite),
        {},
    )
    return str(
        sat_cfg.get("success_dir_relpath")
        or config.get("decode", {}).get("success_dir_relpath", "")
    )


def decode_pass(
    config: Dict[str, Any],
    pass_id: str,
    pass_dir: str,
    reception_payload: Dict[str, Any],
    force: bool = False,
) -> bool:
    """Offline-decode the recorded CADU stream into image products.

    Looks at <pass_dir>/<live_pipeline>.cadu, sanity-checks the file size,
    then runs SatDump's offline CADU pipeline. Output (images, dataset.json)
    is written into pass_dir alongside the CADU and reception.json.

    Skips silently if:
      - the CADU file is missing
      - the CADU file is smaller than [decode] min_cadu_size_bytes
      - the pass already has the success_dir_relpath subdirectory and force=False

    Returns: success: bool
    """
    dec_cfg = config.get("decode", {})
    min_size_bytes = int(dec_cfg.get("min_cadu_size_bytes", 1048576))

    live_pipeline = reception_payload.get("pipeline")
    if not live_pipeline:
        logger.error("reception.json has no pipeline field, cannot decode %s", pass_id)
        return False

    cadu_file = os.path.join(pass_dir, f"{live_pipeline}.cadu")
    if not os.path.exists(cadu_file):
        logger.warning("No CADU file at %s, skipping decode", cadu_file)
        return False

    cadu_size = os.path.getsize(cadu_file)
    if cadu_size < min_size_bytes:
        logger.warning(
            "CADU too small (%d bytes < %d bytes), skipping decode",
            cadu_size, min_size_bytes,
        )
        return False

    # Skip if already decoded (success_dir_relpath subdirectory present).
    success_dir_relpath = _resolve_success_dir_relpath(config, reception_payload)
    if success_dir_relpath:
        success_dir = os.path.join(pass_dir, success_dir_relpath)
        if os.path.isdir(success_dir) and not force:
            logger.info(
                "Pass already decoded (%s/ exists), skipping. Use --force-decode to redo.",
                success_dir_relpath,
            )
            return True

    decoder_pipeline = _resolve_decoder_pipeline(config, reception_payload)
    input_level = _resolve_decode_input_level(config, reception_payload)

    logger.info(
        "Decoding %s with pipeline '%s' level=%s (input %.1f MB)",
        pass_id, decoder_pipeline, input_level, cadu_size / (1024 * 1024),
    )

    # SatDump 1.2.x offline replay:
    #   satdump <pipeline_id> <input_level> <input_file> <output_dir> [--extras ...]
    # In offline mode SatDump runs the pipeline to completion automatically
    # (no --finish_processing needed; that flag is for live mode only).
    cmd = [
        "satdump",
        decoder_pipeline,
        input_level,
        cadu_file,
        pass_dir,
    ]

    decode_log = os.path.join(pass_dir, "decode.log")
    rc = _run_with_timeout(
        cmd,
        timeout=DECODE_TIMEOUT_SECONDS,
        log_path=decode_log,
    )

    if rc != 0:
        logger.error("Decode failed (rc=%s). Full satdump output: %s", rc, decode_log)
        return False

    logger.info("Decode completed for %s", pass_id)
    return True


def copy_output(
    config: Dict[str, Any],
    pass_id: str,
    pass_output_dir: str,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Copy pass output via rclone, optionally generate link.

    Returns: (success: bool, target_path: Optional[str], link: Optional[str])
    """
    cfg = config.get("copytarget", {})
    if not cfg.get("enabled"):
        logger.info("Copy target disabled")
        return False, None, None

    if cfg.get("type") != "rclone":
        logger.error("Unsupported copy type: %s", cfg.get("type"))
        return False, None, None

    remote = str(cfg.get("rclone_remote", "")).strip()
    rclone_dir = str(cfg.get("rclone_dir", "")).strip()

    if not remote or not rclone_dir:
        logger.error("rclone target not fully configured (remote=%s, dir=%s)", remote, rclone_dir)
        return False, None, None

    pass_name = os.path.basename(pass_output_dir)
    target = f"{remote}:{rclone_dir}/{pass_name}"

    log_dir = config.get("paths", {}).get("log_dir", "/tmp")
    upload_log = os.path.join(log_dir, f"{pass_id}-upload.log")

    rc = _run_with_timeout(
        ["rclone", "copy", pass_output_dir, target],
        timeout=COPY_TIMEOUT_SECONDS,
        log_path=upload_log,
    )

    if rc != 0:
        logger.error("rclone copy failed (rc=%s)", rc)
        return False, target, None

    # Try to generate link if enabled
    link = None
    if cfg.get("create_link"):
        try:
            result = subprocess.run(
                ["rclone", "link", target],
                capture_output=True,
                text=True,
                check=True,
                timeout=RCLONE_LINK_TIMEOUT_SECONDS,
            )
            link = result.stdout.strip() or None
            if link:
                logger.info("Generated link: %s", link)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            logger.warning("Link generation failed: %s", e)

    return True, target, link


def send_notification(
    config: Dict[str, Any],
    reception_payload: Dict[str, Any],
    copy_ok: bool,
    target: Optional[str],
    link: Optional[str],
    copy_was_requested: bool = False,
) -> bool:
    """Send mail notification with pass summary.

    Returns: success: bool
    """
    ncfg = config.get("notify", {})
    if not ncfg.get("enabled"):
        logger.info("Notifications disabled")
        return True

    mail_to = ncfg.get("mail_to", "").strip()
    if not mail_to:
        logger.error("notify.mail_to not configured")
        return False

    mail_bin = config.get("paths", {}).get("mail_bin")
    if not mail_bin or not os.path.exists(mail_bin):
        logger.error("mail binary not found: %s", mail_bin)
        return False

    # Build message
    subject_prefix = ncfg.get("mail_subject_prefix", "[SATPI]")
    satellite = reception_payload.get("satellite", "UNKNOWN")
    subject = f"{subject_prefix} {satellite}"

    body_parts = [
        f"Satellite: {satellite}",
        f"Pass ID: {reception_payload.get('pass_id', '?')}",
        f"Samples: {len(reception_payload.get('samples', []))}",
    ]

    if link:
        body_parts.append(f"Link: {link}")
    elif copy_ok and target:
        body_parts.append(f"Target: {target}")
    elif copy_was_requested:
        body_parts.append("Copy failed -- files local only")

    body = "\n".join(body_parts) + "\n"
    mail_data = f"Subject: {subject}\n\n{body}"

    try:
        proc = subprocess.run(
            [mail_bin, mail_to],
            input=mail_data,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=MAIL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error("Mail send timed out")
        return False

    if proc.returncode != 0:
        logger.error("Mail failed (rc=%s)", proc.returncode)
        if proc.stderr.strip():
            logger.error("mail stderr: %s", proc.stderr.strip())
        return False

    logger.info("Notification sent to %s", mail_to)
    return True


def import_to_db(config: Dict[str, Any], pass_id: str) -> bool:
    """Import reception data to SQLite database using pass_id.

    Returns: success: bool
    """
    base_dir = str(Path(__file__).resolve().parent.parent)
    script = os.path.join(base_dir, "bin", "import_to_db.py")

    if not os.path.exists(script):
        logger.warning("import_to_db.py not found: %s", script)
        return False

    python_bin = config.get("paths", {}).get("python_bin", "python3")

    rc = _run_with_timeout(
        [python_bin, script, "--pass-id", pass_id],
        timeout=DB_IMPORT_TIMEOUT_SECONDS,
        cwd=base_dir,
    )

    return rc == 0


def generate_plots(
    config: Dict[str, Any],
    pass_id: str,
    config_path: Optional[str] = None,
) -> bool:
    """Generate plots for the pass using pass_id (composite key: YYYY-MM-DD_HH-MM-SS_SATELLITE).

    Returns: success: bool
    """
    base_dir = str(Path(__file__).resolve().parent.parent)
    script = os.path.join(base_dir, "bin", "plot_reception.py")

    if not os.path.exists(script):
        logger.warning("plot_reception.py not found: %s", script)
        return False

    python_bin = config.get("paths", {}).get("python_bin", "python3")

    # Build plot command with pass_id parameter
    plot_cmd = [
        python_bin, script,
        "--pass-id", pass_id,
    ]
    if config_path:
        plot_cmd.extend(["--config", config_path])

    rc = _run_with_timeout(
        plot_cmd,
        timeout=PLOT_TIMEOUT_SECONDS,
        cwd=base_dir,
    )

    return rc == 0


# --- CLI & Main --------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Post-process recorded satellite passes (decode, copy, notify, DB import, plotting)",
        epilog="""
PASS SELECTION (choose one):
  --pass-id "2026-05-04_13-45-30_METEOR_M2-X"
                             Process specific pass by ID
  --reception-json /path/to/reception.json
                             Process specific reception.json file
  --pass-output-dir /path/to/pass_dir
                             Process specific pass output directory
  --last N                   Process last N passes sequentially

EXECUTION FLAGS (combine as needed):
  --decode                   Offline-decode the recorded CADU stream into images
  --copy                     Copy pass output to rclone remote
  --notify                   Send mail notification
  --db                       Import reception data to SQLite
  --plots                    Generate plots via plot_receptions.py

RECOVERY:
  --force-decode             Re-decode even if dataset.json/products.cbor exists

EXAMPLES:
  # Process last pass with all steps
  python3 post_processing.py --last 1 --decode --db --plots --copy --notify

  # Recover a pass that was only demodulated (no images yet)
  python3 post_processing.py --pass-id "2026-05-18_02-20-30_METEOR-M2-4" --decode --db --plots

  # Force re-decode of a previously decoded pass
  python3 post_processing.py --pass-id "..." --decode --force-decode
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Input: mutually exclusive
    input_group = p.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--pass-id",
        metavar="ID",
        help="Pass ID (format: YYYY-MM-DD_HH-MM-SS_SATELLITE)",
    )
    input_group.add_argument(
        "--reception-json",
        metavar="FILE",
        help="Full path to reception.json file",
    )
    input_group.add_argument(
        "--pass-output-dir",
        metavar="DIR",
        help="Full path to pass output directory",
    )
    input_group.add_argument(
        "--last",
        type=int,
        metavar="N",
        help="Process last N passes (sequentially)",
    )

    # Execution flags (true/false or omit for true)
    p.add_argument(
        "--decode",
        type=str2bool,
        nargs="?",
        const="true",
        help="Decode CADU via SatDump offline (true/false, default: config [decode] enabled)",
    )
    p.add_argument(
        "--force-decode",
        action="store_true",
        help="Re-decode even when the success directory (e.g. MSU-MR/) already exists",
    )
    p.add_argument(
        "--copy",
        type=str2bool,
        nargs="?",
        const="true",
        help="Copy pass output to rclone (true/false, default: config setting). Omit or --copy true to enable.",
    )
    p.add_argument(
        "--notify",
        type=str2bool,
        nargs="?",
        const="true",
        help="Send mail notification (true/false, default: config setting)",
    )
    p.add_argument(
        "--db",
        type=str2bool,
        nargs="?",
        const="true",
        help="Import reception data to SQLite (true/false, default: config setting)",
    )
    p.add_argument(
        "--plots",
        type=str2bool,
        nargs="?",
        const="true",
        help="Generate plots (true/false, default: config setting)",
    )

    # Config
    p.add_argument(
        "--config",
        metavar="FILE",
        help="Path to config.ini (default: ~/satpi/config/config.ini)",
    )

    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Load config
    if args.config:
        config_path = args.config
    else:
        base_dir = str(Path(__file__).resolve().parent.parent)
        config_path = os.path.join(base_dir, "config", "config.ini")

    try:
        config = read_config(config_path)
    except ConfigError as e:
        print(f"[post_processing] CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    output_dir = config.get("paths", {}).get("output_dir")
    if not output_dir:
        print("[post_processing] output_dir not configured in config.ini", file=sys.stderr)
        return 2

    output_dir = os.path.expanduser(output_dir)

    setup_logger()

    logger.info("post_processing started")

    # Determine which passes to process
    passes_to_process: List[Tuple[str, str]] = []

    if args.last:
        logger.info("Processing last %d passes", args.last)
        passes_to_process = find_pass_dirs(output_dir, args.last)
        if not passes_to_process:
            logger.error("No passes found in %s", output_dir)
            return 1

    elif args.pass_id:
        pass_dir = os.path.join(output_dir, args.pass_id)
        if not os.path.isdir(pass_dir):
            logger.error("Pass directory not found: %s", pass_dir)
            return 1
        passes_to_process = [(args.pass_id, pass_dir)]

    elif args.reception_json:
        if not os.path.exists(args.reception_json):
            logger.error("reception.json not found: %s", args.reception_json)
            return 1
        pass_dir = os.path.dirname(args.reception_json)
        pass_id = os.path.basename(pass_dir)
        passes_to_process = [(pass_id, pass_dir)]

    elif args.pass_output_dir:
        if not os.path.isdir(args.pass_output_dir):
            logger.error("pass_output_dir not found: %s", args.pass_output_dir)
            return 1
        pass_dir = args.pass_output_dir
        pass_id = os.path.basename(pass_dir)
        passes_to_process = [(pass_id, pass_dir)]

    # Determine execution steps: CLI args override config defaults
    decode_enabled = args.decode if args.decode is not None else config.get("decode", {}).get("enabled", False)
    copy_enabled = args.copy if args.copy is not None else config.get("copytarget", {}).get("enabled", False)
    notify_enabled = args.notify if args.notify is not None else config.get("notify", {}).get("enabled", False)
    db_enabled = args.db if args.db is not None else config.get("database", {}).get("enabled", False)
    plots_enabled = args.plots if args.plots is not None else config.get("plots", {}).get("enabled", False)

    logger.info(
        "Execution flags: decode=%s db=%s plots=%s copy=%s notify=%s",
        decode_enabled, db_enabled, plots_enabled, copy_enabled, notify_enabled,
    )

    # Process passes
    logger.info("Processing %d pass(es)", len(passes_to_process))

    failed_count = 0
    for pass_id, pass_dir in passes_to_process:
        # Attach per-pass file logger so all output for this pass lands in
        # <pass_dir>/post_processing.log alongside the data.
        with _pass_log_file(pass_dir):
            logger.info("─" * 60)
            logger.info("Processing: %s", pass_id)

            reception_payload = load_pass_file(pass_dir)
            if not reception_payload:
                logger.error("Failed to load reception data for %s", pass_id)
                failed_count += 1
                continue

            # Execute enabled steps in correct order:
            # 1. Decode (CADU -> images via SatDump offline pipeline)
            # 2. DB Import (populate database with samples)
            # 3. Plots (generate plots from database data)
            # 4. Copy (copy everything — including decoded images and plots — to remote)
            # 5. Notify (send notification with link to copied files)
            copy_ok = False
            target = None
            link = None

            if decode_enabled:
                logger.info("Step: Decode")
                decode_pass(
                    config,
                    pass_id,
                    pass_dir,
                    reception_payload,
                    force=args.force_decode,
                )

            if db_enabled:
                logger.info("Step: DB Import")
                import_to_db(config, pass_id)

            if plots_enabled:
                logger.info("Step: Plots")
                generate_plots(config, pass_id)

            if copy_enabled:
                logger.info("Step: Copy")
                copy_ok, target, link = copy_output(config, pass_id, pass_dir)

            if notify_enabled:
                logger.info("Step: Notify")
                send_notification(
                    config, reception_payload, copy_ok, target, link,
                    copy_was_requested=copy_enabled,
                )

            logger.info("Pass %s completed", pass_id)

    logger.info("─" * 60)
    if failed_count > 0:
        logger.error("Completed with %d failure(s)", failed_count)
        return 1

    logger.info("All passes processed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
