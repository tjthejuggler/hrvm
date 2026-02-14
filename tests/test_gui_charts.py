import unittest
from unittest.mock import MagicMock, patch
import dearpygui.dearpygui as dpg
from src.gui.ui_manager import UIManager
from src.utils.ipc import ProcessedData, KEY_INTERPOLATED_HR, KEY_RMSSD, KEY_COHERENCE, KEY_TIMESTAMP

class TestUIManagerCharts(unittest.TestCase):
    def setUp(self):
        # Mock dpg functions to avoid actual GUI creation
        self.dpg_patcher = patch('src.gui.ui_manager.dpg')
        self.mock_dpg = self.dpg_patcher.start()
        
        # Mock pipes
        self.data_pipe = MagicMock()
        self.ble_pipe = MagicMock()
        self.math_pipe = MagicMock()
        
        self.ui = UIManager(self.data_pipe, self.ble_pipe, self.math_pipe, "test_shm")
        
        # Initialize deques manually since setup_ui isn't called fully
        self.ui.rr_history.clear()
        self.ui.poincare_x.clear()
        self.ui.poincare_y.clear()

    def tearDown(self):
        self.dpg_patcher.stop()

    def test_rr_tachogram_update(self):
        """Test that RR intervals are added to history and plot is updated."""
        payload = {
            'rr_intervals': [800.0, 810.0, 790.0]
        }
        
        self.ui.handle_data_update(payload)
        
        # Check history
        self.assertEqual(len(self.ui.rr_history), 3)
        self.assertEqual(list(self.ui.rr_history), [800.0, 810.0, 790.0])
        
        # Check dpg calls
        # We expect set_value for "rr_series"
        self.mock_dpg.set_value.assert_any_call("rr_series", [[0, 1, 2], [800.0, 810.0, 790.0]])

    def test_poincare_plot_update(self):
        """Test that Poincaré plot data is generated correctly."""
        # First update: 1 point (not enough for pair)
        self.ui.handle_data_update({'rr_intervals': [800.0]})
        self.assertEqual(len(self.ui.poincare_x), 0)
        
        # Second update: 2nd point -> 1 pair (800, 810)
        self.ui.handle_data_update({'rr_intervals': [810.0]})
        self.assertEqual(len(self.ui.poincare_x), 1)
        self.assertEqual(list(self.ui.poincare_x), [800.0])
        self.assertEqual(list(self.ui.poincare_y), [810.0])
        
        # Third update: 3rd point -> 2nd pair (810, 790)
        self.ui.handle_data_update({'rr_intervals': [790.0]})
        self.assertEqual(len(self.ui.poincare_x), 2)
        self.assertEqual(list(self.ui.poincare_x), [800.0, 810.0])
        self.assertEqual(list(self.ui.poincare_y), [810.0, 790.0])
        
        # Check dpg calls
        self.mock_dpg.set_value.assert_any_call("poincare_series", [[800.0, 810.0], [810.0, 790.0]])

    def test_metrics_history_update(self):
        """Test that RMSSD and Coherence history plots are updated."""
        # Mock time to be consistent
        self.ui.start_time = 0
        with patch('time.time', return_value=10.0):
            payload = {
                KEY_RMSSD: 45.0,
                KEY_COHERENCE: 3.5
            }
            self.ui.handle_data_update(payload)
            
            # Check history
            self.assertEqual(list(self.ui.rmssd_history), [45.0])
            self.assertEqual(list(self.ui.coherence_history), [3.5])
            
            # Check dpg calls
            # Note: time.time() - start_time = 10.0
            self.mock_dpg.set_value.assert_any_call("rmssd_series", [[10.0], [45.0]])
            self.mock_dpg.set_value.assert_any_call("coherence_series", [[10.0], [3.5]])

if __name__ == '__main__':
    unittest.main()
