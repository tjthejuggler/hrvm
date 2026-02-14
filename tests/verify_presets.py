import sqlite3
import os
import json
from src.database.db_manager import DatabaseManager
from src.utils.ipc import ProcessingConfig

def verify_presets_table():
    db_path = "test_verify_presets.db"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    db = DatabaseManager(db_path)
    
    # 1. Verify Schema
    conn = db._get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(presets)")
    columns = {info[1] for info in cursor.fetchall()}
    
    expected_columns = {
        'id', 'user_id', 'name', 'target_bpm', 
        'inhale_time', 'inhale_hold_time', 'exhale_time', 'exhale_hold_time',
        'window_size', 'waveform_type', 'created_at'
    }
    
    missing = expected_columns - columns
    if missing:
        print(f"FAIL: Missing columns in presets table: {missing}")
    else:
        print("PASS: Presets table schema correct.")
        
    # 2. Verify Data Insertion
    user_id = db.create_user("test_user")
    
    # Test create_preset (new method)
    preset_id = db.create_preset(
        user_id=user_id,
        name="Box Breathing",
        target_bpm=4.0,
        inhale_time=4.0,
        inhale_hold_time=4.0,
        exhale_time=4.0,
        exhale_hold_time=4.0,
        window_size=60,
        waveform_type="sine"
    )
    
    cursor.execute("SELECT * FROM presets WHERE id = ?", (preset_id,))
    row = cursor.fetchone()
    
    if row and row['inhale_time'] == 4.0 and row['name'] == "Box Breathing":
        print("PASS: create_preset inserted data correctly.")
    else:
        print(f"FAIL: create_preset failed. Row: {dict(row) if row else 'None'}")

    # 3. Verify Legacy Save/Load (used by GUI currently)
    # The GUI uses save_preset which stores a ProcessingConfig object into the 'settings' table
    # It does NOT yet use the 'presets' table for the full pacer config, 
    # but we should verify the legacy path still works as the GUI relies on it.
    
    config = ProcessingConfig(window_size_seconds=120, artifact_threshold=5.0, filter_cutoff_low=0.5, filter_cutoff_high=40.0)
    db.save_preset(user_id, "LegacyPreset", config)
    
    loaded_config = db.load_preset(user_id, "LegacyPreset")
    if loaded_config and loaded_config.window_size_seconds == 120:
        print("PASS: Legacy save_preset/load_preset works.")
    else:
        print(f"FAIL: Legacy save/load failed. Loaded: {loaded_config}")

    db.close()
    os.remove(db_path)

if __name__ == "__main__":
    verify_presets_table()
