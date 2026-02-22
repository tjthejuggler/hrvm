"""
ACC-based Respiration Detection Engine.

Detects breathing phase (INHALING, EXHALING, HOLDING) in real-time from
Polar H10 accelerometer Z-axis data using a calibrated threshold approach.

Calibration is per-profile (standing, sitting, laying) and persists across
sessions via a JSON file.

Usage:
    engine = AccRespirationEngine()
    engine.feed_samples(z_samples)          # called each ACC batch
    phase = engine.current_phase            # "INHALING" | "EXHALING" | "HOLDING" | None
    bpm   = engine.current_breath_rate_bpm  # float or None
"""
import json
import logging
import os
import time
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (mirrored from test_acc_respiration.py)
# ---------------------------------------------------------------------------
SAMPLING_RATE = 200          # Hz — Polar H10 ACC at 200 Hz
BUFFER_SEC = 2
BUFFER_SIZE = SAMPLING_RATE * BUFFER_SEC

SMOOTHING_SAMPLES = 40       # 0.2 s
LOOKBACK_SAMPLES = 100       # 0.5 s

DROP_FACTOR = 0.15           # 15% hysteresis

DEBOUNCE_BREATH = 3          # ~0.15 s — fast trigger for inhale/exhale
DEBOUNCE_HOLD = 12           # ~0.60 s — swallows deadzone, prevents micro-holds

PROFILES = ["standing", "sitting", "laying"]
_CAL_FILE = "acc_breath_cal.json"

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AccRespirationEngine:
    """Real-time ACC-based respiration phase detector with per-profile calibration."""

    def __init__(self):
        # Rolling Z-axis buffer (200 Hz × 2 s)
        self._z_buf: deque = deque(maxlen=BUFFER_SIZE)

        # Per-profile calibration thresholds  {profile: (thresh_in, thresh_ex)}
        self._cal: dict = {}
        self._load_calibration()

        # Active profile
        self._profile: str = PROFILES[0]

        # Detection state
        self.current_phase: Optional[str] = None   # debounced committed phase
        self._candidate_phase: Optional[str] = None
        self._debounce_count: int = 0
        self.predicted_phase: Optional[str] = None  # raw live prediction (no debounce)

        # Breath-rate estimation
        self._phase_timestamps: deque = deque(maxlen=20)  # times of INHALING transitions
        self.current_breath_rate_bpm: Optional[float] = None

        # Calibration session state
        self._calibrating: bool = False
        self._cal_state: Optional[str] = None       # "INHALING" | "EXHALING" | "HOLDING"
        self._cal_data: dict = {
            "INHALING": deque(maxlen=400),
            "EXHALING": deque(maxlen=400),
            "HOLDING":  deque(maxlen=400),
        }
        self._thresh_in: float = 2.0
        self._thresh_ex: float = -2.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def profile(self) -> str:
        return self._profile

    @profile.setter
    def profile(self, value: str):
        if value in PROFILES:
            self._profile = value
            self._apply_profile_thresholds()

    def feed_samples(self, z_samples):
        """Feed a list/array of Z-axis ACC samples (mG) into the engine."""
        for z in z_samples:
            self._z_buf.append(z)
        if len(self._z_buf) < BUFFER_SIZE:
            return  # not enough data yet

        delta = self._compute_delta()

        # Always compute the raw live prediction (used by the chart and cal popup)
        self.predicted_phase = self._get_predicted_phase(delta, self.current_phase)

        if self._calibrating:
            if self._cal_state is not None:
                self._cal_data[self._cal_state].append(delta)
                self._recalculate_thresholds()
        else:
            self._update_phase(delta)

    def get_delta(self) -> Optional[float]:
        """Return the current smoothed delta (for debug display)."""
        if len(self._z_buf) < BUFFER_SIZE:
            return None
        return self._compute_delta()

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def start_calibration(self):
        """Begin a calibration session for the current profile.

        Keeps existing thresholds (loaded from file) as the starting point so
        the 'System sees' feedback is accurate from the first frame.  Thresholds
        are only updated incrementally as the user feeds new cal data.
        """
        self._calibrating = True
        self._cal_state = None
        for key in self._cal_data:
            self._cal_data[key].clear()
        # Do NOT reset thresholds here — keep the saved values so the popup
        # shows meaningful feedback immediately.  If no saved calibration exists
        # for this profile, _apply_profile_thresholds already set the defaults.
        logger.info(f"ACC calibration started for profile '{self._profile}'")

    def set_cal_state(self, state: Optional[str]):
        """Set the current calibration key: 'INHALING', 'EXHALING', 'HOLDING', or None."""
        if state in (None, "INHALING", "EXHALING", "HOLDING"):
            self._cal_state = state

    def finish_calibration(self):
        """Commit calibration thresholds for the current profile and save."""
        self._calibrating = False
        self._cal_state = None
        self._cal[self._profile] = (self._thresh_in, self._thresh_ex)
        self._save_calibration()
        # Reset detection state
        self.current_phase = None
        self._candidate_phase = None
        self._debounce_count = 0
        logger.info(
            f"ACC calibration finished for '{self._profile}': "
            f"in>{self._thresh_in:.2f}  ex<{self._thresh_ex:.2f}"
        )

    def cancel_calibration(self):
        """Abort calibration without saving."""
        self._calibrating = False
        self._cal_state = None
        self._apply_profile_thresholds()
        logger.info("ACC calibration cancelled.")

    @property
    def is_calibrating(self) -> bool:
        return self._calibrating

    @property
    def cal_state(self) -> Optional[str]:
        return self._cal_state

    @property
    def thresh_in(self) -> float:
        return self._thresh_in

    @property
    def thresh_ex(self) -> float:
        return self._thresh_ex

    def is_calibrated(self, profile: Optional[str] = None) -> bool:
        p = profile or self._profile
        return p in self._cal

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_delta(self) -> float:
        data = np.array(self._z_buf)
        current_smoothed = np.mean(data[-SMOOTHING_SAMPLES:])
        past_smoothed = np.mean(
            data[-(SMOOTHING_SAMPLES + LOOKBACK_SAMPLES): -LOOKBACK_SAMPLES]
        )
        return float(current_smoothed - past_smoothed)

    def _get_predicted_phase(self, delta: float, active: Optional[str]) -> str:
        if active == "INHALING":
            if delta < (self._thresh_in * DROP_FACTOR):
                return "EXHALING" if delta < self._thresh_ex else "HOLDING"
            return "INHALING"
        elif active == "EXHALING":
            if delta > (self._thresh_ex * DROP_FACTOR):
                return "INHALING" if delta > self._thresh_in else "HOLDING"
            return "EXHALING"
        else:  # HOLDING or None
            if delta > self._thresh_in:
                return "INHALING"
            elif delta < self._thresh_ex:
                return "EXHALING"
            return "HOLDING"

    def _update_phase(self, delta: float):
        raw = self._get_predicted_phase(delta, self.current_phase)

        if raw == self.current_phase:
            self._debounce_count = 0
        else:
            if raw == self._candidate_phase:
                self._debounce_count += 1
            else:
                self._candidate_phase = raw
                self._debounce_count = 1

            limit = DEBOUNCE_HOLD if self._candidate_phase == "HOLDING" else DEBOUNCE_BREATH
            if self._debounce_count >= limit:
                prev = self.current_phase
                self.current_phase = self._candidate_phase
                self._debounce_count = 0
                # Track breath rate on INHALING transitions
                if self.current_phase == "INHALING" and prev != "INHALING":
                    now = time.monotonic()
                    self._phase_timestamps.append(now)
                    self._update_breath_rate()

    def _update_breath_rate(self):
        ts = list(self._phase_timestamps)
        if len(ts) < 2:
            return
        intervals = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        avg_interval = float(np.mean(intervals))
        if avg_interval > 0:
            self.current_breath_rate_bpm = 60.0 / avg_interval

    def _recalculate_thresholds(self):
        holds = list(self._cal_data["HOLDING"])
        inhales = list(self._cal_data["INHALING"])
        exhales = list(self._cal_data["EXHALING"])

        noise_floor = float(np.percentile(np.abs(holds), 85)) if holds else 1.0

        if inhales and holds:
            median_in = float(np.median(inhales))
            self._thresh_in = noise_floor + (median_in - noise_floor) * 0.2
        elif inhales:
            self._thresh_in = float(np.median(inhales)) * 0.3
        else:
            self._thresh_in = noise_floor * 1.5

        if exhales and holds:
            median_ex = float(np.median(exhales))
            self._thresh_ex = -noise_floor + (median_ex - (-noise_floor)) * 0.2
        elif exhales:
            self._thresh_ex = float(np.median(exhales)) * 0.3
        else:
            self._thresh_ex = -noise_floor * 1.5

        self._thresh_in = max(1.0, self._thresh_in)
        self._thresh_ex = min(-1.0, self._thresh_ex)

    def _apply_profile_thresholds(self):
        if self._profile in self._cal:
            self._thresh_in, self._thresh_ex = self._cal[self._profile]
        else:
            self._thresh_in = 2.0
            self._thresh_ex = -2.0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_calibration(self):
        if not os.path.exists(_CAL_FILE):
            return
        try:
            with open(_CAL_FILE, "r") as f:
                raw = json.load(f)
            for profile, vals in raw.items():
                if profile in PROFILES and isinstance(vals, list) and len(vals) == 2:
                    self._cal[profile] = (float(vals[0]), float(vals[1]))
            logger.info(f"ACC calibration loaded: {list(self._cal.keys())}")
        except Exception as e:
            logger.error(f"Failed to load ACC calibration: {e}")

    def _save_calibration(self):
        try:
            serialisable = {p: list(v) for p, v in self._cal.items()}
            with open(_CAL_FILE, "w") as f:
                json.dump(serialisable, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save ACC calibration: {e}")
