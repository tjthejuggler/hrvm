"""
Session Recorder for Chess-Coach Integration.

Records heart rate data from Polar H10 into JSON files following the
chess-coach HR session format specification (v1.0).

Each recording session produces one JSON file at:
    /home/twain/Projects/chess-coach/data/hr_sessions/{YYYYMMDD}T{HHMMSS}Z.json

The recorder accumulates HR, RR interval, RMSSD, and SDNN samples in memory
and writes the complete JSON file when the session is finalized.

RRRecorder is a simpler recorder that captures a flat list of all RR values
for any recording type (chess, meditation, movie, custom). Output is saved to
the current working directory as {unix_epoch_seconds}.json.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Default output directory for chess-coach integration
DEFAULT_OUTPUT_DIR = "/home/twain/Projects/chess-coach/data/hr_sessions"

# HRV computation constants
HRV_WINDOW_SIZE_SECONDS = 5
HRV_MIN_RR_COUNT = 3
ARTIFACT_DEVIATION_PERCENT = 0.20  # 20% deviation from local median


def _format_utc_iso(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC with milliseconds."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _reject_artifacts(rr_intervals: List[float], window: int = 5) -> List[float]:
    """Reject RR intervals deviating >20% from local median (5-beat window).

    Returns list of valid RR intervals (same length, invalid replaced with None
    markers internally, but output is filtered to valid-only).
    """
    if len(rr_intervals) < window:
        return list(rr_intervals)

    valid = []
    arr = np.array(rr_intervals)
    half_w = window // 2

    for i in range(len(arr)):
        start = max(0, i - half_w)
        end = min(len(arr), i + half_w + 1)
        local_median = np.median(arr[start:end])
        if abs(arr[i] - local_median) / local_median <= ARTIFACT_DEVIATION_PERCENT:
            valid.append(float(arr[i]))

    return valid


def _compute_rmssd(rr_intervals: List[float]) -> Optional[float]:
    """Compute RMSSD from a list of RR intervals in ms.

    Returns None if fewer than HRV_MIN_RR_COUNT valid intervals.
    """
    if len(rr_intervals) < HRV_MIN_RR_COUNT:
        return None
    arr = np.array(rr_intervals)
    diffs = np.diff(arr)
    return round(float(np.sqrt(np.mean(diffs ** 2))), 1)


def _compute_sdnn(rr_intervals: List[float]) -> Optional[float]:
    """Compute SDNN from a list of RR intervals in ms.

    Returns None if fewer than HRV_MIN_RR_COUNT valid intervals.
    """
    if len(rr_intervals) < HRV_MIN_RR_COUNT:
        return None
    return round(float(np.std(rr_intervals)), 1)


class SessionRecorder:
    """Records a single continuous HR session to JSON.

    Usage:
        recorder = SessionRecorder(device_id="A1B2C3D4")
        recorder.start()
        # ... during streaming:
        recorder.add_hr_sample(bpm=72)
        recorder.add_rr_interval(rr_ms=833)
        # ... when done:
        recorder.stop()
    """

    def __init__(self, device_id: str = "", device_name: str = "Polar H10",
                 firmware: str = "", output_dir: str = DEFAULT_OUTPUT_DIR,
                 timezone_offset_minutes: int = 60):
        self.device_id = device_id
        self.device_name = device_name
        self.firmware = firmware
        self.output_dir = output_dir
        self.timezone_offset_minutes = timezone_offset_minutes

        # Session state
        self.is_recording = False
        self.session_start_utc: Optional[datetime] = None
        self.session_start_unix: Optional[float] = None

        # Raw data accumulators
        self._hr_samples: List[dict] = []       # {"t": ms, "bpm": int}
        self._rr_samples: List[dict] = []       # {"t": ms, "rr_ms": int}
        self._rr_cumulative_t: int = 0           # Running cumulative t for RR

        # Last HR timestamp for 1Hz sampling enforcement
        self._last_hr_t_ms: int = -1000  # Allow first sample immediately

    def start(self) -> None:
        """Begin a new recording session. Captures start time as UTC now."""
        if self.is_recording:
            logger.warning("SessionRecorder.start() called while already recording")
            return

        self.session_start_utc = datetime.now(timezone.utc)
        self.session_start_unix = time.time()
        self.is_recording = True

        self._hr_samples = []
        self._rr_samples = []
        self._rr_cumulative_t = 0
        self._last_hr_t_ms = -1000

        logger.info(f"Session recording started at {_format_utc_iso(self.session_start_utc)}")

    def add_hr_sample(self, bpm: int) -> None:
        """Record a heart rate BPM sample.

        Samples are timestamped relative to session start. Only one sample
        per second is kept (1 Hz nominal rate).
        """
        if not self.is_recording or self.session_start_unix is None:
            return

        t_ms = int((time.time() - self.session_start_unix) * 1000)

        # Enforce ~1Hz: skip if less than 900ms since last sample
        if t_ms - self._last_hr_t_ms < 900:
            return

        # Snap to 1-second grid
        t_snapped = (len(self._hr_samples)) * 1000

        self._hr_samples.append({"t": t_snapped, "bpm": int(bpm)})
        self._last_hr_t_ms = t_ms

    def add_rr_interval(self, rr_ms: float) -> None:
        """Record a raw RR interval.

        The `t` field is the cumulative sum of all previous RR intervals,
        per the spec: t[i+1] = t[i] + rr_ms[i].
        """
        if not self.is_recording:
            return

        rr_int = int(round(rr_ms))

        # Basic physiological validity
        if not (300 < rr_int < 2000):
            return

        self._rr_samples.append({
            "t": self._rr_cumulative_t,
            "rr_ms": rr_int
        })
        self._rr_cumulative_t += rr_int

    def stop(self) -> Optional[str]:
        """Stop recording and write the JSON file.

        Returns the filepath of the written JSON, or None on failure.
        """
        if not self.is_recording or self.session_start_utc is None:
            logger.warning("SessionRecorder.stop() called but not recording")
            return None

        self.is_recording = False
        session_end_utc = datetime.now(timezone.utc)
        duration_seconds = int((session_end_utc - self.session_start_utc).total_seconds())

        # Compute HRV windows from raw RR data
        rmssd_samples, sdnn_samples = self._compute_hrv_windows()

        # Build the JSON structure
        start_utc_str = _format_utc_iso(self.session_start_utc)
        end_utc_str = _format_utc_iso(session_end_utc)

        session_data = {
            "format_version": "1.0",
            "device": {
                "name": self.device_name,
                "id": self.device_id,
            },
            "session": {
                "start_utc": start_utc_str,
                "end_utc": end_utc_str,
                "duration_seconds": duration_seconds,
                "timezone_offset_minutes": self.timezone_offset_minutes,
            },
            "heart_rate": {
                "sampling_rate_hz": 1,
                "start_utc": start_utc_str,
                "samples": self._hr_samples,
            },
            "rr_intervals": {
                "start_utc": start_utc_str,
                "samples": self._rr_samples,
            },
            "rmssd": {
                "window_size_seconds": HRV_WINDOW_SIZE_SECONDS,
                "start_utc": start_utc_str,
                "samples": rmssd_samples,
            },
            "sdnn": {
                "window_size_seconds": HRV_WINDOW_SIZE_SECONDS,
                "start_utc": start_utc_str,
                "samples": sdnn_samples,
            },
        }

        # Add firmware if available
        if self.firmware:
            session_data["device"]["firmware"] = self.firmware

        # Write to file
        filepath = self._write_json(session_data)

        logger.info(
            f"Session recording stopped. Duration: {duration_seconds}s, "
            f"HR samples: {len(self._hr_samples)}, "
            f"RR samples: {len(self._rr_samples)}, "
            f"HRV windows: {len(rmssd_samples)}"
        )

        return filepath

    def _compute_hrv_windows(self) -> tuple:
        """Compute RMSSD and SDNN from raw RR intervals using 5s windows.

        Uses non-overlapping 5-second windows with artifact rejection.
        Returns (rmssd_samples, sdnn_samples) lists.
        """
        if not self._rr_samples:
            return [], []

        window_ms = HRV_WINDOW_SIZE_SECONDS * 1000
        rmssd_samples = []
        sdnn_samples = []

        # Total duration from RR data
        total_duration_ms = self._rr_cumulative_t
        if total_duration_ms < window_ms:
            return [], []

        # Process non-overlapping windows
        window_idx = 0
        window_start_ms = 0

        while window_start_ms + window_ms <= total_duration_ms:
            # Collect RR intervals that fall within this window
            window_rr = []
            for sample in self._rr_samples:
                if window_start_ms <= sample["t"] < window_start_ms + window_ms:
                    window_rr.append(float(sample["rr_ms"]))

            # Apply artifact rejection
            clean_rr = _reject_artifacts(window_rr)

            # Compute metrics
            rmssd_val = _compute_rmssd(clean_rr)
            sdnn_val = _compute_sdnn(clean_rr)

            t_ms = window_idx * window_ms

            rmssd_samples.append({"t": t_ms, "value": rmssd_val})
            sdnn_samples.append({"t": t_ms, "value": sdnn_val})

            window_start_ms += window_ms
            window_idx += 1

        return rmssd_samples, sdnn_samples

    def _write_json(self, data: dict) -> Optional[str]:
        """Write session data to JSON file in the output directory.

        Filename: {YYYYMMDD}T{HHMMSS}Z.json based on session start UTC.
        """
        if self.session_start_utc is None:
            return None

        # Ensure output directory exists
        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create output directory {self.output_dir}: {e}")
            return None

        # Generate filename from session start UTC
        filename = self.session_start_utc.strftime("%Y%m%dT%H%M%SZ") + ".json"
        filepath = os.path.join(self.output_dir, filename)

        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Session JSON written to: {filepath}")
            return filepath
        except (OSError, IOError) as e:
            logger.error(f"Failed to write session JSON to {filepath}: {e}")
            return None


# Default output directory for RR recordings
RR_OUTPUT_DIR = "/home/twain/Projects/hrvm/recordings"


class RRRecorder:
    """Simple recorder that captures a flat list of all RR intervals.

    Works for any recording type (chess, meditation, movie, custom).
    Output is saved to RR_OUTPUT_DIR as:
        {unix_epoch_seconds}.json

    JSON structure:
        {
            "type": "chess",
            "started_at": "2026-02-20T09:00:00.000Z",
            "rr_values": [832, 845, 801, ...]
        }
    """

    def __init__(self, recording_type: str = "chess",
                 output_dir: str = RR_OUTPUT_DIR):
        self.recording_type = recording_type
        self.output_dir = output_dir
        self.is_recording = False
        self._start_unix: Optional[float] = None
        self._start_utc: Optional[datetime] = None
        self._rr_values: List[int] = []

    def start(self) -> None:
        """Begin capturing RR intervals."""
        if self.is_recording:
            logger.warning("RRRecorder.start() called while already recording")
            return
        self._start_unix = time.time()
        self._start_utc = datetime.now(timezone.utc)
        self._rr_values = []
        self.is_recording = True
        logger.info(
            f"RRRecorder started (type={self.recording_type}) "
            f"at {_format_utc_iso(self._start_utc)}"
        )

    def add_rr(self, rr_ms: float) -> None:
        """Append a single RR interval (ms). Physiologically invalid values are dropped."""
        if not self.is_recording:
            return
        rr_int = int(round(rr_ms))
        if 300 < rr_int < 2000:
            self._rr_values.append(rr_int)

    def stop(self) -> Optional[str]:
        """Stop recording and write the JSON file.

        Returns the filepath of the written JSON, or None on failure.
        Filename is the integer Unix epoch seconds at recording start.
        """
        if not self.is_recording or self._start_utc is None or self._start_unix is None:
            logger.warning("RRRecorder.stop() called but not recording")
            return None

        self.is_recording = False

        data = {
            "type": self.recording_type,
            "started_at": _format_utc_iso(self._start_utc),
            "rr_values": self._rr_values,
        }

        epoch_seconds = int(self._start_unix)
        filename = f"{epoch_seconds}.json"

        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"RRRecorder failed to create output dir {self.output_dir}: {e}")
            return None

        filepath = os.path.join(self.output_dir, filename)

        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(
                f"RRRecorder saved {len(self._rr_values)} RR values to: {filepath}"
            )
            return filepath
        except (OSError, IOError) as e:
            logger.error(f"RRRecorder failed to write {filepath}: {e}")
            return None
