"""Tests for the Rapid Change game controller and persistence helpers."""
import tempfile
import os
import json
import time
import unittest
from unittest.mock import patch

# We need to mock dearpygui before importing the module
import sys
from unittest.mock import MagicMock
sys.modules['dearpygui'] = MagicMock()
sys.modules['dearpygui.dearpygui'] = MagicMock()

from src.gui.rapid_change_game import (
    RapidChangeController, config_key, get_unique_configs,
    get_times_for_config, load_rc_history, save_rc_entry
)


class TestConfigKey(unittest.TestCase):
    def test_config_key(self):
        self.assertEqual(config_key("one_way", 60, 100), "one_way|60|100")
        self.assertEqual(config_key("return", 70, 120), "return|70|120")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.mktemp(suffix='.json')

    def tearDown(self):
        if os.path.exists(self.tmpfile):
            os.remove(self.tmpfile)

    def test_load_empty(self):
        history = load_rc_history(self.tmpfile)
        self.assertEqual(history, [])

    def test_save_and_load(self):
        entry = {'mode': 'one_way', 'start_hr': 60, 'end_hr': 100,
                 'elapsed_s': 15.5, 'direction': 'up', 'timestamp': 1234}
        save_rc_entry(entry, filepath=self.tmpfile)
        history = load_rc_history(self.tmpfile)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['elapsed_s'], 15.5)

    def test_multiple_entries(self):
        for i in range(3):
            save_rc_entry({'mode': 'one_way', 'start_hr': 60, 'end_hr': 100,
                           'elapsed_s': 10.0 + i, 'direction': 'up',
                           'timestamp': 1234 + i}, filepath=self.tmpfile)
        history = load_rc_history(self.tmpfile)
        self.assertEqual(len(history), 3)

    def test_get_unique_configs(self):
        entries = [
            {'mode': 'one_way', 'start_hr': 60, 'end_hr': 100, 'elapsed_s': 10},
            {'mode': 'one_way', 'start_hr': 60, 'end_hr': 100, 'elapsed_s': 12},
            {'mode': 'return', 'start_hr': 60, 'end_hr': 100, 'elapsed_s': 25},
        ]
        configs = get_unique_configs(entries)
        self.assertEqual(len(configs), 2)

    def test_get_times_for_config(self):
        entries = [
            {'mode': 'one_way', 'start_hr': 60, 'end_hr': 100, 'elapsed_s': 15.5},
            {'mode': 'one_way', 'start_hr': 60, 'end_hr': 100, 'elapsed_s': 12.3},
            {'mode': 'return', 'start_hr': 60, 'end_hr': 100, 'elapsed_s': 25.0},
        ]
        times = get_times_for_config(entries, 'one_way', 60, 100)
        self.assertEqual(times, [15.5, 12.3])
        times_return = get_times_for_config(entries, 'return', 60, 100)
        self.assertEqual(times_return, [25.0])


class TestRapidChangeController(unittest.TestCase):
    def setUp(self):
        self.ctrl = RapidChangeController()

    def test_validate_same_hr(self):
        self.ctrl.configure('one_way', 60, 60)
        valid, reason = self.ctrl.validate_config()
        self.assertFalse(valid)
        self.assertIn("different", reason)

    def test_validate_return_peak_lower(self):
        self.ctrl.configure('return', 100, 60)
        valid, reason = self.ctrl.validate_config()
        self.assertFalse(valid)
        self.assertIn("Peak", reason)

    def test_validate_valid_one_way(self):
        self.ctrl.configure('one_way', 60, 100)
        valid, _ = self.ctrl.validate_config()
        self.assertTrue(valid)

    def test_validate_valid_return(self):
        self.ctrl.configure('return', 60, 100)
        valid, _ = self.ctrl.validate_config()
        self.assertTrue(valid)

    def test_can_start_no_hr(self):
        self.ctrl.configure('one_way', 60, 100)
        can, reason = self.ctrl.can_start()
        self.assertFalse(can)
        self.assertIn("No heart rate", reason)

    def test_can_start_hr_too_high_for_up(self):
        self.ctrl.configure('one_way', 60, 100)
        self.ctrl.update_hr(70)
        can, reason = self.ctrl.can_start()
        self.assertFalse(can)
        self.assertIn("≤", reason)

    def test_can_start_valid_up(self):
        self.ctrl.configure('one_way', 60, 100)
        self.ctrl.update_hr(55)
        can, _ = self.ctrl.can_start()
        self.assertTrue(can)

    def test_can_start_down_direction(self):
        self.ctrl.configure('one_way', 100, 60)
        self.ctrl.update_hr(105)
        can, _ = self.ctrl.can_start()
        self.assertTrue(can)

    def test_can_start_hr_too_low_for_down(self):
        self.ctrl.configure('one_way', 100, 60)
        self.ctrl.update_hr(90)
        can, reason = self.ctrl.can_start()
        self.assertFalse(can)
        self.assertIn("≥", reason)

    def test_can_start_return_mode(self):
        self.ctrl.configure('return', 60, 100)
        self.ctrl.update_hr(55)
        can, _ = self.ctrl.can_start()
        self.assertTrue(can)

    def test_can_start_return_hr_too_high(self):
        self.ctrl.configure('return', 60, 100)
        self.ctrl.update_hr(65)
        can, _ = self.ctrl.can_start()
        self.assertFalse(can)

    @patch('src.gui.rapid_change_game.save_rc_entry')
    def test_one_way_up_flow(self, mock_save):
        self.ctrl.configure('one_way', 60, 100)
        self.ctrl.update_hr(55)
        self.assertTrue(self.ctrl.start_round())
        self.assertEqual(self.ctrl.state, 'racing')

        self.ctrl.update_hr(80)
        self.assertIsNone(self.ctrl.tick())

        self.ctrl.update_hr(100)
        result = self.ctrl.tick()
        self.assertIsNotNone(result)
        self.assertEqual(self.ctrl.state, 'finished')
        self.assertEqual(result['mode'], 'one_way')
        self.assertEqual(result['start_hr'], 60)
        self.assertEqual(result['end_hr'], 100)
        mock_save.assert_called_once()

    @patch('src.gui.rapid_change_game.save_rc_entry')
    def test_one_way_down_flow(self, mock_save):
        self.ctrl.configure('one_way', 100, 60)
        self.ctrl.update_hr(105)
        self.assertTrue(self.ctrl.start_round())

        self.ctrl.update_hr(80)
        self.assertIsNone(self.ctrl.tick())

        self.ctrl.update_hr(60)
        result = self.ctrl.tick()
        self.assertIsNotNone(result)
        self.assertEqual(result['direction'], 'down')

    @patch('src.gui.rapid_change_game.save_rc_entry')
    def test_return_mode_flow(self, mock_save):
        self.ctrl.configure('return', 60, 100)
        self.ctrl.update_hr(55)
        self.assertTrue(self.ctrl.start_round())
        self.assertEqual(self.ctrl.state, 'racing')

        # Racing to peak
        self.ctrl.update_hr(80)
        self.assertIsNone(self.ctrl.tick())
        self.assertEqual(self.ctrl.state, 'racing')

        # Reach peak
        self.ctrl.update_hr(100)
        self.assertIsNone(self.ctrl.tick())
        self.assertEqual(self.ctrl.state, 'returning')

        # Returning
        self.ctrl.update_hr(70)
        self.assertIsNone(self.ctrl.tick())

        # Back to start
        self.ctrl.update_hr(60)
        result = self.ctrl.tick()
        self.assertIsNotNone(result)
        self.assertEqual(self.ctrl.state, 'finished')
        self.assertIn('time_to_peak_s', result)
        self.assertIn('time_from_peak_s', result)

    def test_cancel(self):
        self.ctrl.configure('one_way', 60, 100)
        self.ctrl.update_hr(55)
        self.ctrl.start_round()
        self.ctrl.cancel()
        self.assertEqual(self.ctrl.state, 'idle')

    def test_reset(self):
        self.ctrl.state = 'finished'
        self.ctrl.reset()
        self.assertEqual(self.ctrl.state, 'idle')

    def test_get_elapsed(self):
        self.ctrl.configure('one_way', 60, 100)
        self.ctrl.update_hr(55)
        self.ctrl.start_round()
        time.sleep(0.05)
        elapsed = self.ctrl.get_elapsed()
        self.assertGreater(elapsed, 0)

    def test_get_elapsed_idle(self):
        self.assertEqual(self.ctrl.get_elapsed(), 0.0)

    def test_direction_up(self):
        self.ctrl.configure('one_way', 60, 100)
        self.assertEqual(self.ctrl._direction, 'up')

    def test_direction_down(self):
        self.ctrl.configure('one_way', 100, 60)
        self.assertEqual(self.ctrl._direction, 'down')

    def test_return_direction_always_up(self):
        self.ctrl.configure('return', 60, 100)
        self.assertEqual(self.ctrl._direction, 'up')


if __name__ == '__main__':
    unittest.main()
