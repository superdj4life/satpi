"""satpi – pass_sampler

Per-sample telemetry collector for a live satellite pass.

Parses SatDump's INFO-level status lines (SNR / BER / Viterbi / Deframer)
and combines them with az/el computed from the satellite TLE and the QTH
coordinates. Designed to be fed line-by-line from receive_pass.py's reader
thread; flushes one sample dict per paired (SNR, decoder) line pair.

Sample dict shape matches the pass_detail table:
    timestamp        ISO-8601 UTC string ("2026-05-18T02:33:57Z")
    snr_db           float | None
    peak_snr_db      float | None
    ber              float | None
    viterbi_state    str  ("SYNCED" / "NOSYNC" / …)
    deframer_state   str
    azimuth_deg      float | None
    elevation_deg    float | None

Author: Andreas Horvath
Project: satpi
"""

from __future__ import annotations

import logging
import math
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("satpi.pass_sampler")


# SatDump 1.2.x live status format (two paired INFO lines, every ~10 s):
#   [HH:MM:SS - DD/MM/YYYY] (I) Progress NN%, SNR : 12.345678dB, Peak SNR: 14.123456dB
#   [HH:MM:SS - DD/MM/YYYY] (I) Progress NN%, Viterbi : SYNCED BER : 0.001234, Deframer : SYNCED
# Numbers may be "nan" or "inf" before lock.
_SNR_RE = re.compile(
    r"SNR\s*:\s*(?P<snr>[\-\dA-Za-z\.]+)\s*dB\s*,\s*"
    r"Peak\s+SNR\s*:\s*(?P<peak>[\-\dA-Za-z\.]+)\s*dB"
)
_DEC_RE = re.compile(
    r"Viterbi\s*:\s*(?P<vit>\S+)\s+"
    r"BER\s*:\s*(?P<ber>[\-\dA-Za-z\.]+)\s*,\s*"
    r"Deframer\s*:\s*(?P<dfr>\S+)"
)


def _parse_float(s: str) -> Optional[float]:
    """Float-parse that returns None for NaN/inf/garbage. SatDump prints
    'nan'/'inf' before signal lock; we store NULL in the DB instead."""
    try:
        v = float(s)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _normalize_sat_name(name: str) -> str:
    """Strip whitespace/dashes/underscores and lowercase, so satpi's
    'METEOR-M2 4' matches the TLE's 'METEOR-M 2 4' / 'METEOR-M2-4' etc."""
    return re.sub(r"[\s\-_]", "", name).lower()


class PassSampler:
    """Collect per-sample telemetry for one pass.

    Usage:
        sampler = PassSampler(sat_name, tle_file, qth_lat, qth_lon, qth_alt)
        for line in satdump.stdout:
            sampler.feed_satdump_line(line)
        sampler.stop()
        samples = sampler.samples
    """

    def __init__(
        self,
        satellite_name: str,
        tle_file: str,
        qth_lat: float,
        qth_lon: float,
        qth_alt_m: float,
    ) -> None:
        # Defer skyfield import so callers that don't need az/el (e.g. unit
        # tests of the parser) don't pay the import cost.
        from skyfield.api import EarthSatellite, load, wgs84

        self._lock = threading.Lock()
        self._samples: List[Dict[str, Any]] = []
        self._pending: Optional[Dict[str, Any]] = None

        self._ts = load.timescale()
        self._observer = wgs84.latlon(qth_lat, qth_lon, elevation_m=qth_alt_m)
        self._sat = self._load_satellite_from_tle(
            satellite_name, tle_file, EarthSatellite, self._ts,
        )

    @staticmethod
    def _load_satellite_from_tle(
        satellite_name: str,
        tle_file: str,
        EarthSatellite,
        ts,
    ) -> Optional[Any]:
        """Find the named satellite in a 3-line-element TLE file.

        Returns an EarthSatellite or None (warn-only — sample collection still
        works for SNR/BER, just without az/el)."""
        target = _normalize_sat_name(satellite_name)
        try:
            with open(tle_file, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\r\n") for ln in f.readlines()]
        except OSError as e:
            logger.warning("Could not read TLE file %s: %s", tle_file, e)
            return None

        for i in range(0, len(lines) - 2, 3):
            name = lines[i].strip()
            if not name:
                continue
            if _normalize_sat_name(name) == target:
                try:
                    return EarthSatellite(lines[i + 1], lines[i + 2], name, ts)
                except Exception as e:
                    logger.warning("Failed to parse TLE for %s: %s", name, e)
                    return None

        logger.warning(
            "Satellite '%s' not found in TLE %s — az/el will be NULL",
            satellite_name, tle_file,
        )
        return None

    def _compute_azel(self, when_utc: datetime) -> Tuple[Optional[float], Optional[float]]:
        if self._sat is None:
            return None, None
        try:
            t = self._ts.from_datetime(when_utc)
            topocentric = (self._sat - self._observer).at(t)
            alt, az, _distance = topocentric.altaz()
            return float(az.degrees), float(alt.degrees)
        except Exception as e:
            logger.warning("az/el computation failed: %s", e)
            return None, None

    def _flush_pending(self) -> None:
        """Materialize self._pending into a full sample dict and append it."""
        if not self._pending:
            return
        when = self._pending["__when"]
        az, el = self._compute_azel(when)
        sample = {
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "snr_db": self._pending.get("snr_db"),
            "peak_snr_db": self._pending.get("peak_snr_db"),
            "ber": self._pending.get("ber"),
            "viterbi_state": self._pending.get("viterbi_state"),
            "deframer_state": self._pending.get("deframer_state"),
            "azimuth_deg": az,
            "elevation_deg": el,
        }
        with self._lock:
            self._samples.append(sample)
        self._pending = None

    def feed_satdump_line(self, line: str) -> None:
        """Parse a single SatDump output line and update state.

        SatDump emits paired status lines per tick. The SNR line comes first,
        the decoder line right after. Both share the same wall-clock timestamp.
        We accumulate them into one sample and flush on the decoder line.
        """
        m = _SNR_RE.search(line)
        if m:
            # New tick: if the previous tick was missing its decoder line,
            # flush it as-is (with NULL viterbi/ber/deframer fields).
            if self._pending is not None:
                self._flush_pending()
            self._pending = {
                "__when": datetime.now(timezone.utc),
                "snr_db": _parse_float(m.group("snr")),
                "peak_snr_db": _parse_float(m.group("peak")),
            }
            return

        m = _DEC_RE.search(line)
        if m and self._pending is not None:
            self._pending["viterbi_state"] = m.group("vit").strip().upper()
            self._pending["ber"] = _parse_float(m.group("ber"))
            self._pending["deframer_state"] = m.group("dfr").strip().upper()
            self._flush_pending()

    def stop(self) -> None:
        """Called from the main thread after the reader thread has joined.
        Flushes any leftover half-sample."""
        if self._pending is not None:
            self._flush_pending()

    @property
    def samples(self) -> List[Dict[str, Any]]:
        """Snapshot of accumulated samples (returns a copy, safe to mutate)."""
        with self._lock:
            return list(self._samples)

