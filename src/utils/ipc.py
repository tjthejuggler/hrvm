from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import numpy as np

@dataclass
class IPCMessage:
    """
    Standard message format for Inter-Process Communication.
    """
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ECGBatch:
    """
    ECG data batch from BLE process to Signal Processing process.
    """
    timestamp_unix: float          # Unix epoch (seconds)
    sample_rate: int               # Always 130 Hz for Polar H10
    samples: np.ndarray            # Shape: (N,), dtype: int32
    sequence_number: int           # For detecting drops

@dataclass
class HRBatch:
    """
    Heart Rate data from BLE HR Measurement characteristic.
    Provides HR and RR intervals directly from the device (no ECG processing needed).
    """
    timestamp_unix: float          # Unix epoch (seconds)
    heart_rate_bpm: int            # Instantaneous heart rate
    rr_intervals_ms: List[float]   # RR intervals in milliseconds (may be empty)
    sequence_number: int           # For detecting drops

@dataclass
class ACCBatch:
    """
    Accelerometer data from Polar H10 PMD (Polar Measurement Data) service.
    Provides 3-axis acceleration samples at 25/50/100/200 Hz.
    """
    timestamp_unix: float                      # Unix epoch (seconds)
    samples: List[Tuple[int, int, int]]        # List of (x, y, z) in milliG
    sample_rate: int = 25                      # Hz (default 25 for low power)
    sequence_number: int = 0

@dataclass
class BLECommand:
    """
    Command message for BLE process control.
    """
    command: str                   # "connect", "disconnect", "get_battery", "exit"
    params: Dict[str, Any] = field(default_factory=dict)  # Command-specific parameters

@dataclass
class ProcessedData:
    """
    Processed HRV metrics and ECG data for GUI display.
    """
    timestamp: float               # Unix epoch
    ecg_window: np.ndarray         # Last 2 seconds for display
    rr_intervals: List[float]      # Milliseconds
    heart_rate: float              # BPM
    hrv_rmssd: float               # Milliseconds
    hrv_sdnn: float                # Milliseconds
    quality_score: float           # 0.0-1.0 (artifact rejection)
    coherence_score: float = 0.0
    is_assessing: bool = False
    assessment_stage: str = ""
    assessment_progress: float = 0.0

@dataclass
class ProcessingConfig:
    """
    Configuration for signal processing.
    """
    window_size_seconds: int = 60
    artifact_threshold: float = 3.0
    filter_cutoff_low: float = 5.0
    filter_cutoff_high: float = 15.0

@dataclass
class SystemCommand:
    """
    System-wide commands.
    """
    command: str
    params: Dict[str, Any] = field(default_factory=dict)

class CommandType:
    START_RESONANCE_ASSESSMENT = "START_RESONANCE_ASSESSMENT"
    STOP_RESONANCE_ASSESSMENT = "STOP_RESONANCE_ASSESSMENT"

# Message Types
MSG_TERMINATE = "TERMINATE"
MSG_DATA_UPDATE = "DATA_UPDATE"
MSG_CMD_START_STREAM = "START_STREAM"
MSG_CMD_STOP_STREAM = "STOP_STREAM"
MSG_CMD_SET_PACER_TARGET = "SET_PACER_TARGET"
MSG_CMD_START_ASSESSMENT = "START_ASSESSMENT"
MSG_CMD_STOP_ASSESSMENT = "STOP_ASSESSMENT"
MSG_ASSESSMENT_RESULT = "ASSESSMENT_RESULT"

# Payload Keys
KEY_TIMESTAMP = "timestamp"
KEY_RAW_ECG = "raw_ecg"
KEY_RMSSD = "rmssd"
KEY_INTERPOLATED_HR = "interpolated_hr"
KEY_COHERENCE = "coherence_score"
KEY_IS_ARTIFACT = "is_artifact"
KEY_PACER_BPM = "val"
KEY_ASSESSMENT_TAG = "tag"
KEY_OPTIMAL_BPM = "optimal_bpm"
