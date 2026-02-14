import unittest
import time
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.gui.pacer import PacerEngine

class TestPacerEngine(unittest.TestCase):
    def setUp(self):
        self.pacer = PacerEngine()
        
    def test_initial_state(self):
        self.assertEqual(self.pacer.inhale_time, 4.0)
        self.assertEqual(self.pacer.inhale_hold_time, 4.0)
        self.assertEqual(self.pacer.exhale_time, 4.0)
        self.assertEqual(self.pacer.exhale_hold_time, 4.0)
        self.assertAlmostEqual(self.pacer.get_bpm(), 3.75) # 60 / 16
        
    def test_timing_update(self):
        self.pacer.set_timing(5.0, 0.0, 5.0, 0.0)
        self.assertEqual(self.pacer.inhale_time, 5.0)
        self.assertEqual(self.pacer.inhale_hold_time, 0.0)
        self.assertEqual(self.pacer.exhale_time, 5.0)
        self.assertEqual(self.pacer.exhale_hold_time, 0.0)
        self.assertAlmostEqual(self.pacer.get_bpm(), 6.0) # 60 / 10
        
    def test_cycle_logic(self):
        # Set 1s for each stage for easy testing
        self.pacer.set_timing(1.0, 1.0, 1.0, 1.0)
        self.pacer.reset()
        
        # Mock time by overriding start_time
        # t=0.5 -> Inhale
        self.pacer.start_time = time.time() - 0.5
        self.pacer.update(100, 100)
        # We can't easily check internal state without exposing it, 
        # but we can check if it runs without error.
        
        # t=1.5 -> Hold Full
        self.pacer.start_time = time.time() - 1.5
        self.pacer.update(100, 100)
        
        # t=2.5 -> Exhale
        self.pacer.start_time = time.time() - 2.5
        self.pacer.update(100, 100)
        
        # t=3.5 -> Hold Empty
        self.pacer.start_time = time.time() - 3.5
        self.pacer.update(100, 100)

if __name__ == '__main__':
    unittest.main()
