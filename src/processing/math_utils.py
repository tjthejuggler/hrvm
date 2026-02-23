import numpy as np
from numba import njit
from scipy.interpolate import CubicSpline, PchipInterpolator
from scipy.signal import periodogram, get_window, find_peaks as scipy_find_peaks

@njit
def calculate_rmssd(rr_intervals):
    """
    Calculate the Root Mean Square of Successive Differences (RMSSD).
    
    Args:
        rr_intervals (np.array): Array of RR intervals in milliseconds.
        
    Returns:
        float: RMSSD value.
    """
    if len(rr_intervals) < 2:
        return 0.0
    
    diffs = np.diff(rr_intervals)
    squared_diffs = diffs ** 2
    mean_squared_diff = np.mean(squared_diffs)
    return np.sqrt(mean_squared_diff)

@njit
def calculate_sdnn(rr_intervals):
    """
    Calculate the Standard Deviation of NN intervals (SDNN).
    
    Args:
        rr_intervals (np.array): Array of RR intervals in milliseconds.
        
    Returns:
        float: SDNN value.
    """
    if len(rr_intervals) < 2:
        return 0.0
    return np.std(rr_intervals)

def calculate_metrics(rr_intervals):
    """
    Wrapper to calculate both RMSSD and SDNN.
    """
    return calculate_rmssd(rr_intervals), calculate_sdnn(rr_intervals)

def reject_rr_artifacts(rr_intervals, timestamps=None, abs_min=333.0, abs_max=1500.0, rel_change=0.20):
    """
    Reject physiologically implausible RR intervals before interpolation.

    Two-stage filter:
      1. Absolute bounds: keep only intervals in [abs_min, abs_max] ms
         (333 ms = 180 BPM, 1500 ms = 40 BPM — tighter than the BLE validity
         check of 300–2000 ms, which still admits missed-beat artefacts).
      2. Relative bounds: reject any interval that differs from its predecessor
         by more than rel_change (default 20 %) — the standard ectopic-beat
         rejection criterion used in clinical HRV analysis.

    Args:
        rr_intervals (list/np.array): RR intervals in milliseconds.
        timestamps (list/np.array | None): Corresponding timestamps. If provided,
            the filtered timestamps are returned alongside the filtered intervals.
        abs_min (float): Minimum valid RR in ms (default 333 ms = 180 BPM).
        abs_max (float): Maximum valid RR in ms (default 1500 ms = 40 BPM).
        rel_change (float): Maximum allowed fractional change between consecutive
            RR intervals (default 0.20 = 20 %).

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

    # Stage 2 — relative change between consecutive beats
    rel_mask = np.ones(len(rr), dtype=bool)
    for i in range(1, len(rr)):
        if abs(rr[i] - rr[i - 1]) / rr[i - 1] > rel_change:
            rel_mask[i] = False

    rr = rr[rel_mask]
    if ts is not None:
        ts = ts[rel_mask]

    return rr.tolist(), (ts.tolist() if ts is not None else [])


def interpolate_hr_stream(timestamps, nn_intervals, current_time, window_size=64.0, fs=4.0):
    """
    Interpolate discrete NN intervals into a continuous Heart Rate stream.

    The rolling window is 64 seconds (256 samples at 4 Hz) — a power-of-2
    length that gives the FFT its optimal performance in calculate_coherence_score().

    Args:
        timestamps (list/np.array): Timestamps of NN intervals.
        nn_intervals (list/np.array): NN intervals in milliseconds.
        current_time (float): Current system time.
        window_size (float): Time window in seconds to interpolate over (default 64 s).
        fs (float): Sampling frequency in Hz.

    Returns:
        tuple: (interpolated_times, interpolated_hr)
    """
    if len(timestamps) < 4:
        return np.array([]), np.array([])

    # Artifact-reject before interpolation (Stage 1 + 2)
    clean_nn, clean_ts = reject_rr_artifacts(nn_intervals, timestamps)
    if len(clean_nn) < 4:
        return np.array([]), np.array([])

    # Convert to numpy arrays
    ts = np.array(clean_ts)
    nn = np.array(clean_nn)

    # Calculate Instantaneous Heart Rate (IHR)
    ihr = 60000.0 / nn

    # Create uniform time grid
    start_time = current_time - window_size
    num_points = int(window_size * fs)  # 256 at default settings
    x_new = np.linspace(start_time, current_time, num_points)

    # Filter data within range (plus a small buffer for interpolation context)
    mask = (ts >= start_time - 2.0) & (ts <= current_time + 0.5)
    ts_window = ts[mask]
    ihr_window = ihr[mask]

    if len(ts_window) < 4:
        return np.array([]), np.array([])

    # Sort by time just in case
    sort_idx = np.argsort(ts_window)
    ts_window = ts_window[sort_idx]
    ihr_window = ihr_window[sort_idx]

    # Remove duplicates
    unique_ts, unique_indices = np.unique(ts_window, return_index=True)
    unique_ihr = ihr_window[unique_indices]

    if len(unique_ts) < 4:
        return np.array([]), np.array([])

    try:
        # Use PchipInterpolator for monotonic cubic interpolation to avoid Runge phenomenon
        interpolator = PchipInterpolator(unique_ts, unique_ihr)
        y_new = interpolator(x_new)

        # Clamp to biological limits
        y_new = np.clip(y_new, 40.0, 180.0)

        return x_new, y_new
    except Exception:
        return np.array([]), np.array([])

def calculate_coherence_score(interpolated_hr, fs=4.0, target_freq=0.1):
    """
    Calculate the coherence score based on the Power Spectral Density.

    Algorithm:
      1. Require a full 256-sample (64 s at 4 Hz) window — power-of-2 length
         for optimal FFT performance.
      2. Linear detrend to remove slow HR drift.
      3. Apply a Hanning window to reduce spectral leakage.
      4. Compute a standard periodogram (scipy.signal.periodogram).
         Note: welch() with nperseg=N is mathematically identical to a
         periodogram; using periodogram() directly is cleaner and more honest.
      5. Integrate (sum) all PSD bins within the ±0.03 Hz target band rather
         than taking the single-bin max.  At 4 Hz / 256 samples the frequency
         resolution is ~0.0156 Hz/bin, so the target band spans ~4 bins; summing
         captures energy that is smeared across adjacent bins due to slight
         breathing-rate variation.
      6. Coherence ratio = band_power / (total_power - band_power).
      7. Normalize to 0–100 via tanh(ratio × 2.0).

    Args:
        interpolated_hr (np.array): Interpolated Heart Rate data (64 s window,
            256 samples at 4 Hz).
        fs (float): Sampling frequency (default 4.0 Hz).
        target_freq (float): Target breathing frequency in Hz
            (e.g., 0.1 Hz for 6 BPM).

    Returns:
        float: Coherence score (0–100).
    """
    # Require a full 256-sample window (64 s × 4 Hz)
    if len(interpolated_hr) < int(64 * fs):
        return 0.0

    # Detrend — remove linear HR drift over the window
    times = np.arange(len(interpolated_hr))
    detrended = interpolated_hr - np.polyval(np.polyfit(times, interpolated_hr, 1), times)

    # Apply Hanning window to reduce spectral leakage
    window = get_window('hann', len(detrended))
    windowed_data = detrended * window

    # Compute periodogram (equivalent to single-segment Welch, but explicit)
    freqs, psd = periodogram(windowed_data, fs)

    # Define frequency bands
    total_power_mask = (freqs >= 0.0033) & (freqs <= 0.4)
    target_band_mask = (freqs >= target_freq - 0.03) & (freqs <= target_freq + 0.03)

    total_power = np.sum(psd[total_power_mask])
    # Sum (integrate) all bins in the target band — captures energy smeared
    # across adjacent bins when breathing rate doesn't align exactly with a bin.
    band_power = np.sum(psd[target_band_mask]) if np.any(target_band_mask) else 0.0

    if total_power == 0 or band_power == 0:
        return 0.0

    # Coherence ratio: band power vs. everything else
    denominator = total_power - band_power
    if denominator <= 0:
        ratio = 10.0  # All power is in the target band — maximum coherence
    else:
        ratio = band_power / denominator

    # Normalize to 0–100 using tanh for smooth, bounded output
    score = np.tanh(ratio * 2.0) * 100.0

    return score

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
    
    # LF Power (0.04 - 0.15 Hz) - Simplified estimation
    # For full accuracy we'd use the PSD method above, but let's reuse logic if needed
    # For now, just return amplitude as primary metric per spec
    
    return 0.0, amplitude # Placeholder for LF power if strictly needed later

@njit
def pan_tompkins_energy(signal, fs):
    """
    Calculate Pan-Tompkins energy for QRS detection.
    Steps: Derivative -> Square -> Moving Window Integration
    """
    # 1. Derivative
    # H(z) = (1/8T)(-z^-2 - 2z^-1 + 2z^1 + z^2)
    # Simplified 5-point derivative: y[n] = (2x[n] + x[n-1] - x[n-3] - 2x[n-4]) / 8
    # Or simpler difference: y[n] = x[n] - x[n-1]
    
    # Using numpy gradient for simplicity in Python, or simple diff
    diff_sig = np.diff(signal)
    
    # 2. Square
    squared = diff_sig ** 2
    
    # 3. Moving Window Integration
    # Window width ~150ms. At 130Hz -> ~20 samples
    window_width = int(0.15 * fs)
    integrated = np.convolve(squared, np.ones(window_width)/window_width, mode='same')
    
    return integrated

def find_peaks(energy, threshold, min_dist):
    """
    Find peaks in energy signal above threshold with minimum distance.
    Wrapper around scipy.signal.find_peaks for now.
    """
    peaks, _ = scipy_find_peaks(energy, height=threshold, distance=min_dist)
    return peaks

def reject_artifacts(rr_intervals, threshold_sigma=3.0):
    """
    Reject RR intervals that deviate significantly from the mean/median.
    Simple outlier detection.
    """
    if len(rr_intervals) < 3:
        return rr_intervals, 1.0
        
    rr_arr = np.array(rr_intervals)
    median_rr = np.median(rr_arr)
    std_rr = np.std(rr_arr)
    
    # Filter
    mask = np.abs(rr_arr - median_rr) <= (threshold_sigma * std_rr)
    clean_rr = rr_arr[mask]
    
    quality_score = len(clean_rr) / len(rr_arr)
    
    return clean_rr.tolist(), quality_score
