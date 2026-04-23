#!/usr/bin/env python3

import argparse
import json
import math
import os
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

from scipy.io import wavfile
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image as RLImage,
    Table,
    TableStyle,
    PageBreak,
)

DEFAULT_SAMPLE_RATE = 2400000
DEFAULT_FORMAT = "cs16"


def resolve_input(input_path: Path):
    if input_path.is_file():
        return input_path, input_path.parent

    matches = sorted(input_path.glob("pass_iq_baseband.*"))
    if matches:
        return matches[0], input_path

    wav_matches = sorted(input_path.glob("*.wav"))
    if wav_matches:
        return wav_matches[0], input_path

    return None, input_path

def load_cs16_iq(iq_path: Path):
    file_size = iq_path.stat().st_size
    if file_size < 4:
        raise ValueError("IQ file is too small")

    sample_pairs = file_size // 4
    raw = np.memmap(iq_path, dtype=np.int16, mode="r", shape=(sample_pairs * 2,))
    iq_i16 = raw.reshape(-1, 2)

    return iq_i16

def iq_i16_to_complex64(iq_i16: np.ndarray, scale: float = 32768.0):
    i = iq_i16[:, 0].astype(np.float32) / scale
    q = iq_i16[:, 1].astype(np.float32) / scale
    return i + 1j * q


def load_iq_window_cs16(iq_path: Path, max_complex_samples: int):
    iq_i16 = load_cs16_iq(iq_path)
    n_total = iq_i16.shape[0]
    n = min(n_total, max_complex_samples)
    window = iq_i16_to_complex64(iq_i16[:n])
    return window, n_total


def iter_iq_blocks_cs16(iq_path: Path, block_complex_samples: int = 1_000_000):
    iq_i16 = load_cs16_iq(iq_path)
    n_total = iq_i16.shape[0]

    for start in range(0, n_total, block_complex_samples):
        stop = min(start + block_complex_samples, n_total)
        yield start, stop, iq_i16_to_complex64(iq_i16[start:stop])

def load_wav_iq(iq_path: Path):
    sample_rate, data = wavfile.read(str(iq_path))

    if data.ndim != 2 or data.shape[1] != 2:
        raise ValueError(f"WAV IQ file must have exactly 2 channels, got shape {data.shape}")

    if data.dtype == np.float32:
        wav_encoding = "float32"
        arr = data.astype(np.float32, copy=False)
    elif data.dtype == np.int16:
        wav_encoding = "int16"
        arr = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        wav_encoding = "int32"
        arr = data.astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV dtype: {data.dtype}")

    i = arr[:, 0]
    q = arr[:, 1]
    iq = i + 1j * q

    return iq, int(sample_rate), {
        "channels": int(data.shape[1]),
        "dtype": str(data.dtype),
        "wav_encoding": wav_encoding,
    }

def load_iq_file(iq_path: Path, forced_format: str | None = None, forced_sample_rate: int | None = None):
    suffix = iq_path.suffix.lower()

    if suffix == ".wav":
        iq, sample_rate, meta = load_wav_iq(iq_path)
        return iq, sample_rate, "wav_iq", meta

    fmt = (forced_format or "").lower()
    if fmt == "cs16" or suffix == ".cs16":
        if forced_sample_rate is None:
            raise ValueError("sample rate must be provided for raw cs16 files")

        file_size = iq_path.stat().st_size
        complex_samples = file_size // 4

        return None, forced_sample_rate, "cs16", {
            "complex_samples": int(complex_samples),
            "file_size_bytes": int(file_size),
        }

    raise ValueError(f"Unsupported IQ file format: {iq_path}")

def compute_metrics(iq_source, sample_rate: int, detected_format: str, iq_path: Path | None = None):
    if detected_format == "wav_iq":
        iq = iq_source
        i = np.real(iq)
        q = np.imag(iq)
        mag = np.abs(iq)

        i_mean = float(np.mean(i))
        q_mean = float(np.mean(q))
        i_std = float(np.std(i))
        q_std = float(np.std(q))
        mag_mean = float(np.mean(mag))
        mag_std = float(np.std(mag))
        peak_abs = float(max(np.max(np.abs(i)), np.max(np.abs(q))))
        clipped_samples = 0
        clip_ratio = 0.0
        duration_s = float(len(iq) / sample_rate)
        dc_offset = float(np.abs(np.mean(iq)))
        iq_balance_ratio = float((np.std(i) + 1e-9) / (np.std(q) + 1e-9))
        sample_count = int(len(iq))

    elif detected_format == "cs16":
        if iq_path is None:
            raise ValueError("iq_path is required for cs16 metrics")

        iq_i16 = load_cs16_iq(iq_path)
        sample_count = int(iq_i16.shape[0])
        duration_s = float(sample_count / sample_rate)

        block_size = 1_000_000

        sum_i = 0.0
        sum_q = 0.0
        sum_i2 = 0.0
        sum_q2 = 0.0
        sum_mag = 0.0
        sum_mag2 = 0.0
        clipped_samples = 0
        peak_abs = 0.0

        for start in range(0, sample_count, block_size):
            stop = min(start + block_size, sample_count)
            block_i16 = iq_i16[start:stop]

            i = block_i16[:, 0].astype(np.float32) / 32768.0
            q = block_i16[:, 1].astype(np.float32) / 32768.0
            mag = np.sqrt(i * i + q * q)

            sum_i += float(np.sum(i))
            sum_q += float(np.sum(q))
            sum_i2 += float(np.sum(i * i))
            sum_q2 += float(np.sum(q * q))
            sum_mag += float(np.sum(mag))
            sum_mag2 += float(np.sum(mag * mag))

            peak_abs = max(
                peak_abs,
                float(np.max(np.abs(i))),
                float(np.max(np.abs(q))),
            )

            clipped_samples += int(np.sum(
                (np.abs(block_i16[:, 0]) >= 32767) |
                (np.abs(block_i16[:, 1]) >= 32767)
            ))

        n = float(sample_count)

        i_mean = sum_i / n
        q_mean = sum_q / n
        i_var = max(0.0, (sum_i2 / n) - (i_mean * i_mean))
        q_var = max(0.0, (sum_q2 / n) - (q_mean * q_mean))
        mag_mean = sum_mag / n
        mag_var = max(0.0, (sum_mag2 / n) - (mag_mean * mag_mean))

        i_std = math.sqrt(i_var)
        q_std = math.sqrt(q_var)
        mag_std = math.sqrt(mag_var)

        clip_ratio = float(clipped_samples / sample_count)
        dc_offset = math.sqrt(i_mean * i_mean + q_mean * q_mean)
        iq_balance_ratio = float((i_std + 1e-9) / (q_std + 1e-9))

    else:
        raise ValueError(f"Unsupported format for metrics: {detected_format}")

    score = 100.0
    findings = []

    if clip_ratio > 0.01:
        score -= 30
        findings.append("Relevant clipping detected.")
    elif clip_ratio > 0.001:
        score -= 15
        findings.append("Minor clipping detected.")
    else:
        findings.append("No significant clipping detected.")

    if dc_offset > 0.1:
        score -= 20
        findings.append("Strong DC offset detected.")
    elif dc_offset > 0.03:
        score -= 10
        findings.append("Moderate DC offset detected.")
    else:
        findings.append("DC offset is low.")

    if not (0.85 <= iq_balance_ratio <= 1.15):
        score -= 15
        findings.append("I/Q power balance is noticeably uneven.")
    else:
        findings.append("I/Q power balance looks reasonable.")

    if mag_std < 0.01:
        score -= 20
        findings.append("Signal magnitude varies very little; this can indicate a weak or uninformative signal.")
    else:
        findings.append("Signal magnitude shows meaningful variation.")

    score = max(0.0, min(100.0, round(score, 1)))

    if score >= 75:
        quality_class = "good"
    elif score >= 45:
        quality_class = "medium"
    else:
        quality_class = "bad"

    return {
        "sample_count": sample_count,
        "duration_s": round(duration_s, 3),
        "sample_rate": int(sample_rate),
        "i_mean": round(i_mean, 6),
        "q_mean": round(q_mean, 6),
        "i_std": round(i_std, 6),
        "q_std": round(q_std, 6),
        "mag_mean": round(mag_mean, 6),
        "mag_std": round(mag_std, 6),
        "peak_abs": round(peak_abs, 6),
        "dc_offset_abs": round(dc_offset, 6),
        "iq_balance_ratio": round(iq_balance_ratio, 6),
        "clipped_samples": clipped_samples,
        "clip_ratio": round(clip_ratio, 8),
        "quality_score": score,
        "quality_class": quality_class,
        "findings": findings,
    }

def load_analysis_iq(iq_path: Path, detected_format: str, max_complex_samples: int = 262144):
    if detected_format == "wav_iq":
        iq, _sample_rate, _meta = load_wav_iq(iq_path)
        n = min(len(iq), max_complex_samples)
        return iq[:n]

    if detected_format == "cs16":
        iq_window, _n_total = load_iq_window_cs16(iq_path, max_complex_samples)
        return iq_window

    raise ValueError(f"Unsupported format for analysis window: {detected_format}")

def estimate_spectrum(iq: np.ndarray, sample_rate: int):
    n = min(len(iq), 262144)
    if n < 4096:
        raise ValueError("IQ file too short for spectrum analysis")

    x = iq[:n]
    window = np.hanning(n)
    spec = np.fft.fftshift(np.fft.fft(x * window))
    power = 20 * np.log10(np.abs(spec) + 1e-12)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / sample_rate))

    peak_idx = int(np.argmax(power))
    peak_freq_hz = float(freqs[peak_idx])
    peak_power_db = float(power[peak_idx])

    return freqs, power, {
        "peak_idx": peak_idx,
        "peak_freq_hz": round(peak_freq_hz, 3),
        "peak_power_db": round(peak_power_db, 3),
    }

def smooth_power(power: np.ndarray, window_len: int = 301):
    if window_len < 3:
        return power.copy()

    if window_len % 2 == 0:
        window_len += 1

    kernel = np.ones(window_len, dtype=np.float64) / window_len
    return np.convolve(power, kernel, mode="same")

def estimate_signal_band(
    freqs: np.ndarray,
    power: np.ndarray,
    peak_idx: int,
    search_span_hz: float = 200000.0,
    edge_above_noise_db: float = 6.0,
):
    smoothed = smooth_power(power, window_len=301)

    peak_freq = float(freqs[peak_idx])

    search_mask = (freqs >= peak_freq - search_span_hz) & (freqs <= peak_freq + search_span_hz)
    search_indices = np.where(search_mask)[0]
    if len(search_indices) == 0:
        raise ValueError("No search region found for band estimation")

    local_smoothed = smoothed[search_indices]

    noise_floor_db = float(np.percentile(local_smoothed, 20))
    threshold_db = noise_floor_db + edge_above_noise_db

    above_mask = local_smoothed >= threshold_db
    if not np.any(above_mask):
        center_freq_hz = peak_freq
        return {
            "noise_floor_db": round(noise_floor_db, 3),
            "threshold_db": round(threshold_db, 3),
            "left_idx": int(peak_idx),
            "right_idx": int(peak_idx),
            "left_freq_hz": round(peak_freq, 3),
            "right_freq_hz": round(peak_freq, 3),
            "center_freq_hz": round(peak_freq, 3),
            "bandwidth_hz": 0.0,
        }

    segments = []
    in_segment = False
    seg_start = None

    for i, flag in enumerate(above_mask):
        if flag and not in_segment:
            seg_start = i
            in_segment = True
        elif not flag and in_segment:
            segments.append((seg_start, i - 1))
            in_segment = False

    if in_segment:
        segments.append((seg_start, len(above_mask) - 1))

    local_peak_idx = int(np.argmax(local_smoothed))
    chosen = None
    for seg_start, seg_end in segments:
        if seg_start <= local_peak_idx <= seg_end:
            chosen = (seg_start, seg_end)
            break

    if chosen is None:
        best_len = -1
        for seg_start, seg_end in segments:
            seg_len = seg_end - seg_start
            if seg_len > best_len:
                best_len = seg_len
                chosen = (seg_start, seg_end)

    seg_start, seg_end = chosen
    left_idx = int(search_indices[seg_start])
    right_idx = int(search_indices[seg_end])

    left_freq_hz = float(freqs[left_idx])
    right_freq_hz = float(freqs[right_idx])
    center_freq_hz = (left_freq_hz + right_freq_hz) / 2.0
    bandwidth_hz = right_freq_hz - left_freq_hz

    return {
        "noise_floor_db": round(noise_floor_db, 3),
        "threshold_db": round(threshold_db, 3),
        "left_idx": left_idx,
        "right_idx": right_idx,
        "left_freq_hz": round(left_freq_hz, 3),
        "right_freq_hz": round(right_freq_hz, 3),
        "center_freq_hz": round(center_freq_hz, 3),
        "bandwidth_hz": round(bandwidth_hz, 3),
    }

def find_interference_candidates(
    freqs: np.ndarray,
    power: np.ndarray,
    smoothed_power: np.ndarray,
    min_prominence_db: float = 8.0,
    max_width_hz_for_narrow: float = 20000.0,
    max_candidates: int = 12,
):
    candidates = []

    for i in range(1, len(power) - 1):
        if power[i] <= power[i - 1] or power[i] <= power[i + 1]:
            continue

        prominence_db = float(power[i] - smoothed_power[i])
        if prominence_db < min_prominence_db:
            continue

        half_level = power[i] - 3.0
        left = i
        right = i

        while left > 0 and power[left] >= half_level:
            left -= 1
        while right < len(power) - 1 and power[right] >= half_level:
            right += 1

        width_hz = float(freqs[right] - freqs[left])

        classification = "narrowband_interferer" if width_hz <= max_width_hz_for_narrow else "broad_peak"

        candidates.append({
            "freq_hz": round(float(freqs[i]), 3),
            "power_db": round(float(power[i]), 3),
            "prominence_db": round(prominence_db, 3),
            "width_hz": round(width_hz, 3),
            "classification": classification,
        })

    candidates.sort(key=lambda x: x["prominence_db"], reverse=True)
    return candidates[:max_candidates]

def manual_band_info_from_args(band_left_hz: float, band_right_hz: float):
    center_freq_hz = (band_left_hz + band_right_hz) / 2.0
    bandwidth_hz = band_right_hz - band_left_hz

    return {
        "noise_floor_db": None,
        "threshold_db": None,
        "left_idx": None,
        "right_idx": None,
        "left_freq_hz": round(float(band_left_hz), 3),
        "right_freq_hz": round(float(band_right_hz), 3),
        "center_freq_hz": round(float(center_freq_hz), 3),
        "bandwidth_hz": round(float(bandwidth_hz), 3),
        "source": "manual",
    }

def frequency_shift_iq(iq: np.ndarray, sample_rate: int, freq_offset_hz: float):
    n = np.arange(len(iq), dtype=np.float64)
    osc = np.exp(-1j * 2.0 * np.pi * freq_offset_hz * n / float(sample_rate))
    return iq * osc

def save_spectrum_plot(
    freqs,
    power,
    out_path: Path,
    title: str = "Spectrum",
    markers: dict | None = None,
    xlim_khz: tuple[float, float] | None = None,
    smoothed_power: np.ndarray | None = None,
    interference_candidates: list | None = None,
):

    plt.figure(figsize=(10, 4.5))
    plt.plot(freqs / 1000.0, power, linewidth=0.6, alpha=0.75)

    if smoothed_power is not None:
        plt.plot(freqs / 1000.0, smoothed_power, linewidth=1.4)

    if markers:
        if "peak_freq_hz" in markers:
            plt.axvline(markers["peak_freq_hz"] / 1000.0, linestyle="--", linewidth=1.0)
        if "left_freq_hz" in markers:
            plt.axvline(markers["left_freq_hz"] / 1000.0, linestyle=":", linewidth=1.0)
        if "right_freq_hz" in markers:
            plt.axvline(markers["right_freq_hz"] / 1000.0, linestyle=":", linewidth=1.0)
        if "center_freq_hz" in markers:
            plt.axvline(markers["center_freq_hz"] / 1000.0, linestyle="-.", linewidth=1.0)
        if "threshold_db" in markers and markers["threshold_db"] is not None:
            plt.axhline(markers["threshold_db"], linestyle="--", linewidth=0.9)

    if interference_candidates:
        for cand in interference_candidates:
            f_khz = cand["freq_hz"] / 1000.0
            if xlim_khz is not None and not (xlim_khz[0] <= f_khz <= xlim_khz[1]):
                continue
            plt.axvline(f_khz, linestyle=":", linewidth=0.8, alpha=0.8)


    if xlim_khz is not None:
        plt.xlim(*xlim_khz)

    plt.xlabel("Frequency offset (kHz)")
    plt.ylabel("Power (dB)")
    plt.title(title)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()

def save_waterfall_plot(iq: np.ndarray, sample_rate: int, out_path: Path):
    nfft = 2048
    hop = 1024
    max_samples = min(len(iq), 2048 * 600)

    x = iq[:max_samples]
    if len(x) < nfft:
        raise ValueError("IQ file too short for waterfall")

    rows = []
    for start in range(0, len(x) - nfft, hop):
        seg = x[start:start + nfft] * np.hanning(nfft)
        spec = np.fft.fftshift(np.fft.fft(seg))
        power = 20 * np.log10(np.abs(spec) + 1e-12)
        rows.append(power)

    wf = np.array(rows)

    plt.figure(figsize=(10, 5.5))
    extent = [
        -sample_rate / 2000.0,
        sample_rate / 2000.0,
        0,
        wf.shape[0],
    ]
    plt.imshow(wf, aspect="auto", origin="lower", extent=extent)
    plt.xlabel("Frequency offset (kHz)")
    plt.ylabel("Time blocks")
    plt.title("Waterfall")
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()


def save_iq_scatter_plot(iq: np.ndarray, out_path: Path):
    if len(iq) > 40000:
        idx = np.linspace(0, len(iq) - 1, 40000, dtype=np.int64)
        x = iq[idx]
    else:
        x = iq

    plt.figure(figsize=(5.5, 5.5))
    plt.scatter(np.real(x), np.imag(x), s=1, alpha=0.35)
    plt.xlabel("I")
    plt.ylabel("Q")
    plt.title("IQ Scatter")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()


def save_amplitude_plot(iq: np.ndarray, sample_rate: int, out_path: Path):
    mag = np.abs(iq)
    blocks = 2000
    step = max(1, len(mag) // blocks)

    y = []
    x = []
    for idx in range(0, len(mag), step):
        seg = mag[idx:idx + step]
        if len(seg) == 0:
            continue
        y.append(float(np.mean(seg)))
        x.append(idx / sample_rate)

    plt.figure(figsize=(10, 4.5))
    plt.plot(x, y, linewidth=0.8)
    plt.xlabel("Time (s)")
    plt.ylabel("Average magnitude")
    plt.title("Amplitude Over Time")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()


def save_dc_hist_plot(iq: np.ndarray, out_path: Path):
    if len(iq) > 200000:
        idx = np.linspace(0, len(iq) - 1, 200000, dtype=np.int64)
        x = iq[idx]
    else:
        x = iq

    i = np.real(x)
    q = np.imag(x)

    plt.figure(figsize=(10, 4.5))
    plt.hist(i, bins=150, alpha=0.6, label="I")
    plt.hist(q, bins=150, alpha=0.6, label="Q")
    plt.xlabel("Sample value")
    plt.ylabel("Count")
    plt.title("I/Q Histogram")
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_path, dpi=160)
    plt.close()

def build_pdf_report(
    pass_dir: Path,
    iq_file: Path,
    report_dir: Path,
    detected_format: str,
    metrics: dict,
    spectrum_info: dict,
    band_info: dict,
    corrected_spectrum_info: dict,
    corrected_band_info: dict,
    interference_candidates: list,
    corrected_interference_candidates: list,
):
    pdf_path = report_dir / "decode_report.pdf"

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]

    body.fontName = "Helvetica"
    body.fontSize = 9.5
    body.leading = 13

    small = ParagraphStyle(
        "Small",
        parent=body,
        fontSize=8.5,
        leading=11,
        alignment=TA_LEFT,
    )

    story = []

    story.append(Paragraph("IQ/Baseband Decode Report", title_style))
    story.append(Spacer(1, 5 * mm))

    meta_rows = [
        ["Pass directory", str(pass_dir)],
        ["IQ file", iq_file.name],
        ["Format", detected_format],
        ["Sample rate", str(metrics["sample_rate"])],
        ["Samples", str(metrics["sample_count"])],
        ["Duration [s]", str(metrics["duration_s"])],
        ["Quality score", f'{metrics["quality_score"]}/100'],
        ["Quality class", metrics["quality_class"]],
        ["Peak frequency offset [Hz]", str(spectrum_info["peak_freq_hz"])],
        ["Estimated band left [Hz]", str(band_info["left_freq_hz"])],
        ["Estimated band right [Hz]", str(band_info["right_freq_hz"])],
        ["Estimated band center [Hz]", str(band_info["center_freq_hz"])],
        ["Estimated bandwidth [Hz]", str(band_info["bandwidth_hz"])],
        ["Band source", str(band_info.get("source"))],
        ["Applied frequency correction [Hz]", str(spectrum_info["applied_correction_hz"])],
        ["Residual band center after correction [Hz]", str(corrected_band_info["center_freq_hz"])],
        ["Residual peak after correction [Hz]", str(corrected_spectrum_info["peak_freq_hz"])],
    ]

    meta_table = Table(meta_rows, colWidths=[52 * mm, 110 * mm])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF2F8")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEADING", (0, 0), (-1, -1), 11),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("1. Overview", h1))
    story.append(Paragraph(
        "This report analyses the recorded IQ/baseband file before any higher-level decoding. "
        "The goal is to assess whether the recorded signal looks usable and to document the main "
        "steps of the signal inspection process.",
        body,
    ))
    story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("2. Signal statistics", h1))
    stats_rows = [
        ["I mean", str(metrics["i_mean"])],
        ["Q mean", str(metrics["q_mean"])],
        ["I stddev", str(metrics["i_std"])],
        ["Q stddev", str(metrics["q_std"])],
        ["Magnitude mean", str(metrics["mag_mean"])],
        ["Magnitude stddev", str(metrics["mag_std"])],
        ["DC offset abs", str(metrics["dc_offset_abs"])],
        ["I/Q balance ratio", str(metrics["iq_balance_ratio"])],
        ["Clipped samples", str(metrics["clipped_samples"])],
        ["Clip ratio", str(metrics["clip_ratio"])],
    ]
    stats_table = Table(stats_rows, colWidths=[52 * mm, 35 * mm])
    stats_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F6F6F6")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEADING", (0, 0), (-1, -1), 11),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(stats_table)
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "These metrics describe the raw I/Q sample distribution. A large DC offset can indicate receiver "
        "bias, clipping indicates overload, and the I/Q balance gives a quick indication of whether the "
        "in-phase and quadrature components behave similarly.",
        small,
    ))
    story.append(Spacer(1, 5 * mm))

    for idx, finding in enumerate(metrics["findings"], start=1):
        story.append(Paragraph(f"- {finding}", body))
    story.append(Spacer(1, 5 * mm))

    def add_plot(title, explanation, filename, width_mm=170):
        path = report_dir / filename
        if not path.exists():
            return
        story.append(Paragraph(title, h2))
        story.append(Paragraph(explanation, small))
        story.append(Spacer(1, 2 * mm))
        story.append(RLImage(str(path), width=width_mm * mm, height=(width_mm * 0.55) * mm))
        story.append(Spacer(1, 5 * mm))

    add_plot(
        "3. Spectrum",
        "The spectrum shows how signal power is distributed across frequency. A clear concentration around "
        "a stable offset can indicate that the signal was tuned close to the expected centre frequency.",
        "spectrum.png",
    )

    add_plot(
        "4. Corrected spectrum",
        "This spectrum is shown after digital frequency shifting by the estimated signal band centre, "
        "not merely by the strongest FFT peak. This is usually a better approximation when the tallest "
        "peak does not coincide with the true centre of the occupied signal bandwidth.",
        "spectrum_corrected.png",
    )

    add_plot(
        "5. Corrected spectrum zoom",
        "This zoomed corrected spectrum is the most useful view for judging whether the signal band is now "
        "properly centred around 0 Hz.",
        "spectrum_corrected_zoom.png",
    )

    story.append(Paragraph("6. Interference candidates", h2))
    story.append(Paragraph(
        "The following peaks stand out above the smoothed spectral baseline and may indicate narrowband interference "
        "or other strong spectral features. Narrow, stable peaks are often more suspicious than broad occupied bands.",
        small,
    ))
    story.append(Spacer(1, 2 * mm))

    rows = [["Freq [Hz]", "Power [dB]", "Prominence [dB]", "Width [Hz]", "Class"]]
    for cand in corrected_interference_candidates[:8]:
        rows.append([
            str(cand["freq_hz"]),
            str(cand["power_db"]),
            str(cand["prominence_db"]),
            str(cand["width_hz"]),
            cand["classification"],
        ])

    itable = Table(rows, colWidths=[32 * mm, 28 * mm, 34 * mm, 30 * mm, 42 * mm])
    itable.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2F8")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("LEADING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(itable)
    story.append(Spacer(1, 5 * mm))

    add_plot(
        "7. Corrected waterfall",
        "This waterfall is shown after digital frequency correction. A useful correction should move the "
        "main signal trace closer to the centre frequency and make later demodulation steps easier.",
        "waterfall_corrected.png",
    )

    story.append(PageBreak())

    add_plot(
        "8. IQ scatter",
        "The IQ scatter plot shows the distribution of complex samples in the I/Q plane. Strong asymmetry, "
        "offsets, or distortion can point to DC issues, imbalance, or poor signal conditions.",
        "iq_scatter.png",
        width_mm=120,
    )

    add_plot(
        "9. Amplitude over time",
        "This plot shows average signal magnitude over time. It helps identify fades, dropouts, large level "
        "changes, or sections where the signal becomes weak or disappears.",
        "amplitude_over_time.png",
    )

    add_plot(
        "10. I/Q histogram",
        "The histogram of I and Q samples helps identify clipping, asymmetry, and coarse distribution problems.",
        "iq_histogram.png",
    )

    story.append(Paragraph("11. Interpretation", h1))
    story.append(Paragraph(
        "This report currently documents the front-end signal inspection only. It does not yet perform full "
        "demodulation, timing recovery, symbol extraction, or frame reconstruction. The next expansion step "
        "would be to add frequency correction, demodulated symbol plots, and subsequent frame-level diagnostics.",
        body,
    ))

    doc.build(story)
    return pdf_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    parser.add_argument("--format", default=DEFAULT_FORMAT)
    parser.add_argument("--band-left-hz", type=float, default=None)
    parser.add_argument("--band-right-hz", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input_path).expanduser().resolve()
    iq_file, pass_dir = resolve_input(input_path)

    if iq_file is None or not iq_file.exists():
        raise SystemExit("No IQ/baseband file found")

    report_dir = pass_dir / "decode_report"
    report_dir.mkdir(parents=True, exist_ok=True)

    _iq_loaded, actual_sample_rate, detected_format, input_meta = load_iq_file(
        iq_file,
        forced_format=args.format,
        forced_sample_rate=args.sample_rate,
    )

    metrics = compute_metrics(
        _iq_loaded,
        actual_sample_rate,
        detected_format,
        iq_path=iq_file,
    )

    iq = load_analysis_iq(iq_file, detected_format, max_complex_samples=262144)

    freqs, power, spectrum_info = estimate_spectrum(iq, actual_sample_rate)
    smoothed_power = smooth_power(power, window_len=301)

    if (args.band_left_hz is None) != (args.band_right_hz is None):
        raise SystemExit("Either set both --band-left-hz and --band-right-hz, or neither")

    if args.band_left_hz is not None and args.band_right_hz is not None:
        band_info = manual_band_info_from_args(args.band_left_hz, args.band_right_hz)
    else:
        band_info = estimate_signal_band(freqs, power, spectrum_info["peak_idx"])
        band_info["source"] = "automatic"

    interference_candidates = find_interference_candidates(freqs, power, smoothed_power)

    applied_correction_hz = band_info["center_freq_hz"]
    iq_corrected = frequency_shift_iq(iq, actual_sample_rate, applied_correction_hz)

    freqs_corr, power_corr, corrected_spectrum_info = estimate_spectrum(iq_corrected, actual_sample_rate)
    smoothed_power_corr = smooth_power(power_corr, window_len=301)

    corrected_band_info = estimate_signal_band(
        freqs_corr,
        power_corr,
        corrected_spectrum_info["peak_idx"],
    )
    corrected_band_info["source"] = "automatic_after_correction"

    corrected_interference_candidates = find_interference_candidates(
        freqs_corr,
        power_corr,
        smoothed_power_corr,
    )


    spectrum_info["applied_correction_hz"] = round(applied_correction_hz, 3)

    save_spectrum_plot(
        freqs,
        power,
        report_dir / "spectrum.png",
        title="Spectrum",
        markers={
            "peak_freq_hz": spectrum_info["peak_freq_hz"],
            "left_freq_hz": band_info["left_freq_hz"],
            "right_freq_hz": band_info["right_freq_hz"],
            "center_freq_hz": band_info["center_freq_hz"],
            "threshold_db": band_info["threshold_db"],
        },
        smoothed_power=smoothed_power,
        interference_candidates=interference_candidates,
    )

    save_spectrum_plot(
        freqs_corr,
        power_corr,
        report_dir / "spectrum_corrected.png",
        title="Spectrum (Corrected)",
        markers={
            "peak_freq_hz": corrected_spectrum_info["peak_freq_hz"],
            "left_freq_hz": corrected_band_info["left_freq_hz"],
            "right_freq_hz": corrected_band_info["right_freq_hz"],
            "center_freq_hz": corrected_band_info["center_freq_hz"],
            "threshold_db": corrected_band_info["threshold_db"],
        },
        smoothed_power=smoothed_power_corr,
        interference_candidates=corrected_interference_candidates,
    )

    save_spectrum_plot(
        freqs_corr,
        power_corr,
        report_dir / "spectrum_corrected_zoom.png",
        title="Spectrum Zoom (Corrected)",
        markers={
            "peak_freq_hz": corrected_spectrum_info["peak_freq_hz"],
            "left_freq_hz": corrected_band_info["left_freq_hz"],
            "right_freq_hz": corrected_band_info["right_freq_hz"],
            "center_freq_hz": corrected_band_info["center_freq_hz"],
            "threshold_db": corrected_band_info["threshold_db"],
        },
        xlim_khz=(-200, 200),
        smoothed_power=smoothed_power_corr,
        interference_candidates=corrected_interference_candidates,
    )

    save_waterfall_plot(iq_corrected, actual_sample_rate, report_dir / "waterfall_corrected.png")
    save_iq_scatter_plot(iq, report_dir / "iq_scatter.png")
    save_amplitude_plot(iq, actual_sample_rate, report_dir / "amplitude_over_time.png")
    save_dc_hist_plot(iq, report_dir / "iq_histogram.png")

    pdf_path = build_pdf_report(
        pass_dir,
        iq_file,
        report_dir,
        detected_format,
        metrics,
        spectrum_info,
        band_info,
        corrected_spectrum_info,
        corrected_band_info,
        interference_candidates,
        corrected_interference_candidates,
    )

    result = {
        "pass_dir": str(pass_dir),
        "iq_file": str(iq_file),
        "format": detected_format,
        "sample_rate": actual_sample_rate,
        "input_meta": input_meta,
        "metrics": metrics,
        "spectrum": spectrum_info,
        "band": band_info,
        "corrected_spectrum": corrected_spectrum_info,
        "corrected_band": corrected_band_info,
        "interference_candidates": interference_candidates,
        "corrected_interference_candidates": corrected_interference_candidates,
        "report_dir": str(report_dir),
        "pdf_report": str(pdf_path),
        "plots": {
            "spectrum": str(report_dir / "spectrum.png"),
            "spectrum_corrected": str(report_dir / "spectrum_corrected.png"),
            "spectrum_corrected_zoom": str(report_dir / "spectrum_corrected_zoom.png"),
            "waterfall_corrected": str(report_dir / "waterfall_corrected.png"),
            "iq_scatter": str(report_dir / "iq_scatter.png"),
            "amplitude_over_time": str(report_dir / "amplitude_over_time.png"),
            "iq_histogram": str(report_dir / "iq_histogram.png"),
        },
    }

    with open(report_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"Pass directory:   {pass_dir}")
    print(f"IQ file:          {iq_file}")
    print(f"Sample rate:      {actual_sample_rate}")
    print(f"Detected format:  {detected_format}")
    print(f"Quality score:    {metrics['quality_score']}/100")
    print(f"Quality class:    {metrics['quality_class']}")
    print(f"Peak freq offset: {spectrum_info['peak_freq_hz']} Hz")
    print(f"Estimated band left: {band_info['left_freq_hz']} Hz")
    print(f"Estimated band right: {band_info['right_freq_hz']} Hz")
    print(f"Estimated band center: {band_info['center_freq_hz']} Hz")
    print(f"Band source: {band_info.get('source')}")
    print(f"Applied correction: {-band_info['center_freq_hz']} Hz")
    print(f"Residual band center after correction: {corrected_band_info['center_freq_hz']} Hz")
    print(f"Residual peak after correction: {corrected_spectrum_info['peak_freq_hz']} Hz")
    print(f"PDF report:       {pdf_path}")
    print("Findings:")
    for finding in metrics["findings"]:
        print(f"  - {finding}")


if __name__ == "__main__":
    main()
