#!/usr/bin/env python3

import json
from pathlib import Path
from PIL import Image, ImageStat

CAPTURES_DIR = Path.home() / "satpi" / "results" / "captures"


def analyze_image(path: Path):
    img = Image.open(path).convert("L")
    stat = ImageStat.Stat(img)
    min_v, max_v = img.getextrema()
    return {
        "file": str(path),
        "mean": round(stat.mean[0], 3),
        "stddev": round(stat.stddev[0], 3),
        "range": int(max_v - min_v),
        "min": int(min_v),
        "max": int(max_v),
        "size": img.size,
    }


def classify_channel(ch):
    if ch["stddev"] < 5 or ch["range"] < 30:
        return "bad"
    if ch["stddev"] < 20 or ch["range"] < 80:
        return "medium"
    return "good"


def classify_pass(channels):
    stddevs = [c["stddev"] for c in channels]
    ranges = [c["range"] for c in channels]
    means = [c["mean"] for c in channels]

    avg_stddev = sum(stddevs) / len(stddevs)
    avg_range = sum(ranges) / len(ranges)
    mean_spread = max(means) - min(means)

    good_count = sum(1 for c in channels if classify_channel(c) == "good")
    bad_count = sum(1 for c in channels if classify_channel(c) == "bad")

    if avg_stddev < 5 and avg_range < 30:
        verdict = "flat"
    elif bad_count >= 2 and avg_stddev < 10:
        verdict = "mostly_flat"
    elif good_count >= 2:
        verdict = "usable"
    else:
        verdict = "mixed"

    return {
        "verdict": verdict,
        "avg_stddev": round(avg_stddev, 3),
        "avg_range": round(avg_range, 3),
        "mean_spread": round(mean_spread, 3),
        "good_channels": good_count,
        "bad_channels": bad_count,
    }


def main():
    rows = []

    for pass_dir in sorted(CAPTURES_DIR.iterdir()):
        if not pass_dir.is_dir():
            continue
        if pass_dir.name == "decode_report":
            continue

        msu_dir = pass_dir / "MSU-MR"
        if not msu_dir.exists():
            continue

        files = [
            msu_dir / "MSU-MR-1.png",
            msu_dir / "MSU-MR-2.png",
            msu_dir / "MSU-MR-3.png",
        ]
        if not all(f.exists() for f in files):
            continue

        channels = [analyze_image(f) for f in files]
        summary = classify_pass(channels)

        rows.append({
            "pass_dir": pass_dir.name,
            "summary": summary,
            "channels": channels,
        })

    rows.sort(key=lambda r: r["pass_dir"])

    print(f"{'pass_dir':40} {'verdict':12} {'avg_stddev':>10} {'avg_range':>10}")
    print("-" * 78)
    for row in rows:
        s = row["summary"]
        print(
            f"{row['pass_dir'][:40]:40} "
            f"{s['verdict']:12} "
            f"{s['avg_stddev']:10.3f} "
            f"{s['avg_range']:10.3f}"
        )

    out = CAPTURES_DIR / "flat_pass_report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
        f.write("\n")

    print()
    print(f"JSON written to: {out}")


if __name__ == "__main__":
    main()
