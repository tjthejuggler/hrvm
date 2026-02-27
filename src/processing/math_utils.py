import numpy as np
from numba import njit
from scipy.interpolate import CubicSpline, PchipInterpolator
from scipy.signal import periodogram, get_window, find_peaks as scipy_find_peaks


def calculate_rmssd(rr_intervals: np.ndarray) -> float:
    """
    Calculate the Root Mean Square of Successive Differences (RMSSD).

    Artifact-aware implementation: differences are computed on the full
    (pre-rejection) sequence, then any difference that spans an artifact
    beat is dropped.  This preserves temporal adjacency — non-adjacent
    beats are never paired.

    Args:
        rr_intervals (np.array): Array of RR intervals in milliseconds.
            Pass the RAW (un-filtered) sequence; artifact masking is
            applied internally via the artifact_mask parameter.

    Returns:
        float: RMSSD value in milliseconds.
    """
    if len(rr_intervals) < 2:
        return 0.0

    diffs = np.diff(rr_intervals)
    squared_diffs = diffs ** 2
    mean_squared_diff = np.mean(squared_diffs)
    return float(np.sqrt(mean_squared_diff))


def calculate_rmssd_artifact_aware(rr_intervals: np.ndarray,
                                   artifact_mask: np.ndarray) -> float:
    """
    RMSSD that respects artifact positions.

    Computes successive differences on the full array, then drops any
    difference where *either* of the two beats involved is flagged as an
    artifact.  This avoids the "missing beat" flaw where deleting an
    artifact causes two non-adjacent beats to be paired.

    Args:
        rr_intervals (np.array): Full RR interval array in milliseconds
            (including artifact beats — do NOT pre-delete them).
        artifact_mask (np.array): Boolean array, same length as
            rr_intervals.  True = valid beat, False = artifact.

    Returns:
        float: RMSSD value in milliseconds, or 0.0 if fewer than 2
               valid differences remain.
    """
    if len(rr_intervals) < 2:
        return 0.0

    diffs = np.diff(rr_intervals)

    # A difference is valid only when BOTH beats are valid
    diff_mask = artifact_mask[:-1] & artifact_mask[1:]

    valid_diffs = diffs[diff_mask]
    if len(valid_diffs) < 1:
        return 0.0

    return float(np.sqrt(np.mean(valid_diffs ** 2)))


def calculate_sdnn(rr_intervals: np.ndarray) -> float:
    """
    Calculate the Standard Deviation of NN intervals (SDNN).

    Uses sample standard deviation (ddof=1, divides by N-1) per the
    Task Force of the European Society of Cardiology (1996) standard.

    NOTE: SDNN is only clinically valid over a minimum 5-minute
    recording.  Callers should ensure the input spans ≥ 300 seconds
    before trusting this value.

    Args:
        rr_intervals (np.array): Array of RR intervals in milliseconds.

    Returns:
        float: SDNN value in milliseconds.
    """
    if len(rr_intervals) < 2:
        return 0.0
    return float(np.std(rr_intervals, ddof=1))


def calculate_metrics(rr_intervals: np.ndarray,
                      artifact_mask: np.ndarray = None):
    """
    Wrapper to calculate both RMSSD and SDNN.

    Args:
        rr_intervals: RR intervals in milliseconds.
        artifact_mask: Optional boolean mask (True = valid).  When
            provided, RMSSD uses the artifact-aware algorithm that
            avoids pairing non-adjacent beats.

    Returns:
        tuple: (rmssd, sdnn) both in milliseconds.
    """
    if artifact_mask is not None:
        rmssd = calculate_rmssd_artifact_aware(rr_intervals, artifact_mask)
    else:
        rmssd = calculate_rmssd(rr_intervals)
    sdnn = calculate_sdnn(rr_intervals)
    return rmssd, sdnn


def reject_rr_artifacts(rr_intervals, timestamps=None,
                        abs_min=333.0, abs_max=1500.0, rel_change=0.20,
                        median_window=9):
    """
    Reject physiologically implausible RR intervals.

    Two-stage filter:
      1. Absolute bounds: keep only intervals in [abs_min, abs_max] ms
         (333 ms = 180 BPM, 1500 ms = 40 BPM).
      2. Relative bounds: reject any interval that differs from the
         rolling median of the last `median_window` *valid* beats by
         more than rel_change (default 20 %).  Using a rolling median
         instead of the strict i-1 neighbour prevents the "domino
         effect" where a single ectopic beat causes the following
         normal beat to also be rejected.

    Args:
        rr_intervals (list/np.array): RR intervals in milliseconds.
        timestamps (list/np.array | None): Corresponding timestamps.
        abs_min (float): Minimum valid RR in ms (default 333 ms).
        abs_max (float): Maximum valid RR in ms (default 1500 ms).
        rel_change (float): Maximum allowed fractional change from the
            rolling median (default 0.20 = 20 %).
        median_window (int): Number of recent valid beats used to
            compute the rolling reference median (default 9).

    Returns:
        tuple: (clean_rr, clean_ts) — both as lists.
               clean_ts is an empty list when timestamps is None.
    """
    rr = np.array(rr_intervals, dtype=float)
    ts = np.array(timestamps, dtype=float) if timestamps is not None else None

    # Stage 1 — absolute bounds
    abs_mask = (rr >= abs_min) & (rr <= abs_max)
    rr = rr[abs_mask]
    if ts is not None:
        ts = ts[abs_mask]

    if len(rr) < 2:
        return rr.tolist(), (ts.tolist() if ts is not None else [])

    # Stage 2 — relative change vs. rolling median of recent valid beats
    rel_mask = np.ones(len(rr), dtype=bool)
    valid_history = []  # sliding window of accepted RR values

    for i in range(len(rr)):
        if len(valid_history) == 0:
            # No history yet — accept the first beat unconditionally
            valid_history.append(rr[i])
            continue

        reference = np.median(valid_history[-median_window:])
        if abs(rr[i] - reference) / reference > rel_change:
            rel_mask[i] = False
        else:
            valid_history.append(rr[i])

    rr = rr[rel_mask]
    if ts is not None:
        ts = ts[rel_mask]

    return rr.tolist(), (ts.tolist() if ts is not None else [])


def interpolate_rr_stream(timestamps, nn_intervals, current_time,
                          window_size=64.0, fs=4.0):
    """
    Interpolate discrete NN intervals onto a uniform time grid.

    Returns the interpolated RR intervals in **milliseconds** (not BPM).
    This is the correct input for frequency-domain HRV analysis: the
    PSD of RR intervals in ms yields power in ms²/Hz, matching the
    clinical standard (Task Force 1996).

    The rolling window is 64 seconds (256 samples at 4 Hz) — a
    power-of-2 length that gives the FFT its optimal performance in
    calculate_coherence_score().

    Args:
        timestamps (list/np.array): Timestamps of NN intervals.
        nn_intervals (list/np.array): NN intervals in milliseconds.
        current_time (float): Current system time.
        window_size (float): Time window in seconds (default 64 s).
        fs (float): Sampling frequency in Hz (default 4 Hz).

    Returns:
        tuple: (interpolated_times, interpolated_rr_ms)
               Both are empty arrays when there is insufficient data.
    """
    if len(timestamps) < 4:
        return np.array([]), np.array([])

    # Artifact-reject before interpolation
    clean_nn, clean_ts = reject_rr_artifacts(nn_intervals, timestamps)
    if len(clean_nn) < 4:
        return np.array([]), np.array([])

    ts = np.array(clean_ts)
    nn = np.array(clean_nn)  # RR intervals in ms

    # Create uniform time grid
    start_time = current_time - window_size
    num_points = int(window_size * fs)  # 256 at default settings
    x_new = np.linspace(start_time, current_time, num_points)

    # Filter data within range (plus a small buffer for interpolation context)
    mask = (ts >= start_time - 2.0) & (ts <= current_time + 0.5)
    ts_window = ts[mask]
    nn_window = nn[mask]  # still in ms

    if len(ts_window) < 4:
        return np.array([]), np.array([])

    # Sort by time just in case
    sort_idx = np.argsort(ts_window)
    ts_window = ts_window[sort_idx]
    nn_window = nn_window[sort_idx]

    # Remove duplicates
    unique_ts, unique_indices = np.unique(ts_window, return_index=True)
    unique_nn = nn_window[unique_indices]

    if len(unique_ts) < 4:
        return np.array([]), np.array([])

    try:
        # PchipInterpolator: monotonic cubic, avoids Runge phenomenon
        interpolator = PchipInterpolator(unique_ts, unique_nn)
        y_new = interpolator(x_new)

        # Clamp to biological limits (333–1500 ms = 40–180 BPM)
        y_new = np.clip(y_new, 333.0, 1500.0)

        return x_new, y_new
    except Exception:
        return np.array([]), np.array([])


def interpolate_hr_stream(timestamps, nn_intervals, current_time,
                          window_size=64.0, fs=4.0):
    """
    Interpolate discrete NN intervals into a continuous Heart Rate stream.

    Returns instantaneous HR in **BPM** for display purposes only.
    For frequency-domain HRV analysis use interpolate_rr_stream() instead.

    Args:
        timestamps (list/np.array): Timestamps of NN intervals.
        nn_intervals (list/np.array): NN intervals in milliseconds.
        current_time (float): Current system time.
        window_size (float): Time window in seconds (default 64 s).
        fs (float): Sampling frequency in Hz (default 4 Hz).

    Returns:
        tuple: (interpolated_times, interpolated_hr_bpm)
    """
    x_new, y_rr = interpolate_rr_stream(timestamps, nn_intervals,
                                        current_time, window_size, fs)
    if len(y_rr) == 0:
        return np.array([]), np.array([])

    # Convert RR (ms) → IHR (BPM) for display
    y_hr = 60000.0 / y_rr
    y_hr = np.clip(y_hr, 40.0, 180.0)
    return x_new, y_hr


def calculate_coherence_score(interpolated_rr_ms, fs=4.0, target_freq=0.1):
    """
    Calculate the coherence score based on the Power Spectral Density
    of the interpolated RR interval series (ms).

    Algorithm:
      1. Require a full 256-sample (64 s at 4 Hz) window.
      2. Linear detrend to remove slow RR drift.
      3. Apply a Hanning window to reduce spectral leakage.
      4. Compute a standard periodogram (scipy.signal.periodogram).
      5. Total power floor is 0.015 Hz — the lowest frequency resolvable
         in a 64-second window (1/64 ≈ 0.0156 Hz).  Using 0.0033 Hz
         (VLF) is physically impossible with this window length.
      6. Integrate all PSD bins within the ±0.03 Hz target band.
      7. Coherence ratio = band_power / (total_power - band_power),
         protected against zero-division by a small epsilon.
      8. Normalize to 0–100 via tanh(ratio × 2.0).

    Args:
        interpolated_rr_ms (np.array): Interpolated RR intervals in ms
            (64 s window, 256 samples at 4 Hz).  Must be RR in ms, NOT
            heart rate in BPM.
        fs (float): Sampling frequency (default 4.0 Hz).
        target_freq (float): Target breathing frequency in Hz
            (e.g., 0.1 Hz for 6 BPM).

    Returns:
        float: Coherence score (0–100).
    """
    # Require a full 256-sample window (64 s × 4 Hz)
    if len(interpolated_rr_ms) < int(64 * fs):
        return 0.0

    # Detrend — remove linear RR drift over the window
    times = np.arange(len(interpolated_rr_ms))
    detrended = interpolated_rr_ms - np.polyval(
        np.polyfit(times, interpolated_rr_ms, 1), times
    )

    # Apply Hanning window to reduce spectral leakage
    window = get_window('hann', len(detrended))
    windowed_data = detrended * window

    # Compute periodogram
    freqs, psd = periodogram(windowed_data, fs)

    # Total power floor: 0.015 Hz is the lowest frequency resolvable
    # in a 64-second window (1/64 ≈ 0.0156 Hz).  0.0033 Hz (VLF) is
    # physically impossible to measure with this window length.
    total_power_mask = (freqs >= 0.015) & (freqs <= 0.4)
    target_band_mask = (freqs >= target_freq - 0.03) & (freqs <= target_freq + 0.03)

    total_power = np.sum(psd[total_power_mask])
    band_power = np.sum(psd[target_band_mask]) if np.any(target_band_mask) else 0.0

    if total_power == 0 or band_power == 0:
        return 0.0

    # Coherence ratio: band power vs. everything else.
    # Protected against zero-division with epsilon (occurs when all power
    # is in the target band — perfect resonance state).
    denominator = max(total_power - band_power, 1e-9)
    ratio = band_power / denominator

    # Normalize to 0–100 using tanh for smooth, bounded output
    score = np.tanh(ratio * 2.0) * 100.0

    return float(score)


def calculate_resonance_metrics(interpolated_hr):
    """
    Calculate metrics for Resonance Frequency Assessment.

    Args:
        interpolated_hr (np.array): Interpolated HR data for the segment.

    Returns:
        tuple: (lf_power, max_min_amplitude)
    """
    if len(interpolated_hr) == 0:
        return 0.0, 0.0

    # Max-Min Amplitude
    amplitude = np.max(interpolated_hr) - np.min(interpolated_hr)

    return 0.0, amplitude


@njit
def pan_tompkins_energy(signal, fs):
    """
    Calculate Pan-Tompkins energy for QRS detection.
    Steps: Derivative -> Square -> Moving Window Integration
    """
    diff_sig = np.diff(signal)
    squared = diff_sig ** 2

    # Window width ~150ms. At 130Hz -> ~20 samples
    window_width = int(0.15 * fs)
    integrated = np.convolve(squared, np.ones(window_width) / window_width,
                             mode='same')

    return integrated


def find_peaks(energy, threshold, min_dist):
    """
    Find peaks in energy signal above threshold with minimum distance.
    Wrapper around scipy.signal.find_peaks.
    """
    peaks, _ = scipy_find_peaks(energy, height=threshold, distance=min_dist)
    return peaks


def reject_artifacts(rr_intervals, threshold_sigma=3.0):
    """
    Reject RR intervals that deviate significantly from the median.
    Simple outlier detection used for legacy/ECG paths.
    """
    if len(rr_intervals) < 3:
        return rr_intervals, 1.0

    rr_arr = np.array(rr_intervals)
    median_rr = np.median(rr_arr)
    std_rr = np.std(rr_arr, ddof=1)

    mask = np.abs(rr_arr - median_rr) <= (threshold_sigma * std_rr)
    clean_rr = rr_arr[mask]

    quality_score = len(clean_rr) / len(rr_arr)

    return clean_rr.tolist(), quality_score
