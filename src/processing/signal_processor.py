import time
import logging
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing import shared_memory
import numpy as np
from collections import deque
from typing import Dict, Any, Optional, Tuple, List
from scipy.signal import butter, lfilter, lfilter_zi

from src.utils.ipc import (
    IPCMessage, ECGBatch, HRBatch, ACCBatch, MSG_TERMINATE, MSG_DATA_UPDATE,
    MSG_HEARTBEAT_BLINK,
    MSG_CMD_START_STREAM, MSG_CMD_STOP_STREAM, MSG_CMD_SET_PACER_TARGET,
    MSG_CMD_START_ASSESSMENT, MSG_CMD_STOP_ASSESSMENT, MSG_ASSESSMENT_RESULT,
    MSG_CMD_SET_SESSION_MODE, MSG_CMD_START_RECORDING, MSG_CMD_STOP_RECORDING,
    KEY_TIMESTAMP, KEY_RAW_ECG, KEY_RMSSD, KEY_INTERPOLATED_HR, KEY_COHERENCE,
    KEY_IS_ARTIFACT, KEY_PACER_BPM, KEY_ASSESSMENT_TAG, KEY_OPTIMAL_BPM,
    KEY_SESSION_MODE, SESSION_MODE_COUNTING, SESSION_MODE_NONE,
    ProcessedData, ProcessingConfig, SystemCommand, CommandType
)
from src.processing.math_utils import (
    calculate_rmssd, interpolate_hr_stream, calculate_coherence_score,
    calculate_resonance_metrics, pan_tompkins_energy, find_peaks, reject_artifacts,
    calculate_metrics
)
from src.recording.session_recorder import SessionRecorder

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class SignalProcessor:
    def __init__(self, data_pipe: Connection, output_pipe: Connection, control_pipe: Connection, shm_name: str):
        self.data_pipe = data_pipe
        self.output_pipe = output_pipe
        self.control_pipe = control_pipe
        self.shm_name = shm_name
        self.running = False
        
        # Connect to Shared Memory
        try:
            self.shm = shared_memory.SharedMemory(name=shm_name)
            self.shm_buffer = np.ndarray((260,), dtype=np.int32, buffer=self.shm.buf)
        except Exception as e:
            logger.error(f"Failed to connect to shared memory: {e}")
            self.shm = None
            self.shm_buffer = None

        # Data Buffers
        self.rr_buffer = deque(maxlen=1000) # Store (timestamp, rr_ms)
        self.interpolated_hr_buffer = deque(maxlen=240) # Store 60s of 4Hz data
        
        # Signal Processing State
        self.sample_rate = 130
        self.buffer_size = 500
        self.raw_buffer = deque(maxlen=self.buffer_size)
        self.filtered_buffer = deque(maxlen=self.buffer_size)
        self.rr_intervals = deque(maxlen=300)
        self.last_peak_idx = -1
        self.total_samples_processed = 0
        
        # Filter State
        self.config = ProcessingConfig(
            window_size_seconds=60,
            artifact_threshold=3.0,
            filter_cutoff_low=5.0,
            filter_cutoff_high=15.0
        )
        self.b, self.a = self._design_bandpass()
        self.zi = lfilter_zi(self.b, self.a)
        
        # State
        self.pacer_target_bpm = 6.0 # Default
        self.last_coherence_calc_time = 0.0
        self.assessment_active = False
        self.current_assessment_tag = None
        self.assessment_data = {} # {tag: [amplitudes]}

        # Session Recorder (conditionally started based on session mode)
        self.session_recorder = SessionRecorder()
        self._recorder_started = False
        self.session_mode = SESSION_MODE_NONE # Kept for compatibility, but effectively unused/default

    def _design_bandpass(self) -> Tuple[np.ndarray, np.ndarray]:
        nyquist = 0.5 * self.sample_rate
        low = self.config.filter_cutoff_low / nyquist
        high = self.config.filter_cutoff_high / nyquist
        b, a = butter(1, [low, high], btype='band')
        return b, a

    def run(self):
        logger.info("Signal Processor started.")
        self.running = True
        self._data_counts = {"HR": 0, "ACC": 0, "ECG": 0, "IPC": 0}
        
        while self.running:
            try:
                # Check Data Pipe (ECG, HR, or ACC Batches)
                if self.data_pipe.poll():
                    message = self.data_pipe.recv()
                    if isinstance(message, HRBatch):
                        self._data_counts["HR"] += 1
                        if self._data_counts["HR"] <= 3:
                            logger.info(f"[DATA] Received HRBatch #{self._data_counts['HR']}")
                        self.process_hr_batch(message)
                    elif isinstance(message, ACCBatch):
                        self._data_counts["ACC"] += 1
                        if self._data_counts["ACC"] <= 3:
                            logger.info(f"[DATA] Received ACCBatch #{self._data_counts['ACC']} with {len(message.samples)} samples")
                        self.forward_acc_batch(message)
                    elif isinstance(message, ECGBatch):
                        self._data_counts["ECG"] += 1
                        if self._data_counts["ECG"] <= 3:
                            logger.info(f"[DATA] Received ECGBatch #{self._data_counts['ECG']} with {len(message.samples)} samples")
                        self.process_ecg_batch(message)
                    elif isinstance(message, IPCMessage):
                        self._data_counts["IPC"] += 1
                        self.process_message(message)

                # Check Control Pipe (Commands)
                if self.control_pipe.poll():
                    message = self.control_pipe.recv()
                    if message == "STOP":
                        self.running = False
                        logger.info("Signal Processor received STOP signal.")
                    elif isinstance(message, IPCMessage):
                        self.process_message(message)
                    elif isinstance(message, ProcessingConfig):
                        self.config = message
                        self.b, self.a = self._design_bandpass()
                        self.zi = lfilter_zi(self.b, self.a)
                        logger.info(f"Updated config: {self.config}")
                
                time.sleep(0.001) # Prevent busy loop
                
            except EOFError:
                logger.warning("Pipe closed, stopping Signal Processor.")
                self.running = False
            except Exception as e:
                logger.error(f"Error in Signal Processor loop: {e}")

        # Finalize session recording on shutdown
        self._stop_session_recording()

    def forward_acc_batch(self, batch: ACCBatch):
        """Forward ACC data directly to GUI (no processing needed)."""
        try:
            self.output_pipe.send(batch)
        except Exception as e:
            logger.error(f"Failed to forward ACC batch: {e}")
                
    def _start_session_recording(self):
        """Start the session recorder."""
        if not self._recorder_started:
            self.session_recorder.start()
            self._recorder_started = True
            logger.info("Session recording started.")

    def _stop_session_recording(self):
        """Stop the session recorder and write JSON file."""
        if self._recorder_started and self.session_recorder.is_recording:
            filepath = self.session_recorder.stop()
            if filepath:
                logger.info(f"Session JSON saved: {filepath}")
            else:
                logger.warning("Session recording stopped but no file was written.")
            self._recorder_started = False

    def process_hr_batch(self, batch: HRBatch):
        """Process HR data received directly from the BLE HR characteristic."""
        timestamp = batch.timestamp_unix
        hr_bpm = batch.heart_rate_bpm
        rr_intervals = batch.rr_intervals_ms

        # Feed data to session recorder (only if recording is active)
        if self._recorder_started and self.session_recorder.is_recording:
            self.session_recorder.add_hr_sample(bpm=hr_bpm)
            for rr_ms in rr_intervals:
                self.session_recorder.add_rr_interval(rr_ms=rr_ms)

        # Add RR intervals to buffers
        for rr_ms in rr_intervals:
            if 300 < rr_ms < 2000:  # Basic physiological validity check
                self.rr_intervals.append(rr_ms)
                self.rr_buffer.append((timestamp, rr_ms))

        # --- FIX 1: Filter buffer for Rolling Window (Last 60 Seconds) ---
        current_time = time.time()
        window_start = current_time - 60.0
        
        # Extract only beats from the last 60 seconds for calculation
        recent_nn = [rr for t, rr in self.rr_buffer if t >= window_start]
        recent_timestamps = [t for t, rr in self.rr_buffer if t >= window_start]
        # -----------------------------------------------------------------

        # Interpolation for RSA visualization (uses recent history)
        x_new, y_new = interpolate_hr_stream(recent_timestamps, recent_nn, current_time)

        # Use device-reported HR directly
        display_hr = float(hr_bpm) if hr_bpm > 0 else (y_new[-1] if len(y_new) > 0 else 0.0)

        # Coherence calculation
        coherence_score = 0.0
        if current_time - self.last_coherence_calc_time > 1.0:
            if len(y_new) > 0:
                target_freq = self.pacer_target_bpm / 60.0
                coherence_score = calculate_coherence_score(y_new, target_freq=target_freq)
                self.last_coherence_calc_time = current_time

        # Assessment logic
        if self.assessment_active and self.current_assessment_tag:
            if len(y_new) > 0:
                _, amplitude = calculate_resonance_metrics(y_new)
                if self.current_assessment_tag not in self.assessment_data:
                    self.assessment_data[self.current_assessment_tag] = []
                self.assessment_data[self.current_assessment_tag].append(amplitude)

        # --- FIX 2: Calculate Metrics on Rolling Window ---
        rmssd = 0.0
        sdnn = 0.0
        if len(recent_nn) > 1:
            # Calculate on 'recent_nn' (60s window) instead of all 'nn_intervals'
            rmssd, sdnn_val = calculate_metrics(np.array(recent_nn))
            sdnn = sdnn_val
        # --------------------------------------------------

        output = ProcessedData(
            timestamp=timestamp,
            ecg_window=np.array([]),
            rr_intervals=rr_intervals,
            heart_rate=display_hr,
            hrv_rmssd=rmssd,
            hrv_sdnn=sdnn,
            quality_score=1.0,
            coherence_score=coherence_score,
            is_assessing=self.assessment_active,
            assessment_stage=self.current_assessment_tag if self.current_assessment_tag else "",
            assessment_progress=0.0
        )

        try:
            self.output_pipe.send(output)
        except Exception as e:
            logger.error(f"Failed to send HR data update: {e}")

    def process_ecg_batch(self, batch: ECGBatch):
        # Forward raw ECG batch to GUI for chart display
        try:
            self.output_pipe.send(batch)
        except Exception as e:
            logger.error(f"Failed to forward ECG batch to GUI: {e}")

        # 1. Update raw buffer
        new_samples = batch.samples
        self.raw_buffer.extend(new_samples)
        
        # 2. Apply Bandpass Filter
        filtered_batch, self.zi = lfilter(self.b, self.a, new_samples, zi=self.zi)
        self.filtered_buffer.extend(filtered_batch)
        
        # Update Shared Memory for Visualization
        if self.shm_buffer is not None:
            current_signal = np.array(self.filtered_buffer)
            display_data = current_signal[-260:]
            if len(display_data) < 260:
                display_data = np.pad(display_data, (260 - len(display_data), 0), 'constant')
            self.shm_buffer[:] = display_data.astype(np.int32)

        # 3. FAST BLINK DETECTION (Visual-only, does NOT touch self.rr_intervals)
        # Metrics still come from process_hr_batch via the standard HR service.
        # This only fires a "blink" trigger for the GUI heartbeat indicator.
        current_signal = np.array(self.filtered_buffer)
        if len(current_signal) >= 130:
            analysis_window = current_signal[-130:]  # Last ~1 second at 130 Hz
            energy = pan_tompkins_energy(analysis_window, self.sample_rate)
            threshold = np.mean(energy) * 0.6
            min_dist = int(0.25 * self.sample_rate)  # 250ms refractory

            peaks = find_peaks(energy, threshold, min_dist)

            if len(peaks) > 0:
                last_peak_index = peaks[-1]
                samples_in_window = len(analysis_window)
                new_samples_count = len(new_samples)

                # Only fire if the peak is in the newly-arrived samples
                if last_peak_index >= (samples_in_window - new_samples_count):
                    try:
                        self.output_pipe.send(
                            IPCMessage(MSG_HEARTBEAT_BLINK, {}))
                    except Exception:
                        pass

        self.total_samples_processed += len(new_samples)

    def process_message(self, message: IPCMessage):
        if message.type == MSG_TERMINATE:
            self.running = False
            logger.info("Signal Processor received TERMINATE signal.")
            
        elif message.type == MSG_DATA_UPDATE:
            self.handle_data_update(message.payload)
            
        elif message.type == MSG_CMD_SET_PACER_TARGET:
            self.pacer_target_bpm = message.payload.get(KEY_PACER_BPM, 6.0)
            logger.info(f"Pacer target set to {self.pacer_target_bpm} BPM")
            
        elif message.type == MSG_CMD_START_ASSESSMENT:
            self.start_assessment(message.payload.get(KEY_ASSESSMENT_TAG))
            
        elif message.type == MSG_CMD_STOP_ASSESSMENT:
            self.stop_assessment()

        elif message.type == MSG_CMD_SET_SESSION_MODE:
            # Deprecated command, but keeping handler to prevent crash if old message received
            new_mode = message.payload.get(KEY_SESSION_MODE, SESSION_MODE_NONE)
            self.session_mode = new_mode
            logger.info(f"Session mode set to: {self.session_mode} (DEPRECATED)")

        elif message.type == MSG_CMD_START_RECORDING:
            self._start_session_recording()

        elif message.type == MSG_CMD_STOP_RECORDING:
            self._stop_session_recording()

    def handle_data_update(self, payload: Dict[str, Any]):
        rr_ms = payload.get('rr_ms')
        timestamp = payload.get(KEY_TIMESTAMP, time.time())
        
        # 1. Interpolation (RSA Visualization)
        current_time = time.time()
        timestamps = [x[0] for x in self.rr_buffer]
        nn_intervals = [x[1] for x in self.rr_buffer]
        
        x_new, y_new = interpolate_hr_stream(timestamps, nn_intervals, current_time)
        
        latest_interpolated = 0.0
        if len(y_new) > 0:
            latest_interpolated = y_new[-1]
        
        # 2. Coherence Calculation (Throttled)
        coherence_score = 0.0
        if current_time - self.last_coherence_calc_time > 1.0: # 1Hz
            if len(y_new) > 0:
                target_freq = self.pacer_target_bpm / 60.0
                coherence_score = calculate_coherence_score(y_new, target_freq=target_freq)
                self.last_coherence_calc_time = current_time
        
        # 3. Assessment Logic
        if self.assessment_active and self.current_assessment_tag:
            if len(y_new) > 0:
                _, amplitude = calculate_resonance_metrics(y_new)
                if self.current_assessment_tag not in self.assessment_data:
                    self.assessment_data[self.current_assessment_tag] = []
                self.assessment_data[self.current_assessment_tag].append(amplitude)

        # Calculate RMSSD
        rmssd = 0.0
        if len(nn_intervals) > 1:
            rmssd, _ = calculate_metrics(np.array(nn_intervals))

        output_payload = ProcessedData(
            timestamp=timestamp,
            ecg_window=np.array([]),
            rr_intervals=[rr_ms] if rr_ms else [],
            heart_rate=latest_interpolated,
            hrv_rmssd=rmssd,
            hrv_sdnn=0.0,
            quality_score=1.0,
            coherence_score=coherence_score,
            is_assessing=self.assessment_active,
            assessment_stage=self.current_assessment_tag if self.current_assessment_tag else "",
            assessment_progress=0.0
        )
        
        try:
            self.output_pipe.send(output_payload)
        except Exception as e:
            logger.error(f"Failed to send data update: {e}")

    def start_assessment(self, tag: str):
        self.assessment_active = True
        self.current_assessment_tag = tag
        logger.info(f"Assessment started for tag: {tag}")

    def stop_assessment(self):
        self.assessment_active = False
        logger.info("Assessment stopped.")
        self.finalize_assessment()

    def finalize_assessment(self):
        best_tag = None
        max_avg_amplitude = -1.0
        
        for tag, amplitudes in self.assessment_data.items():
            if not amplitudes:
                continue
            avg_amp = np.mean(amplitudes)
            logger.info(f"Tag {tag}: Avg Amplitude = {avg_amp}")
            if avg_amp > max_avg_amplitude:
                max_avg_amplitude = avg_amp
                best_tag = tag
        
        optimal_bpm = 6.0
        if best_tag:
            try:
                optimal_bpm = float(best_tag.split('_')[0])
            except ValueError:
                logger.error(f"Could not parse BPM from tag: {best_tag}")
        
        logger.info(f"Optimal BPM determined: {optimal_bpm}")
        
        try:
            self.output_pipe.send(IPCMessage(MSG_ASSESSMENT_RESULT, {
                KEY_OPTIMAL_BPM: optimal_bpm
            }))
        except Exception as e:
            logger.error(f"Failed to send assessment result: {e}")
        
        self.assessment_data = {}
        self.current_assessment_tag = None

def signal_processing_main(data_pipe: Connection, output_pipe: Connection, control_pipe: Connection, shm_name: str):
    """
    Entry point for the Signal Processing process.
    """
    processor = SignalProcessor(data_pipe, output_pipe, control_pipe, shm_name)
    processor.run()
