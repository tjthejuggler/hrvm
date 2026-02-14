from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum
import numpy as np

class CommandType(Enum):
    UPDATE_PACER_SETTINGS = "update_pacer_settings"
    START_RESONANCE_ASSESSMENT = "start_resonance_assessment"
    LOAD_PRESET = "load_preset"
    STOP_RESONANCE_ASSESSMENT = "stop_resonance_assessment"

@dataclass
class ECGBatch:
    """
    Batch of ECG samples sent from BLE process to Signal Processing process.
    """
    timestamp_unix: float          # Unix epoch (seconds)
    sample_rate: int               # Always 130 Hz
    samples: np.ndarray            # Shape: (N,), dtype: int32
    sequence_number: int           # For detecting drops

@dataclass
class ProcessedData:
    """
    Processed HRV metrics sent from Signal Processing process to GUI process.
    """
    timestamp: float               # Unix epoch
    ecg_window: np.ndarray         # Last 2 seconds for display
    rr_intervals: List[float]      # Milliseconds
    heart_rate: float              # BPM
    hrv_rmssd: float               # Milliseconds
    hrv_sdnn: float                # Milliseconds
    quality_score: float           # 0.0-1.0 (artifact rejection)
    coherence_score: float = 0.0   # 0.0-1.0 (HRV-Breathing synchronization)
    pacer_phase: float = 0.0       # 0.0-1.0 (Current pacer cycle position)
    
    # Resonance Assessment Data
    is_assessing: bool = False
    assessment_stage: str = ""     # e.g., "6.5 BPM"
    assessment_progress: float = 0.0 # 0.0-1.0

@dataclass
class BLECommand:
    """
    Control command sent from GUI to BLE process.
    """
    command: str                   # "connect", "disconnect", "get_battery"
    params: Dict[str, Any]         # Command-specific parameters

@dataclass
class ProcessingConfig:
    """
    Configuration update sent from GUI to Signal Processing process.
    """
    window_size_seconds: int       # HRV calculation window
    artifact_threshold: float      # MAD multiplier
    filter_cutoff_low: float       # Hz
    filter_cutoff_high: float      # Hz

@dataclass
class PacerConfig:
    """
    Configuration for the breathing pacer.
    """
    inhale_time: float             # Seconds
    inhale_hold_time: float        # Seconds
    exhale_time: float             # Seconds
    exhale_hold_time: float        # Seconds
    
@dataclass
class SystemCommand:
    """
    Generic command structure for IPC control messages.
    """
    command: CommandType
    payload: Dict[str, Any] = field(default_factory=dict)
