#!/usr/bin/env python3
# satpi
# Publishes satpi state and events to Home Assistant via MQTT discovery.
# Registers sensors, binary sensors, and buttons automatically so that
# HA picks them up without any manual entity configuration.
# Call this script with a subcommand to publish specific events:
#   scheduled  -- publish the upcoming pass schedule
#   pass_start -- mark a pass as in-progress
#   pass_done  -- mark a pass as complete with result details
#   status     -- publish online/offline LWT status manually
# Author: Andreas Horvath
# Project: Autonomous, Config-driven satellite reception pipeline for Raspberry Pi

import argparse
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from load_config import load_config, ConfigError

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("ERROR: paho-mqtt is required. Install with: pip3 install paho-mqtt")
    sys.exit(1)

logger = logging.getLogger("satpi.ha_notify")

# ==============================
# MQTT TOPIC HELPERS
# ==============================

def _base_topic(cfg: dict) -> str:
    return cfg["ha_mqtt"]["base_topic"].rstrip("/")


def _discovery_prefix(cfg: dict) -> str:
    return cfg["ha_mqtt"]["discovery_prefix"].rstrip("/")


def _device_id(cfg: dict) -> str:
    return cfg["ha_mqtt"]["device_id"]


def _topic(cfg: dict, suffix: str) -> str:
    return f"{_base_topic(cfg)}/{suffix}"


def _discovery_topic(cfg: dict, component: str, object_id: str) -> str:
    return f"{_discovery_prefix(cfg)}/{component}/{_device_id(cfg)}/{object_id}/config"


def _lwt_topic(cfg: dict) -> str:
    return _topic(cfg, "status")


# ==============================
# MQTT CONNECTION
# ==============================

def build_client(cfg: dict) -> mqtt.Client:
    ha_cfg = cfg["ha_mqtt"]
    client_id = f"satpi-{_device_id(cfg)}-{os.getpid()}"

    client = mqtt.Client(client_id=client_id)

    username = ha_cfg.get("username", "").strip()
    password = ha_cfg.get("password", "").strip()
    if username:
        client.username_pw_set(username, password or None)

    # Last Will and Testament — broker publishes this if we disconnect unexpectedly
    client.will_set(
        topic=_lwt_topic(cfg),
        payload="offline",
        qos=1,
        retain=True,
    )

    tls = ha_cfg.get("tls", False)
    if tls:
        client.tls_set()

    return client


def connect(client: mqtt.Client, cfg: dict) -> None:
    ha_cfg = cfg["ha_mqtt"]
    host = ha_cfg["host"]
    port = ha_cfg["port"]
    keepalive = ha_cfg.get("keepalive", 60)

    logger.info("Connecting to MQTT broker %s:%d", host, port)
    client.connect(host, port, keepalive=keepalive)
    client.loop_start()


def disconnect(client: mqtt.Client, cfg: dict) -> None:
    # Publish clean online status before we go, then disconnect
    publish(client, cfg, _lwt_topic(cfg), "offline", retain=True)
    time.sleep(0.3)
    client.loop_stop()
    client.disconnect()
    logger.info("Disconnected from MQTT broker")


def publish(client: mqtt.Client, cfg: dict, topic: str, payload, retain: bool = False) -> None:
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)

    result = client.publish(topic, payload=payload, qos=1, retain=retain)
    result.wait_for_publish(timeout=5)
    logger.debug("Published to %s (retain=%s): %s", topic, retain, payload[:120])


# ==============================
# DEVICE PAYLOAD (shared across all discovery messages)
# ==============================

def _device_payload(cfg: dict) -> dict:
    ha_cfg = cfg["ha_mqtt"]
    hostname = socket.gethostname()
    return {
        "identifiers": [_device_id(cfg)],
        "name": ha_cfg.get("device_name", "satpi"),
        "model": "satpi satellite receiver",
        "manufacturer": "satpi",
        "sw_version": "1.3.0",
        "configuration_url": f"http://{hostname}.local",
    }


# ==============================
# DISCOVERY REGISTRATION
# ==============================

def register_all(client: mqtt.Client, cfg: dict) -> None:
    """Publish all Home Assistant MQTT discovery config messages."""
    _register_status_sensor(client, cfg)
    _register_active_pass_sensor(client, cfg)
    _register_next_pass_sensor(client, cfg)
    _register_schedule_sensor(client, cfg)
    _register_last_pass_sensor(client, cfg)
    _register_last_skyplot_sensor(client, cfg)
    _register_pass_active_binary(client, cfg)
    logger.info("All discovery messages published")


def _register_pass_alarm(client: mqtt.Client, cfg: dict, slot: int, satellite: str, pass_start_utc: str) -> None:
    """Register a timestamp sensor for a single upcoming pass slot (used as an HA alarm trigger)."""
    object_id = f"pass_alarm_{slot:02d}"
    topic = _discovery_topic(cfg, "sensor", object_id)
    state_topic = _topic(cfg, f"pass_alarms/{object_id}/state")
    payload = _discovery_config(cfg, "sensor", object_id, {
        "name": f"satpi Pass Alarm {slot:02d}",
        "state_topic": state_topic,
        "device_class": "timestamp",
        "icon": "mdi:alarm",
        "json_attributes_topic": _topic(cfg, f"pass_alarms/{object_id}/attributes"),
    })
    publish(client, cfg, topic, payload, retain=True)
    publish(client, cfg, state_topic, pass_start_utc, retain=True)
    publish(
        client, cfg,
        _topic(cfg, f"pass_alarms/{object_id}/attributes"),
        {"satellite": satellite, "slot": slot},
        retain=True,
    )


def _discovery_config(cfg: dict, component: str, object_id: str, payload: dict) -> None:
    # Inject shared device block and unique_id into every discovery message
    payload["device"] = _device_payload(cfg)
    payload["unique_id"] = f"{_device_id(cfg)}_{object_id}"
    payload["availability_topic"] = _lwt_topic(cfg)
    payload["payload_available"] = "online"
    payload["payload_not_available"] = "offline"
    return payload


def _register_status_sensor(client: mqtt.Client, cfg: dict) -> None:
    topic = _discovery_topic(cfg, "sensor", "status")
    state_topic = _topic(cfg, "status")
    payload = _discovery_config(cfg, "sensor", "status", {
        "name": "satpi Status",
        "state_topic": state_topic,
        "icon": "mdi:satellite-uplink",
    })
    publish(client, cfg, topic, payload, retain=True)


def _register_active_pass_sensor(client: mqtt.Client, cfg: dict) -> None:
    topic = _discovery_topic(cfg, "sensor", "active_pass")
    state_topic = _topic(cfg, "active_pass/state")
    payload = _discovery_config(cfg, "sensor", "active_pass", {
        "name": "satpi Active Pass",
        "state_topic": state_topic,
        "icon": "mdi:satellite-variant",
        "json_attributes_topic": _topic(cfg, "active_pass/attributes"),
    })
    publish(client, cfg, topic, payload, retain=True)


def _register_next_pass_sensor(client: mqtt.Client, cfg: dict) -> None:
    topic = _discovery_topic(cfg, "sensor", "next_pass")
    state_topic = _topic(cfg, "next_pass/state")
    payload = _discovery_config(cfg, "sensor", "next_pass", {
        "name": "satpi Next Pass",
        "state_topic": state_topic,
        "icon": "mdi:clock-start",
        "json_attributes_topic": _topic(cfg, "next_pass/attributes"),
    })
    publish(client, cfg, topic, payload, retain=True)


def _register_schedule_sensor(client: mqtt.Client, cfg: dict) -> None:
    topic = _discovery_topic(cfg, "sensor", "schedule")
    state_topic = _topic(cfg, "schedule/state")
    payload = _discovery_config(cfg, "sensor", "schedule", {
        "name": "satpi Schedule",
        "state_topic": state_topic,
        "icon": "mdi:calendar-clock",
        "json_attributes_topic": _topic(cfg, "schedule/attributes"),
    })
    publish(client, cfg, topic, payload, retain=True)


def _register_last_pass_sensor(client: mqtt.Client, cfg: dict) -> None:
    topic = _discovery_topic(cfg, "sensor", "last_pass")
    state_topic = _topic(cfg, "last_pass/state")
    payload = _discovery_config(cfg, "sensor", "last_pass", {
        "name": "satpi Last Pass",
        "state_topic": state_topic,
        "icon": "mdi:history",
        "json_attributes_topic": _topic(cfg, "last_pass/attributes"),
    })
    publish(client, cfg, topic, payload, retain=True)


def _register_last_skyplot_sensor(client: mqtt.Client, cfg: dict) -> None:
    topic = _discovery_topic(cfg, "sensor", "last_skyplot")
    state_topic = _topic(cfg, "last_skyplot/state")
    payload = _discovery_config(cfg, "sensor", "last_skyplot", {
        "name": "satpi Last Skyplot",
        "state_topic": state_topic,
        "icon": "mdi:image",
    })
    publish(client, cfg, topic, payload, retain=True)


def _register_pass_active_binary(client: mqtt.Client, cfg: dict) -> None:
    topic = _discovery_topic(cfg, "binary_sensor", "pass_active")
    state_topic = _topic(cfg, "pass_active/state")
    payload = _discovery_config(cfg, "binary_sensor", "pass_active", {
        "name": "satpi Pass Active",
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "running",
        "icon": "mdi:broadcast",
    })
    publish(client, cfg, topic, payload, retain=True)


# ==============================
# SKYPLOT PATH RESOLUTION
# ==============================

def _smb_skyplot_path(cfg: dict, pass_id: str) -> str | None:
    """
    Build a UNC path to the skyplot PNG on the SMB share.
    The share is \\<hostname>\skyplots and the file lives inside
    the pass subdirectory: \\<hostname>\skyplots\<pass_id>\skyplot_<pass_id>.png
    """
    ha_cfg = cfg["ha_mqtt"]
    smb_host = ha_cfg.get("smb_host", "").strip()
    smb_share = ha_cfg.get("smb_skyplots_share", "skyplots").strip()

    if not smb_host:
        return None

    return f"\\\\{smb_host}\\{smb_share}\\{pass_id}\\skyplot_{pass_id}.png"


def _find_latest_skyplot(cfg: dict) -> tuple[str | None, str | None]:
    """
    Return (pass_id, smb_path) for the most recently modified skyplot PNG
    found under the captures output directory.
    """
    output_dir = cfg["paths"]["output_dir"]

    candidates = sorted(
        Path(output_dir).rglob("*-skyplot.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        return None, None

    latest = candidates[0]
    # Directory name is the pass_id
    pass_id = latest.parent.name
    smb_path = _smb_skyplot_path(cfg, pass_id)
    return pass_id, smb_path


# ==============================
# SUBCOMMAND: register
# ==============================

def cmd_register(client: mqtt.Client, cfg: dict, _args) -> None:
    register_all(client, cfg)
    publish(client, cfg, _lwt_topic(cfg), "online", retain=True)


# ==============================
# SUBCOMMAND: scheduled
# ==============================

def cmd_scheduled(client: mqtt.Client, cfg: dict, args) -> None:
    """Publish the upcoming pass schedule from the passes JSON file."""
    pass_file = cfg["paths"]["pass_file"]

    if not os.path.exists(pass_file):
        logger.warning("Pass file not found: %s", pass_file)
        return

    with open(pass_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    passes = data.get("passes", [])
    tz_name = cfg["station"]["timezone"]
    tz = ZoneInfo(tz_name)
    now_utc = datetime.now(timezone.utc)

    # Build list of upcoming passes in local time
    upcoming = []
    for p in passes:
        start_utc = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
        if start_utc <= now_utc:
            continue

        end_utc = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
        start_local = start_utc.astimezone(tz)
        end_local = end_utc.astimezone(tz)

        upcoming.append({
            "satellite": p["satellite"],
            "start": start_local.strftime("%Y-%m-%d %H:%M %Z"),
            "end": end_local.strftime("%H:%M %Z"),
            "max_elevation_deg": round(p.get("max_elevation", 0), 1),
            "aos_azimuth_deg": round(p.get("aos_azimuth_deg", 0), 1),
            "los_azimuth_deg": round(p.get("los_azimuth_deg", 0), 1),
            "direction": p.get("direction", ""),
        })

    # Next pass state and attributes
    if upcoming:
        nxt = upcoming[0]
        next_state = f"{nxt['satellite']} @ {nxt['start']}"
        next_attrs = nxt
    else:
        next_state = "none"
        next_attrs = {}

    publish(client, cfg, _topic(cfg, "next_pass/state"), next_state, retain=True)
    publish(client, cfg, _topic(cfg, "next_pass/attributes"), next_attrs, retain=True)

    # Full schedule as attribute list on the schedule sensor
    schedule_state = f"{len(upcoming)} upcoming"
    schedule_attrs = {"passes": upcoming[:args.max_passes]}
    publish(client, cfg, _topic(cfg, "schedule/state"), schedule_state, retain=True)
    publish(client, cfg, _topic(cfg, "schedule/attributes"), schedule_attrs, retain=True)

    # Publish one timestamp alarm sensor per upcoming pass (for HA automations)
    future_passes = [p for p in passes if datetime.fromisoformat(p["start"].replace("Z", "+00:00")) > now_utc]
    for slot, p in enumerate(future_passes[:args.max_passes]):
        _register_pass_alarm(client, cfg, slot, p["satellite"], p["start"])

    logger.info("Published schedule: %d upcoming passes", len(upcoming))


# ==============================
# SUBCOMMAND: pass_start
# ==============================

def cmd_pass_start(client: mqtt.Client, cfg: dict, args) -> None:
    """Mark a pass as active. Called at the start of receive_pass.py."""
    tz = ZoneInfo(cfg["station"]["timezone"])
    now_local = datetime.now(tz)

    state = f"Receiving {args.satellite}"
    attrs = {
        "satellite": args.satellite,
        "pass_start": args.pass_start or now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "pass_end": args.pass_end or "",
        "pass_id": args.pass_id or "",
    }

    publish(client, cfg, _topic(cfg, "active_pass/state"), state, retain=True)
    publish(client, cfg, _topic(cfg, "active_pass/attributes"), attrs, retain=True)
    publish(client, cfg, _topic(cfg, "pass_active/state"), "ON", retain=True)

    logger.info("Published pass_start for %s", args.satellite)


# ==============================
# SUBCOMMAND: pass_done
# ==============================

def cmd_pass_done(client: mqtt.Client, cfg: dict, args) -> None:
    """Mark a pass as complete and publish result details."""
    tz = ZoneInfo(cfg["station"]["timezone"])
    now_local = datetime.now(tz)
    pass_id = args.pass_id or ""

    # Resolve skyplot path
    if pass_id:
        smb_path = _smb_skyplot_path(cfg, pass_id)
    else:
        _, smb_path = _find_latest_skyplot(cfg)

    outcome = "success" if args.success else "no_sync"

    last_state = f"{args.satellite} — {outcome}"
    last_attrs = {
        "satellite": args.satellite,
        "pass_id": pass_id,
        "completed_at": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "outcome": outcome,
        "decode_ok": args.success,
        "skyplot": smb_path or "",
    }

    publish(client, cfg, _topic(cfg, "last_pass/state"), last_state, retain=True)
    publish(client, cfg, _topic(cfg, "last_pass/attributes"), last_attrs, retain=True)

    if smb_path:
        publish(client, cfg, _topic(cfg, "last_skyplot/state"), smb_path, retain=True)

    # Clear the active-pass indicator
    publish(client, cfg, _topic(cfg, "active_pass/state"), "idle", retain=True)
    publish(client, cfg, _topic(cfg, "active_pass/attributes"), {}, retain=True)
    publish(client, cfg, _topic(cfg, "pass_active/state"), "OFF", retain=True)

    logger.info("Published pass_done for %s (outcome=%s)", args.satellite, outcome)


# ==============================
# SUBCOMMAND: status
# ==============================

def cmd_status(client: mqtt.Client, cfg: dict, args) -> None:
    """Publish online/offline status manually."""
    payload = args.state
    publish(client, cfg, _lwt_topic(cfg), payload, retain=True)
    logger.info("Published status: %s", payload)


# ==============================
# ARGUMENT PARSING
# ==============================

def parse_args():
    parser = argparse.ArgumentParser(
        description="satpi Home Assistant MQTT notification publisher"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.ini (default: ../config/config.ini relative to this script)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # register
    sub.add_parser("register", help="Publish HA MQTT discovery config for all entities")

    # scheduled
    p_sched = sub.add_parser("scheduled", help="Publish upcoming pass schedule")
    p_sched.add_argument(
        "--max-passes",
        type=int,
        default=10,
        help="Maximum number of upcoming passes to include in schedule payload",
    )

    # pass_start
    p_start = sub.add_parser("pass_start", help="Publish pass-start event")
    p_start.add_argument("--satellite", required=True)
    p_start.add_argument("--pass-start", default=None)
    p_start.add_argument("--pass-end", default=None)
    p_start.add_argument("--pass-id", default=None)

    # pass_done
    p_done = sub.add_parser("pass_done", help="Publish pass-complete event")
    p_done.add_argument("--satellite", required=True)
    p_done.add_argument("--pass-id", default=None)
    p_done.add_argument(
        "--success",
        action="store_true",
        default=False,
        help="Set if the pass decoded successfully",
    )

    # status
    p_status = sub.add_parser("status", help="Publish online or offline status")
    p_status.add_argument(
        "state",
        choices=["online", "offline"],
        help="Status to publish",
    )

    return parser.parse_args()


# ==============================
# CONFIG PATH
# ==============================

def get_config_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "config" / "config.ini"


# ==============================
# MAIN
# ==============================

SUBCOMMAND_MAP = {
    "register": cmd_register,
    "scheduled": cmd_scheduled,
    "pass_start": cmd_pass_start,
    "pass_done": cmd_pass_done,
    "status": cmd_status,
}


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = get_config_path(args.config)

    try:
        cfg = load_config(str(config_path))
    except ConfigError as e:
        logger.error("CONFIG ERROR: %s", e)
        return 1

    if not cfg["ha_mqtt"]["enabled"]:
        logger.info("ha_mqtt is disabled in config, nothing to do")
        return 0

    client = build_client(cfg)

    try:
        connect(client, cfg)
        # Give the broker a moment to settle the connection
        time.sleep(0.5)
        # Mark ourselves online after connecting
        publish(client, cfg, _lwt_topic(cfg), "online", retain=True)

        handler = SUBCOMMAND_MAP[args.subcommand]
        handler(client, cfg, args)

    except Exception as e:
        logger.error("Unexpected error: %s", e)
        return 1
    finally:
        disconnect(client, cfg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
