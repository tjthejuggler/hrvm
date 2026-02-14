import time
import logging
import numpy as np
from multiprocessing import Process, Pipe
from multiprocessing.connection import Connection
from multiprocessing import shared_memory
from collections import deque
from typing import List, Optional, Tuple, Dict, Any
from scipy.signal import butter, lfilter, lfilter_zi

from src.utils.ipc import ECGBatch, ProcessedData, ProcessingConfig, SystemCommand, CommandType
from src.processing.math_utils import (
    calculate_metrics,
    pan_tompkins_energy,
    find_peaks,
    reject_artifacts,
    calculate_coherence_score,
    calculate_resonance_metrics
)

# Configure logging
logger = logging.getLogger("SignalProcessor")
logger.setLevel(logging.DEBUG)

class SignalProcessor:
    """
    Applies Pan-Tompkins algorithm and calculates HRV metrics.
    Optimized with Numba JIT compilation.
    """
    
    def __init__(self, input_pipe: Connection, output_pipe: Connection, 
                 control_pipe: Connection, shm_name: str):
        self.input_pipe = input_pipe
        self.output_pipe = output_pipe
        self.control_pipe = control_pipe
        
        # Shared memory for ECG display
        # We attach to existing shared memory created by main process or GUI
        try:
            self.shm = shared_memory.SharedMemory(name=shm_name)
            # 260 samples * 4 bytes (int32) = 1040 bytes
            self.ecg_display_buffer = np.ndarray((260,), dtype=np.int32, buffer=self.shm.buf)
        except FileNotFoundError:
            logger.error(f"Shared memory {shm_name} not found. Display buffer disabled.")
            self.shm = None
            self.ecg_display_buffer = None
        
        # Processing state
        self.sample_rate = 130  # Hz
        
        # Internal buffer for continuous processing
        # We need enough history for filtering and peak detection
        # 2 seconds for display + extra for filter settling
        self.buffer_size = 500 
        self.raw_buffer = deque(maxlen=self.buffer_size)
        self.filtered_buffer = deque(maxlen=self.buffer_size)
        
        # RR Intervals for HRV calculation
        # Store enough for window_size_seconds (default 60s)
        # Max heart rate ~200 bpm -> ~3.3 beats/sec -> ~200 beats/min
        # 60s window -> ~200 intervals. Let's keep 300 to be safe.
        self.rr_intervals = deque(maxlen=300) 
        self.last_peak_idx = -1
        self.total_samples_processed = 0
        
        # Configuration
        self.config = ProcessingConfig(
            window_size_seconds=60,
            artifact_threshold=3.0,
            filter_cutoff_low=5.0,
            filter_cutoff_high=15.0
        )
        
        # Filter state
        self.b, self.a = self._design_bandpass()
        self.zi = lfilter_zi(self.b, self.a)
        
        # Resonance Assessment State
        self.is_assessing = False
        self.assessment_start_time = 0.0
        self.assessment_stages = [6.5, 6.0, 5.5, 5.0, 4.5] # BPM
        self.current_stage_idx = 0
        self.stage_duration = 120.0 # 2 minutes per stage
        self.stage_start_time = 0.0
        self.assessment_results = {} # Store metrics per stage
        
    def _design_bandpass(self) -> Tuple[np.ndarray, np.ndarray]:
        """Design Butterworth bandpass filter."""
        nyquist = 0.5 * self.sample_rate
        low = self.config.filter_cutoff_low / nyquist
        high = self.config.filter_cutoff_high / nyquist
        b, a = butter(1, [low, high], btype='band')
        return b, a

    def handle_config_update(self, config: ProcessingConfig) -> None:
        """Update processing parameters."""
        logger.info(f"Updating config: {config}")
        self.config = config
        self.b, self.a = self._design_bandpass()
        # Reset filter state
        self.zi = lfilter_zi(self.b, self.a)
        
        # Resize RR buffer if needed (approximate)
        # Max HR 200bpm = 3.33 Hz. Window * 3.33
        new_maxlen = int(config.window_size_seconds * 4)
        if new_maxlen != self.rr_intervals.maxlen:
            new_deque = deque(self.rr_intervals, maxlen=new_maxlen)
            self.rr_intervals = new_deque

    def handle_system_command(self, cmd: SystemCommand) -> None:
        """Handle system commands like starting assessment."""
        if cmd.command == CommandType.START_RESONANCE_ASSESSMENT:
            logger.info("Starting Resonance Assessment")
            self.is_assessing = True
            self.assessment_start_time = time.time()
            self.current_stage_idx = 0
            self.stage_start_time = self.assessment_start_time
            self.assessment_results = {}
            # Clear RR buffer to start fresh for assessment? 
            # Maybe not, we need some history for immediate feedback.
            # But for stage calculation, we should only use data from that stage.
            
        elif cmd.command == CommandType.STOP_RESONANCE_ASSESSMENT:
            logger.info("Stopping Resonance Assessment")
            self.is_assessing = False
            # Could save results here or send them out

    def update_assessment_state(self, current_time: float, rr_list: List[float]) -> Tuple[str, float]:
        """
        Update assessment state machine.
        Returns (current_stage_label, progress_0_1)
        """
        if not self.is_assessing:
            return "", 0.0
            
        elapsed_stage = current_time - self.stage_start_time
        
        if elapsed_stage >= self.stage_duration:
            # Stage complete, calculate metrics for this stage
            # We need to filter RR intervals that occurred during this stage
            # This is tricky with just a list of RRs without timestamps.
            # Approximation: Use the current buffer which represents the last ~60s.
            # Since stage is 120s, the buffer covers the latter half.
            # For MVP, taking the current buffer metrics is acceptable as it represents the steady state of the stage.
            
            current_bpm = self.assessment_stages[self.current_stage_idx]
            metrics = calculate_resonance_metrics(rr_list)
            self.assessment_results[current_bpm] = metrics
            logger.info(f"Stage {current_bpm} BPM complete. Metrics: {metrics}")
            
            # Move to next stage
            self.current_stage_idx += 1
            if self.current_stage_idx >= len(self.assessment_stages):
                logger.info("Assessment Complete!")
                self.is_assessing = False
                # TODO: Identify best frequency and send result
                return "Complete", 1.0
            
            self.stage_start_time = current_time
            elapsed_stage = 0.0
            
        current_bpm = self.assessment_stages[self.current_stage_idx]
        progress = elapsed_stage / self.stage_duration
        return f"{current_bpm} BPM", progress

    def process_batch(self, batch: ECGBatch) -> Optional[ProcessedData]:
        """Main processing pipeline for incoming ECG batch."""
        
        # 1. Update raw buffer
        new_samples = batch.samples
        self.raw_buffer.extend(new_samples)
        
        # Debug logging every second
        if batch.sequence_number % 13 == 0:
            logger.debug(f"[DEBUG] Processing batch seq={batch.sequence_number}, samples={len(new_samples)}")
        
        # 2. Apply Bandpass Filter
        # We filter the new batch, maintaining state
        filtered_batch, self.zi = lfilter(self.b, self.a, new_samples, zi=self.zi)
        self.filtered_buffer.extend(filtered_batch)
        
        # We need enough data to detect peaks reliably
        if len(self.filtered_buffer) < self.buffer_size:
            return None
            
        # Convert buffer to array for Numba processing
        # We process the last N samples to find peaks
        # But we need to be careful about overlap.
        # For simplicity in this version, we re-process the buffer tail
        # In a highly optimized version, we would only process new samples + overlap
        
        current_signal = np.array(self.filtered_buffer)
        
        # 3. Pan-Tompkins Energy
        # This includes Derivative -> Square -> Integrate
        energy = pan_tompkins_energy(current_signal, self.sample_rate)
        
        # 4. Peak Detection
        # Threshold can be dynamic, but fixed for now based on typical ECG amplitude
        # We can use a moving average of max energy to set threshold
        threshold = np.mean(energy) * 0.6 
        min_dist = int(0.25 * self.sample_rate) # 250ms refractory period
        
        peaks = find_peaks(energy, threshold, min_dist)
        
        # 5. Extract RR Intervals
        # We need to map peaks in current buffer to absolute sample indices
        # to calculate RR intervals correctly across batches.
        
        # Current buffer represents samples [total - len, total]
        buffer_start_idx = self.total_samples_processed - len(self.filtered_buffer) + len(new_samples)
        
        # We only care about peaks in the *new* part of the signal to avoid duplicates
        # The new part starts at index: len(current_signal) - len(new_samples)
        new_data_start_idx = len(current_signal) - len(new_samples)
        
        # However, a peak might be formed by the combination of old and new data.
        # So we look for peaks that end in the new region.
        
        new_rr_intervals = []
        
        for p in peaks:
            # p is index in current_signal
            # absolute index
            abs_idx = buffer_start_idx + p - len(new_samples) # Wait, let's re-calc
            
            # Let's simplify:
            # total_samples_processed tracks the count *after* adding new_samples
            # So the sample at current_signal[-1] has index total_samples_processed - 1
            # The sample at current_signal[p] has index:
            #   total_samples_processed - len(current_signal) + p
            
            abs_p = self.total_samples_processed - len(current_signal) + p
            
            # If this peak is new (after last processed peak)
            if abs_p > self.last_peak_idx:
                if self.last_peak_idx != -1:
                    # Calculate RR in ms
                    rr_samples = abs_p - self.last_peak_idx
                    rr_ms = (rr_samples / self.sample_rate) * 1000.0
                    
                    # Basic sanity check (300ms to 2000ms) -> 200bpm to 30bpm
                    if 300 < rr_ms < 2000:
                        self.rr_intervals.append(rr_ms)
                        new_rr_intervals.append(rr_ms)
                
                self.last_peak_idx = abs_p
        
        self.total_samples_processed += len(new_samples)
        
        # 6. Calculate Metrics
        # We re-calculate on every new beat (or batch)
        if len(self.rr_intervals) > 1:
            # Get recent intervals for window
            # We already limit deque size, so we can just use all
            rr_list = list(self.rr_intervals)
            
            # Artifact Rejection
            clean_rr, quality = reject_artifacts(rr_list, self.config.artifact_threshold)
            
            # Metrics
            rmssd, sdnn = calculate_metrics(np.array(clean_rr))
            
            # Coherence
            coherence = calculate_coherence_score(clean_rr)
            
            # Heart Rate (from last few intervals for responsiveness)
            # Use last 10 beats or all if fewer
            recent_n = min(len(clean_rr), 10)
            if recent_n > 0:
                avg_rr = np.mean(clean_rr[-recent_n:])
                heart_rate = 60000.0 / avg_rr
            else:
                heart_rate = 0.0
                
        else:
            rmssd, sdnn, heart_rate, quality, coherence = 0.0, 0.0, 0.0, 1.0, 0.0
            clean_rr = []

        # 7. Update Assessment State
        stage_label, progress = self.update_assessment_state(time.time(), clean_rr)

        # 8. Update Shared Memory (Display)
        if self.ecg_display_buffer is not None:
            # We want the last 260 samples (2 seconds)
            # current_signal has the filtered data
            display_data = current_signal[-260:]
            # Pad if not enough
            if len(display_data) < 260:
                display_data = np.pad(display_data, (260 - len(display_data), 0), 'constant')
            
            # Cast to int32 for display buffer (it was float from filter)
            # Scale up for visibility if needed, but DPG plots auto-scale usually.
            # Let's keep it as is, maybe cast to int.
            self.ecg_display_buffer[:] = display_data.astype(np.int32)

        # 9. Construct Output
        # We send ProcessedData to the UI.
        
        return ProcessedData(
            timestamp=batch.timestamp_unix,
            ecg_window=np.array([]), # Empty, using SHM
            rr_intervals=new_rr_intervals,
            heart_rate=heart_rate,
            hrv_rmssd=rmssd,
            hrv_sdnn=sdnn,
            quality_score=quality,
            coherence_score=coherence,
            is_assessing=self.is_assessing,
            assessment_stage=stage_label,
            assessment_progress=progress
        )

    def run(self) -> None:
        """Main loop for signal processing."""
        logger.info("Signal Processor started.")
        logger.info("[DEBUG] Waiting for ECG batches from BLE process...")
        
        batch_count = 0
        while True:
            # 1. Check Control Pipe
            try:
                if self.control_pipe.poll():
                    msg = self.control_pipe.recv()
                    if isinstance(msg, tuple) and msg[0] == 'UPDATE_CONFIG':
                        self.handle_config_update(msg[1])
                        self.control_pipe.send(('ACK', None))
                    elif isinstance(msg, ProcessingConfig):
                        self.handle_config_update(msg)
                    elif isinstance(msg, SystemCommand):
                        self.handle_system_command(msg)
                    elif msg == 'STOP':
                        logger.info("Stopping Signal Processor.")
                        break
            except EOFError:
                logger.warning("Control pipe closed. Stopping Signal Processor.")
                break
            except Exception as e:
                logger.error(f"Error reading control pipe: {e}")
            
            # 2. Check Data Pipe
            try:
                if self.input_pipe.poll(timeout=0.01): # 10ms timeout
                    batch = self.input_pipe.recv()
                    if isinstance(batch, ECGBatch):
                        batch_count += 1
                        if batch_count == 1:
                            logger.info(f"[DEBUG] Received first batch! seq={batch.sequence_number}")
                        result = self.process_batch(batch)
                        if result:
                            self.output_pipe.send(result)
                            if batch_count % 13 == 0:
                                logger.debug(f"[DEBUG] Sent ProcessedData to GUI (HR={result.heart_rate:.1f})")
            except EOFError:
                logger.warning("Input pipe closed.")
                break
            except Exception as e:
                logger.error(f"Error processing batch: {e}")
                import traceback
                traceback.print_exc()

def signal_processing_main(input_pipe: Connection, output_pipe: Connection, 
                          control_pipe: Connection, shm_name: str):
    """Entry point for the process."""
    processor = SignalProcessor(input_pipe, output_pipe, control_pipe, shm_name)
    processor.run()
