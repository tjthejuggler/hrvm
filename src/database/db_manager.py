import sqlite3
import json
import pickle
import logging
from typing import List, Dict, Any, Optional, Union
from dataclasses import asdict
from src.utils.ipc import ProcessedData, ProcessingConfig

class DatabaseManager:
    """
    Manages SQLite database for user data and HRV metrics.
    Thread-safe with connection pooling (via check_same_thread=False for simplicity in this context).
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self._initialize_schema()
        
    def _get_connection(self) -> sqlite3.Connection:
        """
        Returns the current database connection or creates a new one.
        """
        if self.conn is None:
            # check_same_thread=False allows the connection to be used by different threads.
            # This is necessary if the GUI framework calls callbacks on different threads.
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        return self.conn

    def _initialize_schema(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Users Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")

        # Sessions Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            notes TEXT,
            avg_hr REAL,
            avg_rmssd REAL,
            avg_sdnn REAL,
            avg_coherence REAL,
            resonance_score REAL,
            pacer_settings_snapshot TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start_time ON sessions(start_time);")
        
        # Check for new columns in sessions table (migration)
        cursor.execute("PRAGMA table_info(sessions)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'avg_coherence' not in columns:
            cursor.execute("ALTER TABLE sessions ADD COLUMN avg_coherence REAL")
        if 'resonance_score' not in columns:
            cursor.execute("ALTER TABLE sessions ADD COLUMN resonance_score REAL")
        if 'pacer_settings_snapshot' not in columns:
            cursor.execute("ALTER TABLE sessions ADD COLUMN pacer_settings_snapshot TEXT")

        # Settings/Presets Table (Legacy)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            setting_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            preset_name TEXT NOT NULL,
            config TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            UNIQUE(user_id, preset_name)
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_settings_user_id ON settings(user_id);")

        # New Presets Table (Pacer + Processing)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            target_bpm REAL,
            inhale_time REAL,
            inhale_hold_time REAL,
            exhale_time REAL,
            exhale_hold_time REAL,
            window_size INTEGER,
            waveform_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            UNIQUE(user_id, name)
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_presets_user_id ON presets(user_id);")
        
        # Migration for presets table
        cursor.execute("PRAGMA table_info(presets)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'inhale_time' not in columns:
            cursor.execute("ALTER TABLE presets ADD COLUMN inhale_time REAL")
        if 'inhale_hold_time' not in columns:
            cursor.execute("ALTER TABLE presets ADD COLUMN inhale_hold_time REAL")
        if 'exhale_time' not in columns:
            cursor.execute("ALTER TABLE presets ADD COLUMN exhale_time REAL")
        if 'exhale_hold_time' not in columns:
            cursor.execute("ALTER TABLE presets ADD COLUMN exhale_hold_time REAL")

        # Breathing Sessions Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS breathing_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_s REAL,
            resonance_score REAL,
            inhale_time REAL,
            hold_full_time REAL,
            exhale_time REAL,
            hold_empty_time REAL
        );
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_breathing_sessions_ts "
            "ON breathing_sessions(timestamp);"
        )

        # HRV Data Table (Time-Series)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS hrv_data (
            data_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            timestamp TIMESTAMP NOT NULL,
            heart_rate REAL NOT NULL,
            rmssd REAL,
            sdnn REAL,
            quality_score REAL,
            rr_intervals BLOB,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hrv_data_session_id ON hrv_data(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hrv_data_timestamp ON hrv_data(timestamp);")
        
        conn.commit()

    def create_user(self, username: str, email: str = "", **metadata) -> int:
        """Insert new user and return user_id."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (username, email, metadata) VALUES (?, ?, ?)",
                (username, email, json.dumps(metadata))
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # If user exists, return existing ID
            cursor.execute("SELECT user_id FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            if row:
                return row['user_id']
            raise

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Retrieve user details by username."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def list_users(self) -> List[Dict[str, Any]]:
        """List all users."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users")
        return [dict(row) for row in cursor.fetchall()]

    def create_session(self, user_id: int, notes: str = "") -> int:
        """Start new recording session."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (user_id, notes) VALUES (?, ?)",
            (user_id, notes)
        )
        conn.commit()
        return cursor.lastrowid

    def end_session(self, session_id: int, pacer_settings: Optional[Dict[str, Any]] = None) -> None:
        """Mark session as completed and calculate averages."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Calculate averages from hrv_data
        cursor.execute("""
            SELECT AVG(heart_rate), AVG(rmssd), AVG(sdnn)
            FROM hrv_data
            WHERE session_id = ?
        """, (session_id,))
        result = cursor.fetchone()
        
        avg_hr = result[0] if result[0] is not None else 0.0
        avg_rmssd = result[1] if result[1] is not None else 0.0
        avg_sdnn = result[2] if result[2] is not None else 0.0
        
        # TODO: Calculate avg_coherence if we store it in hrv_data or a separate table
        # For now, we'll leave it as NULL or update it if passed explicitly (future enhancement)
        
        pacer_snapshot = json.dumps(pacer_settings) if pacer_settings else None
        
        cursor.execute("""
            UPDATE sessions
            SET end_time = CURRENT_TIMESTAMP, avg_hr = ?, avg_rmssd = ?, avg_sdnn = ?, pacer_settings_snapshot = ?
            WHERE session_id = ?
        """, (avg_hr, avg_rmssd, avg_sdnn, pacer_snapshot, session_id))
        conn.commit()

    def get_session_history(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent sessions for a user."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM sessions 
            WHERE user_id = ? 
            ORDER BY start_time DESC 
            LIMIT ?
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]

    def save_preset(self, user_id: int, preset_name: str, config: ProcessingConfig) -> None:
        """Save or update a configuration preset."""
        conn = self._get_connection()
        cursor = conn.cursor()
        config_json = json.dumps(asdict(config))
        cursor.execute("""
            INSERT INTO settings (user_id, preset_name, config) 
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, preset_name) DO UPDATE SET config = excluded.config
        """, (user_id, preset_name, config_json))
        conn.commit()

    def load_preset(self, user_id: int, preset_name: str) -> Optional[ProcessingConfig]:
        """Load a specific preset."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT config FROM settings WHERE user_id = ? AND preset_name = ?",
            (user_id, preset_name)
        )
        row = cursor.fetchone()
        if row:
            config_dict = json.loads(row['config'])
            return ProcessingConfig(**config_dict)
        return None

    def get_user_presets(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all presets for a user (from new presets table)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM presets WHERE user_id = ?", (user_id,))
        return [dict(row) for row in cursor.fetchall()]

    def create_preset(self, user_id: int, name: str, target_bpm: float,
                     inhale_time: float, inhale_hold_time: float,
                     exhale_time: float, exhale_hold_time: float,
                     window_size: int, waveform_type: str) -> int:
        """Create a new configuration preset."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO presets (user_id, name, target_bpm, inhale_time, inhale_hold_time, exhale_time, exhale_hold_time, window_size, waveform_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, name) DO UPDATE SET
                target_bpm = excluded.target_bpm,
                inhale_time = excluded.inhale_time,
                inhale_hold_time = excluded.inhale_hold_time,
                exhale_time = excluded.exhale_time,
                exhale_hold_time = excluded.exhale_hold_time,
                window_size = excluded.window_size,
                waveform_type = excluded.waveform_type
        """, (user_id, name, target_bpm, inhale_time, inhale_hold_time, exhale_time, exhale_hold_time, window_size, waveform_type))
        conn.commit()
        
        # If it was an update, we need to fetch the ID
        if cursor.lastrowid:
            return cursor.lastrowid
        else:
            cursor.execute("SELECT id FROM presets WHERE user_id = ? AND name = ?", (user_id, name))
            row = cursor.fetchone()
            return row['id'] if row else -1

    def save_breathing_session(
        self,
        duration_s: float,
        resonance_score: float,
        inhale: float = 4.0,
        hold_full: float = 0.0,
        exhale: float = 4.0,
        hold_empty: float = 0.0,
    ) -> int:
        """Persist a completed resonance-breathing session."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO breathing_sessions
                (duration_s, resonance_score, inhale_time, hold_full_time,
                 exhale_time, hold_empty_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (duration_s, resonance_score, inhale, hold_full, exhale, hold_empty),
        )
        conn.commit()
        return cursor.lastrowid

    def get_breathing_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent breathing sessions, oldest-first."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM breathing_sessions
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def log_hrv_data(self, session_id: int, data: Union[ProcessedData, List[ProcessedData]]) -> None:
        """
        Persist HRV metrics to database.
        Supports both single item and bulk insert (list of items).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        if isinstance(data, list):
            data_list = data
        else:
            data_list = [data]
            
        if not data_list:
            return

        rows = []
        for item in data_list:
            # Pickling rr_intervals (list of floats)
            rr_blob = pickle.dumps(item.rr_intervals)
            rows.append((
                session_id,
                item.timestamp,
                item.heart_rate,
                item.hrv_rmssd,
                item.hrv_sdnn,
                item.quality_score,
                rr_blob
            ))
            
        cursor.executemany("""
            INSERT INTO hrv_data (session_id, timestamp, heart_rate, rmssd, sdnn, quality_score, rr_intervals)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

if __name__ == "__main__":
    # Quick verification block
    import os
    
    test_db_path = "test_hrv.db"
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
        
    db = DatabaseManager(test_db_path)
    print("Database initialized.")
    
    uid = db.create_user("test_user", "test@example.com")
    print(f"User created with ID: {uid}")
    
    sid = db.create_session(uid, "Test Session")
    print(f"Session created with ID: {sid}")
    
    # Mock data
    import numpy as np
    data = ProcessedData(
        timestamp=1234567890.0,
        ecg_window=np.array([]),
        rr_intervals=[800.0, 810.0],
        heart_rate=75.0,
        hrv_rmssd=40.0,
        hrv_sdnn=50.0,
        quality_score=0.95
    )
    
    db.log_hrv_data(sid, data)
    print("Data logged.")
    
    db.end_session(sid)
    print("Session ended.")
    
    history = db.get_session_history(uid)
    print(f"Session history: {history}")
    
    db.close()
    os.remove(test_db_path)
    print("Test complete.")
