import numpy as np
from numba import njit
from scipy.interpolate import CubicSpline, PchipInterpolator
from scipy.signal import welch, get_window, find_peaks as scipy_find_peaks

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

def interpolate_hr_stream(timestamps, nn_intervals, current_time, window_size=60.0, fs=4.0):
    """
    Interpolate discrete NN intervals into a continuous Heart Rate stream.
    
    Args:
        timestamps (list/np.array): Timestamps of NN intervals.
        nn_intervals (list/np.array): NN intervals in milliseconds.
        current_time (float): Current system time.
        window_size (float): Time window in seconds to interpolate over.
        fs (float): Sampling frequency in Hz.
        
    Returns:
        tuple: (interpolated_times, interpolated_hr)
    """
    if len(timestamps) < 4:
        return np.array([]), np.array([])

    # Convert to numpy arrays
    ts = np.array(timestamps)
    nn = np.array(nn_intervals)
    
    # Calculate Instantaneous Heart Rate (IHR)
    ihr = 60000.0 / nn
    
    # Create uniform time grid
    start_time = current_time - window_size
    num_points = int(window_size * fs)
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
    except Exception as e:
        # Fallback or log error (though we want to avoid logging in tight loops if possible)
        return np.array([]), np.array([])

def calculate_coherence_score(interpolated_hr, fs=4.0, target_freq=0.1):
    """
    Calculate the coherence score based on the Power Spectral Density.
    
    Args:
        interpolated_hr (np.array): Interpolated Heart Rate data (60s window).
        fs (float): Sampling frequency.
        target_freq (float): Target breathing frequency in Hz (e.g., 0.1 Hz for 6 BPM).
        
    Returns:
        float: Coherence score (0-100).
    """
    if len(interpolated_hr) < int(60 * fs):
        return 0.0
        
    # Detrend
    times = np.arange(len(interpolated_hr))
    detrended = interpolated_hr - np.polyval(np.polyfit(times, interpolated_hr, 1), times)
    
    # Apply Hanning Window
    window = get_window('hann', len(detrended))
    windowed_data = detrended * window
    
    # Calculate PSD using Welch's method
    freqs, psd = welch(windowed_data, fs, nperseg=len(windowed_data))
    
    # Define bands
    total_power_mask = (freqs >= 0.0033) & (freqs <= 0.4)
    target_band_mask = (freqs >= target_freq - 0.03) & (freqs <= target_freq + 0.03)
    
    total_power = np.sum(psd[total_power_mask])
    peak_power_in_target = np.max(psd[target_band_mask]) if np.any(target_band_mask) else 0.0
    
    if total_power == 0 or peak_power_in_target == 0:
        return 0.0
        
    # Coherence Ratio
    # Avoid division by zero if peak power is the only power (unlikely but possible)
    denominator = total_power - peak_power_in_target
    if denominator <= 0:
        ratio = 10.0 # Max coherence
    else:
        ratio = peak_power_in_target / denominator
        
    # Normalize to 0-100 using a hyperbolic tangent function for stability
    # Adjust the scaling factor (e.g., 2.0) to tune sensitivity
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
