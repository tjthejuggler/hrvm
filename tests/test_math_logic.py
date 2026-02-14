import unittest
import numpy as np
from src.processing.math_utils import (
    calculate_metrics,
    pan_tompkins_energy,
    find_peaks,
    reject_artifacts,
    calculate_coherence_score,
    calculate_resonance_metrics,
    interpolate_rr_intervals
)

class TestMathUtils(unittest.TestCase):
    
    def test_calculate_metrics(self):
        # Test with known values
        rr = np.array([800, 810, 790, 800, 820], dtype=float)
        rmssd, sdnn = calculate_metrics(rr)
        
        # Manual calc
        # Mean = 804
        # Diffs: 10, -20, 10, 20
        # Sq Diffs: 100, 400, 100, 400 -> Sum = 1000
        # RMSSD = sqrt(1000/4) = sqrt(250) = 15.81
        
        self.assertAlmostEqual(rmssd, 15.811, places=3)
        self.assertTrue(sdnn > 0)

    def test_pan_tompkins(self):
        # Create a dummy signal
        signal = np.zeros(100)
        signal[50] = 10 # Spike
        
        energy = pan_tompkins_energy(signal, 100)
        self.assertEqual(len(energy), 100)
        # Energy should be high around index 50
        self.assertTrue(np.max(energy) > 0)

    def test_find_peaks(self):
        signal = np.array([0, 1, 5, 1, 0, 0, 1, 6, 1, 0], dtype=float)
        peaks = find_peaks(signal, threshold=3, min_distance=2)
        
        self.assertEqual(len(peaks), 2)
        self.assertEqual(peaks[0], 2)
        self.assertEqual(peaks[1], 7)

    def test_reject_artifacts(self):
        rr = [800, 800, 800, 2000, 800] # 2000 is outlier
        clean, quality = reject_artifacts(rr)
        
        self.assertEqual(len(clean), 4)
        self.assertEqual(quality, 0.8)

    def test_interpolate_rr(self):
        rr = [1000, 1000, 1000] # 60 BPM constant
        hr, t = interpolate_rr_intervals(rr, sampling_rate=4.0)
        
        # Should be constant 60
        self.assertTrue(np.allclose(hr, 60.0))
        self.assertEqual(len(hr), len(t))

    def test_coherence_score_sine_wave(self):
        # Simulate perfect coherence (sine wave HR)
        # 6 breaths per minute = 0.1 Hz
        # HR varies between 60 and 80
        t = np.linspace(0, 60, 60) # 60 seconds
        hr = 70 + 10 * np.sin(2 * np.pi * 0.1 * t)
        
        # Convert HR to RR
        rr = 60000.0 / hr
        
        score = calculate_coherence_score(rr.tolist())
        
        # Should be high
        self.assertTrue(score > 0.8, f"Score {score} should be > 0.8 for sine wave")

    def test_coherence_score_random(self):
        # Random noise
        rr = np.random.normal(800, 50, 100).tolist()
        score = calculate_coherence_score(rr)
        
        # Should be low
        self.assertTrue(score < 0.5, f"Score {score} should be low for noise")

    def test_resonance_metrics(self):
        rr = [800] * 20
        metrics = calculate_resonance_metrics(rr)
        self.assertEqual(metrics['amplitude'], 0.0)
        self.assertEqual(metrics['lf_power'], 0.0)

if __name__ == '__main__':
    unittest.main()
