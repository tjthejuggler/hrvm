import unittest
import os
import sys
import numpy as np
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database.db_manager import DatabaseManager
from src.utils.ipc import ProcessedData, ProcessingConfig

class TestDatabaseManager(unittest.TestCase):
    
    def setUp(self):
        self.db_path = "test_hrv_integration.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_user_management(self):
        # Create User
        user_id = self.db.create_user("testuser", "test@example.com", age=30)
        self.assertIsNotNone(user_id)
        
        # Get User
        user = self.db.get_user("testuser")
        self.assertIsNotNone(user)
        self.assertEqual(user['email'], "test@example.com")
        self.assertIn("age", user['metadata'])
        
        # List Users
        users = self.db.list_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]['username'], "testuser")

    def test_session_management(self):
        user_id = self.db.create_user("session_user", "session@example.com")
        
        # Create Session
        session_id = self.db.create_session(user_id, "Morning Meditation")
        self.assertIsNotNone(session_id)
        
        # Log Data
        data = ProcessedData(
            timestamp=datetime.now().timestamp(),
            ecg_window=np.array([1, 2, 3]),
            rr_intervals=[800.0, 810.0],
            heart_rate=75.0,
            hrv_rmssd=42.0,
            hrv_sdnn=50.0,
            quality_score=0.98
        )
        self.db.log_hrv_data(session_id, data)
        
        # End Session
        self.db.end_session(session_id)
        
        # Verify Session History
        history = self.db.get_session_history(user_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['notes'], "Morning Meditation")
        self.assertIsNotNone(history[0]['end_time'])
        
        # Verify Averages (should match the single data point)
        self.assertAlmostEqual(history[0]['avg_hr'], 75.0)
        self.assertAlmostEqual(history[0]['avg_rmssd'], 42.0)

    def test_settings_management(self):
        user_id = self.db.create_user("settings_user", "settings@example.com")
        
        config = ProcessingConfig(
            window_size_seconds=120,
            artifact_threshold=2.5,
            filter_cutoff_low=0.5,
            filter_cutoff_high=40.0
        )
        
        # Save Preset
        self.db.save_preset(user_id, "Focus Mode", config)
        
        # Load Preset
        loaded_config = self.db.load_preset(user_id, "Focus Mode")
        self.assertIsNotNone(loaded_config)
        self.assertEqual(loaded_config.window_size_seconds, 120)
        
        # Get User Presets
        presets = self.db.get_user_presets(user_id)
        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0]['preset_name'], "Focus Mode")

if __name__ == '__main__':
    unittest.main()
