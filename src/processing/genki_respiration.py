"""
Genki Wave ACC-based Respiration Detection Engine.

Detects breathing phase (INHALING, EXHALING, HOLDING) in real-time from
Genki Wave 3-axis accelerometer data.

Key difference from the H10 engine:
  - Receives 3-axis (X, Y, Z) samples simultaneously
  - During calibration, records deltas for ALL three axes
  - On finish_calibration(), auto-selects the axis with the best
    inhale/exhale separation (highest |median_in - median_ex|)
  - Stores chosen axis + thresholds in genki_breath_cal.json (separate
    from the H10 acc_breath_cal.json)

Calibration and profile logic is otherwise identical to AccRespirationEngine
(same 6 profiles, same no-hold variants, same weighted-percentile thresholds).

Usage:
    engine = GenkiRespirationEngine()
    engine.feed_samples(wave_samples)   # list of WaveSample
    phase = engine.current_phase        # "INHALING" | "EXHALING" | "HOLDING" | None
    bpm   = engine.current_breath_rate_bpm
"""
import json
import logging
import os
import time
from collections import deque
from typing import Optional, Tuple

import numpy as np

# Re-use profile constants from the H10 engine — same 6 profiles
from src.processing.acc_respiration import (
    PROFILES, _NO_HOLD_PROFILES,
    SMOOTHING_SAMPLES, LOOKBACK_SAMPLES,
    DROP_FACTOR, DEBOUNCE_BREATH, DEBOUNCE_HOLD,
    _weighted_percentile_standalone,
)

logger = logging.getLogger(__name__)

# Genki Wave streams at ~100 Hz
GENKI_SAMPLING_RATE = 100
GENKI_BUFFER_SEC = 2
GENKI_BUFFER_SIZE = GENKI_SAMPLING_RATE * GENKI_BUFFER_SEC

_GENKI_CAL_FILE = "genki_breath_cal.json"

# Axis index names for display
_AXIS_NAMES = {0: "X", 1: "Y", 2: "Z"}


class GenkiRespirationEngine:
    """Real-time Genki Wave ACC-based respiration phase detector.

    Calibration records all 3 axes simultaneously and auto-selects the
    axis with the best inhale/exhale separation at finish time.
    """

    def __init__(self):
        # Rolling buffers for each axis (100 Hz × 2 s)
        self._x_buf: deque = deque(maxlen=GENKI_BUFFER_SIZE)
        self._y_buf: deque = deque(maxlen=GENKI_BUFFER_SIZE)
        self._z_buf: deque = deque(maxlen=GENKI_BUFFER_SIZE)

        # Per-profile calibration: {profile: (axis_idx, thresh_in, thresh_ex)}
        self._cal: dict = {}
        self._load_calibration()

        # Active profile
        self._profile: str = PROFILES[0]

        # Active axis (0=X, 1=Y, 2=Z) — set from calibration or default
        self._active_axis: int = 2  # default Z

        # Detection state
        self.current_phase: Optional[str] = None
        self._candidate_phase: Optional[str] = None
        self._debounce_count: int = 0
        self.predicted_phase: Optional[str] = None

        # Breath-rate estimation
        self._phase_timestamps: deque = deque(maxlen=20)
        self.current_breath_rate_bpm: Optional[float] = None

        # Calibration session state
        self._calibrating: bool = False
        self._cal_state: Optional[str] = None
        # Store (delta_x, delta_y, delta_z) tuples per state
        self._cal_data: dict = {
            "INHALING": deque(maxlen=800),
            "EXHALING": deque(maxlen=800),
            "HOLDING":  deque(maxlen=800),
        }
        self._thresh_in: float = 2.0
        self._thresh_ex: float = -2.0
        self._cal_weight_decay: float = 0.995

        # Apply loaded thresholds for default profile
        self._apply_profile_thresholds()

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

    @property
    def is_no_hold_profile(self) -> bool:
        return self._profile in _NO_HOLD_PROFILES

    @property
    def active_axis_name(self) -> str:
        return _AXIS_NAMES.get(self._active_axis, "Z")

    def feed_samples(self, wave_samples):
        """Feed a list of WaveSample objects into the engine.

        Each WaveSample must have an .acc attribute of (x, y, z).
        """
        for s in wave_samples:
            x, y, z = s.acc
            self._x_buf.append(x)
            self._y_buf.append(y)
            self._z_buf.append(z)

        if len(self._x_buf) < GENKI_BUFFER_SIZE:
            return

        dx, dy, dz = self._compute_deltas()
        delta = self._pick_delta(dx, dy, dz)

        self.predicted_phase = self._get_predicted_phase(delta, self.current_phase)

        if self._calibrating:
            if self._cal_state is not None:
                # Store all 3 axis deltas for later axis selection
                self._cal_data[self._cal_state].append((dx, dy, dz))
                self._recalculate_thresholds()
        else:
            self._update_phase(delta)

    def get_delta(self) -> Optional[float]:
        """Return the current delta on the active axis (for debug display)."""
        if len(self._x_buf) < GENKI_BUFFER_SIZE:
            return None
        dx, dy, dz = self._compute_deltas()
        return self._pick_delta(dx, dy, dz)

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def start_calibration(self):
        """Begin a calibration session for the current profile."""
        self._calibrating = True
        self._cal_state = None
        for key in self._cal_data:
            self._cal_data[key].clear()
        logger.info(f"Genki calibration started for profile '{self._profile}'")

    def set_cal_state(self, state: Optional[str]):
        """Set the current calibration key. HOLDING ignored for no-hold profiles."""
        if self.is_no_hold_profile and state == "HOLDING":
            return
        if state in (None, "INHALING", "EXHALING", "HOLDING"):
            self._cal_state = state

    def finish_calibration(self):
        """Auto-select best axis, commit thresholds, and save."""
        self._calibrating = False
        self._cal_state = None

        # Select the axis with the best inhale/exhale separation
        best_axis = self._select_best_axis()
        self._active_axis = best_axis

        # Recalculate thresholds using the chosen axis
        self._recalculate_thresholds()

        self._cal[self._profile] = (self._active_axis, self._thresh_in, self._thresh_ex)
        self._save_calibration()

        # Reset detection state
        self.current_phase = None
        self._candidate_phase = None
        self._debounce_count = 0
        logger.info(
            f"Genki calibration finished for '{self._profile}': "
            f"axis={self.active_axis_name}  in>{self._thresh_in:.2f}  ex<{self._thresh_ex:.2f}"
        )

    def cancel_calibration(self):
        """Abort calibration without saving."""
        self._calibrating = False
        self._cal_state = None
        self._apply_profile_thresholds()
        logger.info("Genki calibration cancelled.")

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

    def _compute_deltas(self) -> Tuple[float, float, float]:
        """Compute smoothed delta for each axis."""
        def _delta(buf):
            data = np.array(buf)
            cur  = np.mean(data[-SMOOTHING_SAMPLES:])
            past = np.mean(data[-(SMOOTHING_SAMPLES + LOOKBACK_SAMPLES): -LOOKBACK_SAMPLES])
            return float(cur - past)

        return _delta(self._x_buf), _delta(self._y_buf), _delta(self._z_buf)

    def _pick_delta(self, dx: float, dy: float, dz: float) -> float:
        """Return the delta for the currently active axis."""
        return (dx, dy, dz)[self._active_axis]

    def _select_best_axis(self) -> int:
        """Choose the axis with the highest |median_inhale - median_exhale| separation."""
        inhales = list(self._cal_data["INHALING"])
        exhales = list(self._cal_data["EXHALING"])

        if not inhales or not exhales:
            return self._active_axis  # keep current if no data

        best_axis = 0
        best_sep  = -1.0

        for axis in range(3):
            in_vals  = [t[axis] for t in inhales]
            ex_vals  = [t[axis] for t in exhales]
            med_in   = float(np.median(in_vals))
            med_ex   = float(np.median(ex_vals))
            sep      = abs(med_in - med_ex)
            if sep > best_sep:
                best_sep  = sep
                best_axis = axis

        logger.info(
            f"Genki axis selection: best={_AXIS_NAMES[best_axis]} "
            f"(separation={best_sep:.2f})"
        )
        return best_axis

    def _get_predicted_phase(self, delta: float, active: Optional[str]) -> str:
        no_hold = self.is_no_hold_profile

        if active == "INHALING":
            if delta < (self._thresh_in * DROP_FACTOR):
                if delta < self._thresh_ex:
                    return "EXHALING"
                return "INHALING" if no_hold else "HOLDING"
            return "INHALING"

        elif active == "EXHALING":
            if delta > (self._thresh_ex * DROP_FACTOR):
                if delta > self._thresh_in:
                    return "INHALING"
                return "EXHALING" if no_hold else "HOLDING"
            return "EXHALING"

        else:
            if delta > self._thresh_in:
                return "INHALING"
            elif delta < self._thresh_ex:
                return "EXHALING"
            return "INHALING" if no_hold else "HOLDING"

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
                if self.current_phase == "INHALING" and prev != "INHALING":
                    self._phase_timestamps.append(time.monotonic())
                    self._update_breath_rate()

    def _update_breath_rate(self):
        ts = list(self._phase_timestamps)
        if len(ts) < 2:
            return
        intervals = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        avg = float(np.mean(intervals))
        if avg > 0:
            self.current_breath_rate_bpm = 60.0 / avg

    def _recalculate_thresholds(self):
        """Recalculate thresholds for the active axis using weighted percentiles."""
        axis = self._active_axis

        def _extract(state):
            raw = list(self._cal_data[state])
            if not raw:
                return []
            # Each entry is a (dx, dy, dz) tuple
            return [t[axis] for t in raw]

        holds   = _extract("HOLDING")
        inhales = _extract("INHALING")
        exhales = _extract("EXHALING")

        if holds:
            noise_floor = float(_weighted_percentile_standalone(
                np.abs(holds), 85, self._cal_weight_decay))
        else:
            all_data = inhales + exhales
            if all_data:
                noise_floor = max(0.5, float(np.percentile(np.abs(all_data), 15)))
            else:
                noise_floor = 1.0

        if inhales:
            peak_in = float(_weighted_percentile_standalone(
                inhales, 75, self._cal_weight_decay))
            self._thresh_in = noise_floor + (peak_in - noise_floor) * 0.5
        else:
            self._thresh_in = noise_floor * 1.5

        if exhales:
            peak_ex = float(_weighted_percentile_standalone(
                exhales, 25, self._cal_weight_decay))
            self._thresh_ex = -noise_floor + (peak_ex - (-noise_floor)) * 0.5
        else:
            self._thresh_ex = -noise_floor * 1.5

        self._thresh_in = max(0.5, self._thresh_in)
        self._thresh_ex = min(-0.5, self._thresh_ex)

    def _apply_profile_thresholds(self):
        if self._profile in self._cal:
            axis, ti, te = self._cal[self._profile]
            self._active_axis = int(axis)
            self._thresh_in   = float(ti)
            self._thresh_ex   = float(te)
        else:
            self._active_axis = 2  # default Z
            self._thresh_in   = 2.0
            self._thresh_ex   = -2.0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_calibration(self):
        if not os.path.exists(_GENKI_CAL_FILE):
            return
        try:
            with open(_GENKI_CAL_FILE, "r") as f:
                raw = json.load(f)
            for profile, vals in raw.items():
                if profile in PROFILES and isinstance(vals, list) and len(vals) == 3:
                    self._cal[profile] = (int(vals[0]), float(vals[1]), float(vals[2]))
            logger.info(f"Genki calibration loaded: {list(self._cal.keys())}")
        except Exception as e:
            logger.error(f"Failed to load Genki calibration: {e}")

    def _save_calibration(self):
        try:
            serialisable = {p: list(v) for p, v in self._cal.items()}
            with open(_GENKI_CAL_FILE, "w") as f:
                json.dump(serialisable, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save Genki calibration: {e}")
