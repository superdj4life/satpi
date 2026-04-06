#!/usr/bin/env python3
# satpi
# Renders graphical views from structured reception JSON data.
# Creates:
# - skyplot of the pass path across the sky
# - time series plot for SNR and BER with sync-state background
# Author: Andreas Horvath
# Project: Autonomous, Config-driven satellite reception pipeline for Raspberry Pi

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def parse_args():
    parser = argparse.ArgumentParser(description="Render reception plots from satpi reception JSON")
    parser.add_argument("input_json", help="Path to <pass_id>-reception.json")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for generated PNG files (default: same directory as input JSON)",
    )
    return parser.parse_args()


def load_reception_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def derive_sync_state(viterbi_state: str, deframer_state: str) -> str:
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


def prepare_samples(samples: list[dict]) -> list[dict]:
    prepared = []

    for sample in samples:
        ts = parse_utc(sample["timestamp"])
        viterbi_state = sample.get("viterbi_state", "NOSYNC")
        deframer_state = sample.get("deframer_state", "NOSYNC")
        sync_state = derive_sync_state(viterbi_state, deframer_state)

        prepared.append(
            {
                "timestamp": ts,
                "snr_db": float(sample["snr_db"]),
                "peak_snr_db": float(sample["peak_snr_db"]),
                "ber": float(sample["ber"]),
                "viterbi_state": viterbi_state,
                "deframer_state": deframer_state,
                "sync_state": sync_state,
                "azimuth_deg": float(sample["azimuth_deg"]),
                "elevation_deg": float(sample["elevation_deg"]),
            }
        )

    prepared.sort(key=lambda x: x["timestamp"])
    return prepared


def plot_skyplot(data: dict, samples: list[dict], output_path: str):
    if len(samples) < 2:
        raise ValueError("Need at least 2 samples for skyplot")

    fig = plt.figure(figsize=(9, 9))
    ax = fig.add_subplot(111, projection="polar")

    # Skyplot convention:
    # - North at top
    # - clockwise azimuth
    # - zenith in center
    # - horizon at outer ring
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_rlim(90, 0)
    ax.set_rlabel_position(225)

    for i in range(len(samples) - 1):
        s1 = samples[i]
        s2 = samples[i + 1]

        theta = [math.radians(s1["azimuth_deg"]), math.radians(s2["azimuth_deg"])]
        radius = [90.0 - s1["elevation_deg"], 90.0 - s2["elevation_deg"]]

        color = state_color(s1["sync_state"])
        ax.plot(theta, radius, linewidth=2.5, color=color)

    # mark start and end
    start = samples[0]
    end = samples[-1]
    ax.scatter(
        [math.radians(start["azimuth_deg"])],
        [90.0 - start["elevation_deg"]],
        marker="o",
        s=60,
        color="blue",
        label="Start",
    )
    ax.scatter(
        [math.radians(end["azimuth_deg"])],
        [90.0 - end["elevation_deg"]],
        marker="x",
        s=80,
        color="black",
        label="End",
    )

    title = f"{data['pass_id']} Skyplot"
    ax.set_title(title, va="bottom")

    metadata_text = build_metadata_text(data)

    ax.text(
        1.05,
        0.95,
        metadata_text,
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.85),
    )

    legend_handles = [
        plt.Line2D([0], [0], color="red", lw=3, label="NOSYNC"),
        plt.Line2D([0], [0], color="gold", lw=3, label="SYNCING"),
        plt.Line2D([0], [0], color="green", lw=3, label="SYNCED"),
        plt.Line2D([0], [0], marker="o", color="blue", lw=0, label="Start"),
        plt.Line2D([0], [0], marker="x", color="black", lw=0, label="End"),
    ]
    ax.legend(handles=legend_handles, loc="lower left", bbox_to_anchor=(1.05, 0.05))

    fig.tight_layout(rect=[0, 0, 0.82, 1])
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def merge_segments_by_state(samples: list[dict]) -> list[tuple[datetime, datetime, str]]:
    if not samples:
        return []

    segments = []
    seg_start = samples[0]["timestamp"]
    current_state = samples[0]["sync_state"]

    for i in range(1, len(samples)):
        state = samples[i]["sync_state"]
        if state != current_state:
            segments.append((seg_start, samples[i]["timestamp"], current_state))
            seg_start = samples[i]["timestamp"]
            current_state = state

    segments.append((seg_start, samples[-1]["timestamp"], current_state))
    return segments


def plot_timeseries(data: dict, samples: list[dict], output_path: str):
    if not samples:
        raise ValueError("No samples available")

    times = [s["timestamp"] for s in samples]
    snr = [s["snr_db"] for s in samples]
    ber = [s["ber"] for s in samples]

    fig, ax1 = plt.subplots(figsize=(12, 6))

    # background sync-state bands
    for start, end, state in merge_segments_by_state(samples):
        ax1.axvspan(start, end, alpha=0.15, color=state_color(state))

    ax1.plot(times, snr, linewidth=1.8, label="SNR (dB)")
    ax1.set_ylabel("SNR (dB)")
    ax1.set_xlabel("Time (UTC)")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    ax2 = ax1.twinx()
    ax2.plot(times, ber, linewidth=1.2, linestyle="--", label="BER")
    ax2.set_ylabel("BER")

    title = f"{data['pass_id']} Reception Time Series"
    ax1.set_title(title)

    metadata_text = build_metadata_text(data)

    ax1.text(
        1.02,
        0.98,
        metadata_text,
        transform=ax1.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.85),
    )

    handles = [
        plt.Line2D([0], [0], color="red", lw=6, alpha=0.3, label="NOSYNC"),
        plt.Line2D([0], [0], color="gold", lw=6, alpha=0.3, label="SYNCING"),
        plt.Line2D([0], [0], color="green", lw=6, alpha=0.3, label="SYNCED"),
        plt.Line2D([0], [0], lw=1.8, label="SNR (dB)"),
        plt.Line2D([0], [0], lw=1.2, linestyle="--", label="BER"),
    ]
    ax1.legend(handles=handles, loc="upper left")

    fig.autofmt_xdate()
    fig.tight_layout(rect=[0, 0, 0.82, 1])
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

def build_metadata_text(data: dict) -> str:
    lines = [
        f"Satellite: {data.get('satellite', '-')}",
        f"Pipeline: {data.get('pipeline', '-')}",
        f"Frequency: {data.get('frequency_hz', '-')} Hz",
        f"Bandwidth: {data.get('bandwidth_hz', '-')} Hz",
        f"Gain: {data.get('gain', '-')}",
        f"Source ID: {data.get('source_id', '-')}",
        f"Bias-T: {data.get('bias_t', '-')}",
        f"Pass start: {data.get('pass_start', '-')}",
        f"Pass end: {data.get('pass_end', '-')}",
    ]
    return "\n".join(lines)

def main():
    args = parse_args()

    input_json = os.path.abspath(args.input_json)
    data = load_reception_json(input_json)
    samples = prepare_samples(data.get("samples", []))

    if not samples:
        raise SystemExit("No samples found in JSON")

    if args.output_dir:
        output_dir = os.path.abspath(args.output_dir)
    else:
        output_dir = os.path.dirname(input_json)

    os.makedirs(output_dir, exist_ok=True)

    pass_id = data["pass_id"]
    skyplot_path = os.path.join(output_dir, f"{pass_id}-skyplot.png")
    timeseries_path = os.path.join(output_dir, f"{pass_id}-timeseries.png")

    plot_skyplot(data, samples, skyplot_path)
    plot_timeseries(data, samples, timeseries_path)

    print(f"Created: {skyplot_path}")
    print(f"Created: {timeseries_path}")


if __name__ == "__main__":
    raise SystemExit(main())
