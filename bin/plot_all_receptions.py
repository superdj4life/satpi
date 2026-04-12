#!/usr/bin/env python3
# satpi
# Draw a combined "super satplot" from all recorded pass samples in SQLite.
# It overlays many passes into one skyplot and colors segments by sync state.
# Author: Andreas Horvath
# Project: Autonomous, Config-driven satellite reception pipeline for Raspberry Pi

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from load_config import load_config, ConfigError

def parse_args():
    parser = argparse.ArgumentParser(description="Create a combined skyplot from satpi SQLite data")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.ini (default: ../config/config.ini relative to this script)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (default: paths.reception_db_file from config.ini)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output PNG path (default: paths.base_dir/results/reports/plot_all_receptions.png)",
    )
    parser.add_argument(
        "--satellite",
        default=None,
        help="Optional satellite filter, e.g. 'METEOR-M2 4'",
    )
    parser.add_argument(
        "--pipeline",
        default=None,
        help="Optional pipeline filter, e.g. 'meteor_m2-x_lrpt'",
    )
    parser.add_argument(
        "--only-visible",
        action="store_true",
        help="Only plot samples with elevation >= 0",
    )
    parser.add_argument(
        "--synced-only",
        action="store_true",
        help="Only plot samples with deframer_state=SYNCED",
    )
    parser.add_argument(
        "--include-syncing",
        action="store_true",
        help="When used with --synced-only, also include Viterbi-synced / deframer-not-synced samples",
    )
    return parser.parse_args()


def derive_sync_state(viterbi_state: str | None, deframer_state: str | None) -> str:
    if deframer_state == "SYNCED":
        return "SYNCED"
    if viterbi_state == "SYNCED":
        return "SYNCING"
    return "NOSYNC"


def state_color(state: str) -> str:
    if state == "SYNCED":
        return "green"
    if state == "SYNCING":
        return "gold"
    return "red"


def angular_delta_deg(a1: float, a2: float) -> float:
    diff = abs(a2 - a1)
    if diff > 180:
        diff = 360 - diff
    return diff


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_samples(db_path: str, satellite: str | None, pipeline: str | None):
    conn = open_db(db_path)
    try:
        sql = """
        SELECT
            h.pass_id,
            h.satellite,
            h.pipeline,
            d.timestamp,
            d.snr_db,
            d.peak_snr_db,
            d.ber,
            d.viterbi_state,
            d.deframer_state,
            d.azimuth_deg,
            d.elevation_deg
        FROM pass_detail d
        JOIN pass_header h ON h.pass_id = d.pass_id
        WHERE 1=1
        """
        params = []

        if satellite:
            sql += " AND h.satellite = ?"
            params.append(satellite)

        if pipeline:
            sql += " AND h.pipeline = ?"
            params.append(pipeline)

        sql += " ORDER BY h.pass_id, d.timestamp"

        rows = conn.execute(sql, params).fetchall()
        return rows
    finally:
        conn.close()


def build_pass_map(rows, only_visible: bool, synced_only: bool, include_syncing: bool):
    passes = defaultdict(list)

    for row in rows:
        az = row["azimuth_deg"]
        el = row["elevation_deg"]

        if az is None or el is None:
            continue

        az = float(az)
        el = float(el)

        if only_visible and el < 0.0:
            continue

        sync_state = derive_sync_state(row["viterbi_state"], row["deframer_state"])

        if synced_only:
            if include_syncing:
                if sync_state not in ("SYNCED", "SYNCING"):
                    continue
            else:
                if sync_state != "SYNCED":
                    continue

        passes[row["pass_id"]].append(
            {
                "timestamp": row["timestamp"],
                "satellite": row["satellite"],
                "pipeline": row["pipeline"],
                "azimuth_deg": az,
                "elevation_deg": el,
                "snr_db": float(row["snr_db"]) if row["snr_db"] is not None else None,
                "peak_snr_db": float(row["peak_snr_db"]) if row["peak_snr_db"] is not None else None,
                "ber": float(row["ber"]) if row["ber"] is not None else None,
                "sync_state": sync_state,
            }
        )

    return passes


def ensure_parent_dir(path: str):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def draw_super_satplot(pass_map, output_path: str, title: str):
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(12, 9))
    ax = fig.add_axes([0.06, 0.08, 0.62, 0.84], projection="polar")

    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_rlim(0, 90)
    ax.set_rticks([0, 10, 20, 30, 40, 50, 60, 70, 80, 90])
    ax.set_yticklabels(["90", "80", "70", "60", "50", "40", "30", "20", "10", "0"])
    ax.set_rlabel_position(225)
    ax.set_title(title, va="bottom")

    total_segments = 0
    pass_count = 0

    for pass_id, samples in sorted(pass_map.items()):
        if len(samples) < 2:
            continue

        pass_count += 1

        for i in range(len(samples) - 1):
            s1 = samples[i]
            s2 = samples[i + 1]

            az1 = s1["azimuth_deg"]
            az2 = s2["azimuth_deg"]
            el1 = s1["elevation_deg"]
            el2 = s2["elevation_deg"]

            az_diff = angular_delta_deg(az1, az2)
            if az_diff > 60:
                continue

            theta = [math.radians(az1), math.radians(az2)]
            radius = [90.0 - el1, 90.0 - el2]

            color = state_color(s1["sync_state"])
            linewidth = 1.2 if s1["sync_state"] == "NOSYNC" else 2.0
            alpha = 0.35 if s1["sync_state"] == "NOSYNC" else 0.8

            ax.plot(theta, radius, color=color, linewidth=linewidth, alpha=alpha)
            total_segments += 1

        visible_samples = [s for s in samples if s["elevation_deg"] >= 0.0]
        if len(visible_samples) >= 3:
            start = visible_samples[0]
            next_point = visible_samples[2]

            start_theta = math.radians(start["azimuth_deg"])
            start_radius = 90.0 - start["elevation_deg"]
            next_theta = math.radians(next_point["azimuth_deg"])
            next_radius = 90.0 - next_point["elevation_deg"]

            ax.annotate(
                "",
                xy=(next_theta, next_radius),
                xytext=(start_theta, start_radius),
                arrowprops=dict(
                    arrowstyle="simple",
                    fc="blue",
                    ec="blue",
                    lw=0.0,
                    alpha=0.95,
                    shrinkA=0,
                    shrinkB=0,
                    mutation_scale=14,
                ),
                zorder=80,
            )

    legend_handles = [
        plt.Line2D([0], [0], color="red", lw=3, label="NOSYNC"),
        plt.Line2D([0], [0], color="gold", lw=3, label="SYNCING"),
        plt.Line2D([0], [0], color="green", lw=3, label="SYNCED"),
        plt.Line2D([0], [0], color="blue", lw=2, label="Start direction"),
    ]

    fig.legend(
        handles=legend_handles,
        loc="lower left",
        bbox_to_anchor=(0.72, 0.20),
        fontsize=8,
        frameon=True,
    )

    info_lines = [
        f"Passes plotted: {pass_count}",
        f"Segments plotted: {total_segments}",
    ]

    fig.text(
        0.72,
        0.82,
        "\n".join(info_lines),
        va="top",
        ha="left",
        fontsize=9,
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.9),
    )

    ensure_parent_dir(output_path)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

def get_config_path(cli_value: str | None) -> str:
    if cli_value:
        return os.path.abspath(cli_value)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "config", "config.ini")

def main():
    args = parse_args()

    config_path = get_config_path(args.config)

    try:
        config = load_config(config_path)
    except ConfigError as e:
        raise SystemExit(f"CONFIG ERROR: {e}")

    db_path = args.db or config["paths"]["reception_db_file"]
    output_path = args.output or os.path.join(
        config["paths"]["base_dir"],
        "results",
        "reports",
        "plot_all_receptions.png",
    )

    rows = load_samples(db_path, args.satellite, args.pipeline)
    if not rows:
        raise SystemExit("No matching rows found in database")

    pass_map = build_pass_map(
        rows,
        only_visible=args.only_visible,
        synced_only=args.synced_only,
        include_syncing=args.include_syncing,
    )

    usable_passes = {k: v for k, v in pass_map.items() if len(v) >= 2}
    if not usable_passes:
        raise SystemExit("No usable passes after filtering")

    title_parts = ["satpi All Receptions Plot"]
    if args.satellite:
        title_parts.append(args.satellite)
    if args.pipeline:
        title_parts.append(args.pipeline)
    if args.synced_only:
        title_parts.append("SYNC FILTERED")
    if args.only_visible:
        title_parts.append("VISIBLE ONLY")

    draw_super_satplot(
        usable_passes,
        output_path,
        " | ".join(title_parts),
    )

    print(f"Created: {output_path}")

if __name__ == "__main__":
    main()
