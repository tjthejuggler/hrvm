"""PPG-based Heart Rate Calculator.

Calculates heart rate from raw PPG (photoplethysmography) data using
bandpass filtering and peak detection. Designed for the Polar Verity Sense
which provides 4-channel PPG data at 135 Hz via SDK mode.

Algorithm:
  1. Collect PPG samples into a sliding window buffer
  2. Subtract ambient channel from LED channels to remove DC offset
  3. Apply bandpass filter (0.5-4 Hz = 30-240 BPM range)
  4. Detect peaks using zero-crossing of first derivative + minimum prominence
  5. Calculate HR from median inter-peak interval

Thread-safe: designed to be called from BLE notification callbacks.
"""

import logging
import threading
import time
from collections import deque
from typing import Optional, List, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# PPG processing constants
PPG_SAMPLE_RATE = 135       # Hz (Polar Verity Sense SDK mode)
PPG_WINDOW_SECONDS = 8      # Seconds of data to keep for HR calculation
PPG_MIN_WINDOW_SECONDS = 4  # Minimum seconds before attempting HR calculation
PPG_UPDATE_INTERVAL = 1.0   # Seconds between HR recalculations

# Heart rate range (BPM)
HR_MIN_BPM = 30
HR_MAX_BPM = 240

# Bandpass filter range (Hz)
BANDPASS_LOW = HR_MIN_BPM / 60.0   # 0.5 Hz
BANDPASS_HIGH = HR_MAX_BPM / 60.0  # 4.0 Hz


def _bandpass_filter(signal: np.ndarray, sample_rate: float,
                     low_hz: float, high_hz: float,
                     order: int = 3) -> np.ndarray:
    """Apply a Butterworth bandpass filter to the signal.

    Uses scipy if available, falls back to simple FFT-based filtering.
    """
    try:
        from scipy.signal import butter, filtfilt
        nyq = sample_rate / 2.0
        low = low_hz / nyq
        high = high_hz / nyq
        # Clamp to valid range
        low = max(low, 0.001)
        high = min(high, 0.999)
        if low >= high:
            return signal
        b, a = butter(order, [low, high], btype='band')
        return filtfilt(b, a, signal)
    except ImportError:
        # Fallback: FFT-based bandpass
        n = len(signal)
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        fft = np.fft.rfft(signal)
        # Zero out frequencies outside the band
        mask = (freqs >= low_hz) & (freqs <= high_hz)
        fft[~mask] = 0
        return np.fft.irfft(fft, n=n)


def _find_peaks_simple(signal: np.ndarray, min_distance: int,
                       min_prominence: float = 0.0) -> List[int]:
    """Simple peak detection without scipy dependency.

    Finds local maxima that are at least min_distance apart and
    have at least min_prominence above their neighbors.
    """
    peaks = []
    n = len(signal)
    if n < 3:
        return peaks

    for i in range(1, n - 1):
        if signal[i] > signal[i - 1] and signal[i] > signal[i + 1]:
            # Check prominence (simple: compare to min of neighbors)
            prom = signal[i] - min(signal[max(0, i - min_distance):i],
                                    default=signal[i])
            prom2 = signal[i] - min(signal[i + 1:min(n, i + min_distance + 1)],
                                     default=signal[i])
            prominence = min(prom, prom2)
            if prominence >= min_prominence:
                # Check minimum distance from last peak
                if not peaks or (i - peaks[-1]) >= min_distance:
                    peaks.append(i)

    return peaks


def _find_peaks(signal: np.ndarray, sample_rate: float) -> List[int]:
    """Find peaks in the filtered PPG signal.

    Uses scipy.signal.find_peaks if available, otherwise falls back
    to simple peak detection.
    """
    # Minimum distance between peaks: corresponds to HR_MAX_BPM
    min_distance = int(sample_rate * 60.0 / HR_MAX_BPM)

    try:
        from scipy.signal import find_peaks
        # Estimate prominence threshold as fraction of signal range
        sig_range = np.ptp(signal)
        if sig_range == 0:
            return []
        min_prominence = sig_range * 0.15  # 15% of range

        peaks, properties = find_peaks(
            signal,
            distance=min_distance,
            prominence=min_prominence,
        )
        return peaks.tolist()
    except ImportError:
        sig_range = np.ptp(signal)
        if sig_range == 0:
            return []
        min_prominence = sig_range * 0.15
        return _find_peaks_simple(signal, min_distance, min_prominence)


class PPGHeartRateCalculator:
    """Calculates heart rate from raw PPG channel data.

    Thread-safe. Feed samples via add_sample(), read HR via get_hr().

    The Polar Verity Sense provides 4 PPG channels:
      - Channel 0: LED 1 (green)
      - Channel 1: LED 2 (green)
      - Channel 2: LED 3 (green/red)
      - Channel 3: Ambient light

    We use Channel 0 (primary green LED) minus Channel 3 (ambient)
    as the PPG signal for HR calculation.
    """

    def __init__(self, sample_rate: float = PPG_SAMPLE_RATE,
                 window_seconds: float = PPG_WINDOW_SECONDS):
        self._sample_rate = sample_rate
        self._window_size = int(sample_rate * window_seconds)
        self._min_samples = int(sample_rate * PPG_MIN_WINDOW_SECONDS)

        # Raw PPG channel buffers (thread-safe via lock)
        self._lock = threading.Lock()
        self._ppg0: deque = deque(maxlen=self._window_size)
        self._ppg1: deque = deque(maxlen=self._window_size)
        self._ppg2: deque = deque(maxlen=self._window_size)
        self._ambient: deque = deque(maxlen=self._window_size)

        # Calculated HR
        self._current_hr: Optional[float] = None
        self._last_calc_time: float = 0.0
        self._hr_confidence: float = 0.0  # 0.0 to 1.0

    def add_sample(self, channels: List[int]):
        """Add a single PPG sample (4 channels).

        Args:
            channels: List of 4 integer values [ppg0, ppg1, ppg2, ambient]
        """
        if len(channels) < 4:
            return

        with self._lock:
            self._ppg0.append(channels[0])
            self._ppg1.append(channels[1])
            self._ppg2.append(channels[2])
            self._ambient.append(channels[3])

    def add_samples(self, samples_list: List[List[int]]):
        """Add multiple PPG samples at once.

        Args:
            samples_list: List of [ppg0, ppg1, ppg2, ambient] lists
        """
        with self._lock:
            for channels in samples_list:
                if len(channels) < 4:
                    continue
                self._ppg0.append(channels[0])
                self._ppg1.append(channels[1])
                self._ppg2.append(channels[2])
                self._ambient.append(channels[3])

    def get_hr(self) -> Optional[float]:
        """Get the current calculated heart rate in BPM.

        Returns None if not enough data or calculation hasn't run yet.
        Automatically triggers recalculation if enough time has passed.
        """
        now = time.time()
        if now - self._last_calc_time >= PPG_UPDATE_INTERVAL:
            self._calculate_hr()
        return self._current_hr

    def get_hr_with_confidence(self) -> Tuple[Optional[float], float]:
        """Get HR and confidence score (0.0-1.0).

        Returns (hr_bpm, confidence) tuple.
        """
        now = time.time()
        if now - self._last_calc_time >= PPG_UPDATE_INTERVAL:
            self._calculate_hr()
        return self._current_hr, self._hr_confidence

    def _calculate_hr(self):
        """Perform HR calculation from buffered PPG data.

        Algorithm:
          1. Get ambient-subtracted PPG signal from primary LED channel
          2. Bandpass filter (0.5-4 Hz)
          3. Find peaks
          4. Calculate HR from median peak-to-peak interval
        """
        self._last_calc_time = time.time()

        with self._lock:
            n = len(self._ppg0)
            if n < self._min_samples:
                return

            # Use primary green LED minus ambient
            ppg = np.array(self._ppg0, dtype=np.float64)
            amb = np.array(self._ambient, dtype=np.float64)

        # Ambient subtraction
        signal = ppg - amb

        # Remove DC offset (detrend)
        signal = signal - np.mean(signal)

        # Skip if signal is flat (no skin contact)
        if np.std(signal) < 1.0:
            self._current_hr = None
            self._hr_confidence = 0.0
            return

        # Bandpass filter
        try:
            filtered = _bandpass_filter(
                signal, self._sample_rate,
                BANDPASS_LOW, BANDPASS_HIGH
            )
        except Exception as e:
            logger.debug(f"PPG bandpass filter error: {e}")
            filtered = signal

        # Find peaks
        peaks = _find_peaks(filtered, self._sample_rate)

        if len(peaks) < 2:
            # Not enough peaks to calculate HR
            self._hr_confidence = 0.0
            return

        # Calculate inter-peak intervals
        intervals = np.diff(peaks) / self._sample_rate  # in seconds

        # Filter out physiologically impossible intervals
        min_interval = 60.0 / HR_MAX_BPM  # 0.25s at 240 BPM
        max_interval = 60.0 / HR_MIN_BPM  # 2.0s at 30 BPM
        valid = intervals[(intervals >= min_interval) & (intervals <= max_interval)]

        if len(valid) < 1:
            self._hr_confidence = 0.0
            return

        # Use median interval for robustness against outliers
        median_interval = np.median(valid)
        hr = 60.0 / median_interval

        # Confidence based on consistency of intervals
        if len(valid) >= 3:
            cv = np.std(valid) / np.mean(valid)  # coefficient of variation
            # Lower CV = more consistent = higher confidence
            self._hr_confidence = max(0.0, min(1.0, 1.0 - cv * 2.0))
        else:
            self._hr_confidence = 0.3  # Low confidence with few peaks

        # Clamp to valid range
        if HR_MIN_BPM <= hr <= HR_MAX_BPM:
            self._current_hr = round(hr, 1)
        else:
            self._hr_confidence = 0.0

    def reset(self):
        """Clear all buffered data and reset HR calculation."""
        with self._lock:
            self._ppg0.clear()
            self._ppg1.clear()
            self._ppg2.clear()
            self._ambient.clear()
        self._current_hr = None
        self._last_calc_time = 0.0
        self._hr_confidence = 0.0

    @property
    def sample_count(self) -> int:
        """Number of samples currently in the buffer."""
        with self._lock:
            return len(self._ppg0)

    @property
    def has_enough_data(self) -> bool:
        """Whether enough data has been collected for HR calculation."""
        with self._lock:
            return len(self._ppg0) >= self._min_samples
