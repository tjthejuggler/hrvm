import numpy as np
from numba import njit

@njit(fastmath=True)
def calculate_metrics(rr_intervals):
    """
    Calculate RMSSD and SDNN from a list of RR intervals (in ms).
    
    Args:
        rr_intervals: Array of RR intervals in milliseconds
        
    Returns:
        Tuple[float, float]: (rmssd, sdnn)
    """
    n = len(rr_intervals)
    if n < 2:
        return 0.0, 0.0
    
    # SDNN (Standard Deviation of NN intervals)
    # We use sample standard deviation (ddof=1)
    mean_rr = 0.0
    for x in rr_intervals:
        mean_rr += x
    mean_rr /= n
    
    var_sum = 0.0
    for x in rr_intervals:
        var_sum += (x - mean_rr)**2
    sdnn = np.sqrt(var_sum / (n - 1))
    
    # RMSSD (Root Mean Square of Successive Differences)
    sum_sq_diff = 0.0
    for i in range(n - 1):
        diff = rr_intervals[i+1] - rr_intervals[i]
        sum_sq_diff += diff * diff
    rmssd = np.sqrt(sum_sq_diff / (n - 1))
    
    return rmssd, sdnn

@njit(fastmath=True)
def pan_tompkins_energy(signal, sample_rate):
    """
    Apply Pan-Tompkins transformation: Derivative -> Square -> Integrate.
    
    Args:
        signal: Bandpass filtered ECG signal
        sample_rate: Sampling rate in Hz
        
    Returns:
        np.ndarray: Integrated energy signal
    """
    n = len(signal)
    
    # 1. Derivative
    # y[n] = (1/8) * (2x[n] + x[n-1] - x[n-3] - 2x[n-4])
    # We assume signal has enough history or is padded.
    derivative = np.zeros(n)
    for i in range(4, n):
        derivative[i] = (2*signal[i] + signal[i-1] - signal[i-3] - 2*signal[i-4]) / 8.0
        
    # 2. Squaring
    squared = derivative ** 2
    
    # 3. Moving Window Integration
    # Window width ~150ms
    window_width = int(0.150 * sample_rate)
    integrated = np.zeros(n)
    
    current_sum = 0.0
    
    # Initial ramp up
    for i in range(min(window_width, n)):
        current_sum += squared[i]
        integrated[i] = current_sum / (i + 1)
        
    # Moving average
    for i in range(window_width, n):
        current_sum += squared[i] - squared[i - window_width]
        integrated[i] = current_sum / window_width
        
    return integrated

@njit(fastmath=True)
def find_peaks(signal, threshold, min_distance):
    """
    Find local maxima greater than threshold and separated by min_distance.
    
    Args:
        signal: Input signal (usually integrated ECG)
        threshold: Minimum amplitude threshold
        min_distance: Minimum samples between peaks
        
    Returns:
        np.ndarray: Indices of detected peaks
    """
    peaks = []
    n = len(signal)
    if n < 3:
        # Numba requires consistent return type
        # We can't return empty list if we typed it as array elsewhere, 
        # but here we construct it.
        # To be safe, we return a typed empty list or array.
        return np.array([0], dtype=np.int32)[:0] # Empty int32 array
        
    last_peak = -min_distance
    
    # We need to be careful not to detect peaks on the very edge if they are rising
    # But for a sliding window, we usually process the middle.
    
    for i in range(1, n-1):
        if signal[i] > threshold:
            if signal[i] > signal[i-1] and signal[i] > signal[i+1]:
                if (i - last_peak) > min_distance:
                    peaks.append(i)
                    last_peak = i
    
    return np.array(peaks, dtype=np.int32)

@njit(fastmath=True)
def reject_artifacts(rr_intervals, threshold_multiplier=3.0):
    """
    Reject artifacts using Median Absolute Deviation (MAD).
    
    Args:
        rr_intervals: List or array of RR intervals
        threshold_multiplier: Multiplier for MAD (default 3.0)
        
    Returns:
        List[float]: Cleaned RR intervals
        float: Quality score (ratio of kept/total)
    """
    n = len(rr_intervals)
    if n < 3:
        return rr_intervals, 1.0
        
    # Convert to array for numpy ops
    rr_arr = np.array(rr_intervals)
    
    median_rr = np.median(rr_arr)
    
    # Calculate MAD
    abs_diffs = np.abs(rr_arr - median_rr)
    mad = np.median(abs_diffs)
    
    # If MAD is 0 (e.g. all values same), use a small epsilon or skip
    if mad == 0:
        # If all values are the same, they are all valid (or all invalid, but we assume valid)
        # However, if we have outliers that are exactly the median, MAD is 0.
        # But if MAD is 0, it means at least half the data is exactly the median.
        # So any value != median is technically infinitely far away in terms of MAD.
        # But for HRV, if we have [800, 800, 800, 2000, 800], median=800, MAD=0.
        # We should probably keep only the ones equal to median?
        # Or use a minimum MAD.
        mad = 1.0 # Minimum 1ms deviation allowed
        
    # Threshold
    limit = threshold_multiplier * mad
    
    cleaned = []
    for x in rr_intervals:
        if abs(x - median_rr) <= limit:
            cleaned.append(x)
            
    quality_score = len(cleaned) / n
    
    return cleaned, quality_score

def interpolate_rr_intervals(rr_intervals, sampling_rate=4.0):
    """
    Interpolate RR intervals to a uniform grid for spectral analysis.
    
    Args:
        rr_intervals: List of RR intervals in ms
        sampling_rate: Target sampling rate in Hz (default 4Hz)
        
    Returns:
        Tuple[np.ndarray, np.ndarray]: (Interpolated HR signal (BPM), Time points)
    """
    if len(rr_intervals) < 2:
        return np.array([]), np.array([])
        
    # Convert RR to seconds
    rr_sec = np.array(rr_intervals) / 1000.0
    
    # Create time axis (cumulative sum of RR intervals)
    t_rr = np.cumsum(rr_sec)
    t_rr = t_rr - t_rr[0] # Start at 0
    
    # Create uniform time axis
    duration = t_rr[-1]
    if duration <= 0:
        return np.array([]), np.array([])
        
    num_samples = int(duration * sampling_rate)
    if num_samples < 2:
        return np.array([]), np.array([])
        
    t_uniform = np.linspace(0, duration, num_samples)
    
    # Convert RR to HR (BPM)
    # Avoid division by zero
    with np.errstate(divide='ignore', invalid='ignore'):
        hr_values = 60.0 / rr_sec
        hr_values[~np.isfinite(hr_values)] = 0.0
    
    # Interpolate
    hr_interpolated = np.interp(t_uniform, t_rr, hr_values)
    
    return hr_interpolated, t_uniform

def calculate_coherence_score(rr_intervals, target_bpm=None):
    """
    Calculate coherence score based on LF power or smoothness.
    
    Args:
        rr_intervals: List of RR intervals in ms
        target_bpm: Optional target breathing rate (not used in self-coherence for now)
        
    Returns:
        float: Coherence score (0.0 to 1.0)
    """
    if len(rr_intervals) < 10:
        return 0.0
        
    # Interpolate to 4Hz
    hr_signal, _ = interpolate_rr_intervals(rr_intervals, sampling_rate=4.0)
    
    if len(hr_signal) < 32: # Need enough samples for FFT
        return 0.0
        
    # Detrend (remove DC component)
    hr_detrended = hr_signal - np.mean(hr_signal)
    
    # Apply Hanning window
    window = np.hanning(len(hr_detrended))
    hr_windowed = hr_detrended * window
    
    # FFT
    fft_vals = np.fft.rfft(hr_windowed)
    fft_freq = np.fft.rfftfreq(len(hr_windowed), d=1.0/4.0)
    
    # Power Spectrum
    power_spectrum = np.abs(fft_vals)**2
    
    # Define Bands
    # LF: 0.04 - 0.15 Hz (Coherence band)
    # VLF: 0.0033 - 0.04 Hz
    # HF: 0.15 - 0.4 Hz
    
    lf_mask = (fft_freq >= 0.04) & (fft_freq <= 0.15)
    total_mask = (fft_freq >= 0.0033) & (fft_freq <= 0.4)
    
    lf_power = np.sum(power_spectrum[lf_mask])
    total_power = np.sum(power_spectrum[total_mask])
    
    if total_power == 0:
        return 0.0
        
    # Coherence Ratio: LF / (VLF + LF + HF)
    # Ideally, in high coherence, most power is in LF (around 0.1Hz or 6bpm)
    coherence = lf_power / total_power
    
    # Clip to 0-1
    return min(max(coherence, 0.0), 1.0)

def calculate_resonance_metrics(rr_intervals):
    """
    Calculate metrics for resonance assessment.
    
    Args:
        rr_intervals: List of RR intervals in ms
        
    Returns:
        dict: {
            'lf_power': float,
            'amplitude': float (HR Max - HR Min)
        }
    """
    if len(rr_intervals) < 10:
        return {'lf_power': 0.0, 'amplitude': 0.0}
        
    # 1. Amplitude (HR Max - HR Min)
    # We use the interpolated signal to be more robust to artifacts
    hr_signal, _ = interpolate_rr_intervals(rr_intervals, sampling_rate=4.0)
    
    if len(hr_signal) == 0:
        return {'lf_power': 0.0, 'amplitude': 0.0}
        
    amplitude = np.max(hr_signal) - np.min(hr_signal)
    
    # 2. LF Power (same as coherence logic)
    hr_detrended = hr_signal - np.mean(hr_signal)
    window = np.hanning(len(hr_detrended))
    hr_windowed = hr_detrended * window
    fft_vals = np.fft.rfft(hr_windowed)
    fft_freq = np.fft.rfftfreq(len(hr_windowed), d=1.0/4.0)
    power_spectrum = np.abs(fft_vals)**2
    
    lf_mask = (fft_freq >= 0.04) & (fft_freq <= 0.15)
    lf_power = np.sum(power_spectrum[lf_mask])
    
    return {
        'lf_power': lf_power,
        'amplitude': amplitude
    }
