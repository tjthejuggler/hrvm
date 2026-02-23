"""
Resonance Math Module.

Contains clinical-grade signal processing algorithms for both 
Stationary (Stepped) and Non-Stationary (Continuous Sliding Window) 
Heart Rate Variability Biofeedback protocols.
"""
import numpy as np
import pandas as pd
import scipy.interpolate
import scipy.signal
import scipy.ndimage
import pywt
from typing import List, Tuple, Dict, Optional

# -------------------------------------------------------------------------
# 1. STATIONARY METRICS (For Stepped Protocols)
# -------------------------------------------------------------------------
class PhysiologicalMath:
    @staticmethod
    def calculate_rmssd(rr_intervals: List[float]) -> float:
        if len(rr_intervals) < 2: return 0.0
        diffs = np.diff(np.array(rr_intervals))
        return float(np.sqrt(np.mean(diffs ** 2)))

    @staticmethod
    def calculate_pt_amplitude(rr_intervals: List[float], timestamps: List[float], breath_cycles: List[Tuple[float, float]]) -> float:
        if not rr_intervals or not breath_cycles: return 0.0
        hr_array = 60000.0 / np.array(rr_intervals)
        ts_array = np.array(timestamps)
        
        pt_amplitudes = []
        for start, end in breath_cycles:
            mask = (ts_array >= start) & (ts_array <= end)
            cycle_hr = hr_array[mask]
            if len(cycle_hr) > 0:
                pt_amplitudes.append(np.max(cycle_hr) - np.min(cycle_hr))
        return float(np.mean(pt_amplitudes)) if pt_amplitudes else 0.0

    @staticmethod
    def calculate_spectral_power(rr_intervals: List[float], timestamps: List[float]) -> Tuple[float, float]:
        if len(rr_intervals) < 10: return 0.0, 0.0
        rr_centered = np.array(rr_intervals) - np.mean(rr_intervals)
        ts_array = np.array(timestamps)
        
        freqs_tot = np.linspace(0.0033, 0.4, 2000)
        freqs_lf = np.linspace(0.04, 0.15, 1000)
        
        try:
            power_tot = scipy.signal.lombscargle(ts_array, rr_centered, 2 * np.pi * freqs_tot)
            power_lf = scipy.signal.lombscargle(ts_array, rr_centered, 2 * np.pi * freqs_lf)
        except TypeError:
            power_tot = scipy.signal.lombscargle(ts_array, rr_centered, 2 * np.pi * freqs_tot)
            power_lf = scipy.signal.lombscargle(ts_array, rr_centered, 2 * np.pi * freqs_lf)
            
        tot_power = np.trapz(power_tot, freqs_tot)
        lf_power = np.trapz(power_lf, freqs_lf)
        lf_nu = (lf_power / tot_power) * 100.0 if tot_power > 0 else 0.0
        return float(lf_power), float(lf_nu)

    @staticmethod
    def calculate_phase_synchrony(rr_intervals: List[float], timestamps: List[float], i_sec: float, e_sec: float) -> float:
        if len(rr_intervals) < 10: return 0.0
        cycle_sec = i_sec + e_sec
        even_ts = np.arange(timestamps[0], timestamps[-1], 0.25)
        interp_func = scipy.interpolate.interp1d(timestamps, 60000.0 / np.array(rr_intervals), kind='linear', fill_value="extrapolate")
        even_hr = interp_func(even_ts)
        
        synth_resp = []
        for t in even_ts:
            mod_t = t % cycle_sec
            vol = mod_t / i_sec if mod_t < i_sec else 1.0 - ((mod_t - i_sec) / e_sec)
            synth_resp.append(vol)
        synth_resp = np.array(synth_resp)
        
        hr_norm = (even_hr - np.mean(even_hr)) / (np.std(even_hr) + 1e-6)
        resp_norm = (synth_resp - np.mean(synth_resp)) / (np.std(synth_resp) + 1e-6)
        
        correlation = np.correlate(hr_norm, resp_norm, mode='full')
        best_lag_sec = (np.arange(-len(hr_norm) + 1, len(hr_norm)) * 0.25)[np.argmax(correlation)]
        max_lag = cycle_sec / 2.0
        return float(1.0 - (min(abs(best_lag_sec), max_lag) / max_lag))
        
    # Minimum thresholds for a stepped epoch to be considered valid/conclusive.
    # phase < MIN_PHASE_SYNCHRONY  → HR is not following the breath pacer at all.
    # pt_amp < MIN_PT_AMPLITUDE    → RSA swing is too small to be physiologically meaningful.
    MIN_PHASE_SYNCHRONY: float = 0.25   # 0–1 scale; below this = no meaningful phase locking
    MIN_PT_AMPLITUDE: float = 1.5       # BPM; below this = RSA too small to interpret

    @staticmethod
    def score_epoch(rmssd: float, pt_amp: float, lf_nu: float, phase: float, baseline: Dict) -> Tuple[float, bool]:
        """Calculate the composite resonance score for a stepped test epoch.

        Returns:
            (score, is_valid) — score is 0–260+; is_valid is False when the
            epoch does not meet minimum quality thresholds (phase synchrony or
            PT amplitude too low), indicating the result should not be trusted.
        """
        b_rmssd = baseline.get('rmssd', 1.0) or 1.0
        b_pt = baseline.get('pt_amp', 1.0) or 1.0
        score = (phase * 0.40) + (min(pt_amp / b_pt, 5.0) * 0.30) + (min(lf_nu / 100.0, 1.0) * 0.20) + (min(rmssd / b_rmssd, 5.0) * 0.10)

        is_valid = (
            phase >= PhysiologicalMath.MIN_PHASE_SYNCHRONY
            and pt_amp >= PhysiologicalMath.MIN_PT_AMPLITUDE
        )
        return round(score * 100, 2), is_valid

# -------------------------------------------------------------------------
# 2. NON-STATIONARY DYNAMIC PACER (For Continuous Window Protocol)
# -------------------------------------------------------------------------
class ContinuousPacer:
    """Analytical mathematical engine for the continuously sliding frequency wave."""
    def __init__(self, start_bpm=6.75, delta_t=0.06704, breaths=78):
        self.T_0 = 60.0 / start_bpm
        self.total_breaths = breaths
        self.cycle_durations = self.T_0 + np.arange(breaths) * delta_t
        self.cycle_end_times = np.cumsum(self.cycle_durations)
        self.total_duration = self.cycle_end_times[-1]

    def evaluate(self, t_array):
        """Returns Instantaneous BPM, Phase (radians), and Reference Wave (0 to 1) for a given time t."""
        t_arr = np.atleast_1d(t_array)
        idx = np.clip(np.searchsorted(self.cycle_end_times, t_arr), 0, self.total_breaths - 1)
        
        inst_T = self.cycle_durations[idx]
        inst_hz = 1.0 / inst_T
        
        starts = np.insert(self.cycle_end_times[:-1], 0, 0.0)
        t_starts = starts[idx]
        
        # Phase is analytically integrated to prevent mathematical discontinuities
        phase = 2 * np.pi * idx + 2 * np.pi * inst_hz * (t_arr - t_starts)
        bpm = inst_hz * 60.0
        ref_wave = (np.sin(phase - np.pi/2) + 1.0) / 2.0
        
        return bpm, phase, ref_wave

# -------------------------------------------------------------------------
# 3. NON-STATIONARY ANALYTICS (Wavelets, Hilbert PLV)
# -------------------------------------------------------------------------
class ContinuousSlidingMath:
    @staticmethod
    def compute_continuous_lf_power(time_rr: np.ndarray, rr_intervals: np.ndarray, fs=4.0):
        time_grid = np.arange(time_rr[0], time_rr[-1], 1.0 / fs)
        interpolator = scipy.interpolate.interp1d(time_rr, rr_intervals, kind='linear', bounds_error=False, fill_value="extrapolate")
        rr_resampled = scipy.signal.detrend(interpolator(time_grid))
        
        wavelet = 'cmor1.5-1.0'
        freqs = np.linspace(0.04, 0.15, num=60)
        scales = pywt.frequency2scale(wavelet, freqs / fs)
        
        coeffs, _ = pywt.cwt(rr_resampled, scales, wavelet, sampling_period=1.0/fs)
        lf_power_continuous = np.trapz(np.abs(coeffs) ** 2, freqs, axis=0)
        return time_grid, interpolator(time_grid), lf_power_continuous

    @staticmethod
    def calculate_rolling_metrics(rr_resampled: np.ndarray, ref_wave: np.ndarray, fs=4.0):
        window_samples = int(60.0 * fs)
        ihr = 60000.0 / rr_resampled
        
        # Pandas Rolling Min/Max (C-backend)
        ihr_series = pd.Series(ihr)
        rolling_max = ihr_series.rolling(window=window_samples, center=True).max()
        rolling_min = ihr_series.rolling(window=window_samples, center=True).min()
        pt_amplitude = np.nan_to_num((rolling_max - rolling_min).to_numpy(), nan=0.0)
        
        # Hilbert Phase Locking Value
        nyq = 0.5 * fs
        b, a = scipy.signal.butter(3, [0.04 / nyq, 0.15 / nyq], btype='band')
        ihr_filtered = scipy.signal.filtfilt(b, a, ihr)
        
        phase_hr = np.unwrap(np.angle(scipy.signal.hilbert(ihr_filtered)))
        phase_ref = np.unwrap(np.angle(scipy.signal.hilbert(ref_wave - np.mean(ref_wave))))
        
        complex_diff = np.exp(1j * (phase_hr - phase_ref))
        plv_complex = scipy.ndimage.uniform_filter1d(complex_diff, size=window_samples, mode='reflect')
        
        return pt_amplitude, np.abs(plv_complex)

    # Minimum quality thresholds for the continuous protocol result to be conclusive.
    # PLV_THRESHOLD: peak PLV must exceed this at some point during the session.
    #   PLV < 0.3 means HR phase and breath phase were essentially uncorrelated throughout.
    # PEAK_SHAPE_RATIO: peak resonance_index must be at least this multiple of the session
    #   mean. A flat curve (ratio < 1.5) means there is no clear resonance peak — just noise.
    PLV_THRESHOLD: float = 0.3
    PEAK_SHAPE_RATIO: float = 1.5

    @staticmethod
    def extract_resonance_frequency(time_grid, lf_power, pt_amp, plv, pacing_bpm) -> Tuple[Optional[float], float]:
        """Find the resonance frequency from the continuous sliding-window metrics.

        Returns:
            (best_bpm, score) — best_bpm is None and score is 0.0 when the
            session does not meet minimum quality thresholds, indicating the
            result is inconclusive and should not be saved as a resonance frequency.
        """
        def normalize(x):
            x = np.nan_to_num(x, nan=np.nanmedian(x) if len(x)>0 else 0)
            ptp = np.ptp(x)
            return (x - np.min(x)) / ptp if ptp != 0 else np.zeros_like(x)

        # --- Quality gate 1: PLV must reach the minimum threshold at some point ---
        plv_clean = np.nan_to_num(plv, nan=0.0)
        if np.max(plv_clean) < ContinuousSlidingMath.PLV_THRESHOLD:
            return None, 0.0

        resonance_index = (0.4 * normalize(lf_power)) + (0.4 * normalize(pt_amp)) + (0.2 * normalize(plv_clean))

        b, a = scipy.signal.butter(2, 0.05 / (0.5 * 4.0), btype='low')
        resonance_smoothed = scipy.signal.filtfilt(b, a, resonance_index)

        # --- Quality gate 2: peak must be clearly above the session mean (no flat curve) ---
        mean_val = np.mean(resonance_smoothed)
        peak_val = np.max(resonance_smoothed)
        if mean_val > 0 and (peak_val / mean_val) < ContinuousSlidingMath.PEAK_SHAPE_RATIO:
            return None, 0.0

        max_idx = np.argmax(resonance_smoothed)
        t_max = time_grid[max_idx]

        # 5 Second Baroreflex Delay Correction
        stimulus_idx = (np.abs(time_grid - max(0.0, t_max - 5.0))).argmin()
        return float(pacing_bpm[stimulus_idx]), float(resonance_index[max_idx]) * 100.0