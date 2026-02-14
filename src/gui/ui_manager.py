import dearpygui.dearpygui as dpg
import numpy as np
import time
import threading
import logging
from collections import deque
from typing import Optional, Dict, Any, List
from multiprocessing.connection import Connection
from multiprocessing import shared_memory

from src.utils.ipc import (
    IPCMessage, BLECommand, MSG_TERMINATE, MSG_DATA_UPDATE, MSG_CMD_START_STREAM,
    MSG_CMD_STOP_STREAM, MSG_CMD_SET_PACER_TARGET, MSG_CMD_START_ASSESSMENT,
    MSG_CMD_STOP_ASSESSMENT, MSG_ASSESSMENT_RESULT, KEY_TIMESTAMP, KEY_RAW_ECG,
    KEY_RMSSD, KEY_INTERPOLATED_HR, KEY_COHERENCE, KEY_IS_ARTIFACT,
    KEY_PACER_BPM, KEY_ASSESSMENT_TAG, KEY_OPTIMAL_BPM,
    ProcessedData, ProcessingConfig, CommandType, SystemCommand
)
from src.gui.audio_feedback import AudioFeedback
from src.database.db_manager import DatabaseManager
from src.gui.pacer import PacerEngine

logger = logging.getLogger(__name__)

class UIManager:
    def __init__(self, data_pipe: Connection, ble_control_pipe: Connection, math_control_pipe: Connection, shm_name: str, auto_connect=False):
        self.data_pipe = data_pipe
        self.ble_control_pipe = ble_control_pipe
        self.math_control_pipe = math_control_pipe
        self.shm_name = shm_name
        self.auto_connect = auto_connect
        self.running = False
        
        # Audio Feedback
        self.audio_feedback = AudioFeedback()
        self.audio_enabled = False
        
        # Shared memory for ECG display
        self.shm: Optional[shared_memory.SharedMemory] = None
        self.ecg_display_buffer: Optional[np.ndarray] = None
        
        # Data Buffers
        self.hr_data_x = deque(maxlen=1000)
        self.hr_data_y = deque(maxlen=1000)
        self.interpolated_hr_x = deque(maxlen=240) # 60s @ 4Hz
        self.interpolated_hr_y = deque(maxlen=240)
        self.pacer_data_x = deque(maxlen=240)
        self.pacer_data_y = deque(maxlen=240)
        
        # Metric History (for graphs)
        self.max_history = 600
        self.time_history = deque(maxlen=self.max_history)
        self.hr_history = deque(maxlen=self.max_history)
        self.rmssd_history = deque(maxlen=self.max_history)
        self.sdnn_history = deque(maxlen=self.max_history)
        self.coherence_history = deque(maxlen=self.max_history)
        
        # Missing Charts Data
        self.rr_history = deque(maxlen=self.max_history) # For Tachogram
        self.poincare_x = deque(maxlen=self.max_history) # RR_n
        self.poincare_y = deque(maxlen=self.max_history) # RR_n+1
        
        # ECG Plot Data
        self.ecg_plot_data_x = np.linspace(0, 2, 260) # 2 seconds at 130Hz
        self.ecg_plot_data_y = np.zeros(260)

        # State
        self.current_bpm = 0.0
        self.current_rmssd = 0.0
        self.current_coherence = 0.0
        self.pacer_rate = 6.0 # BPM
        self.pacer_phase = 0.0
        self.start_time = time.time()
        self.is_connected = False
        self.is_recording = False
        
        # Assessment State
        self.assessment_active = False
        self.assessment_step = 0
        self.assessment_timer = 0.0
        self.assessment_steps = [
            ("BASELINE", 2.0, 0), # 2 min baseline (no pacer)
            ("STEP_1", 2.0, 6.5), # 2 min @ 6.5 BPM
            ("REST", 0.5, 0),     # 30 sec rest
            ("STEP_2", 2.0, 6.0), # 2 min @ 6.0 BPM
            ("REST", 0.5, 0),
            ("STEP_3", 2.0, 5.5), # 2 min @ 5.5 BPM
            ("REST", 0.5, 0),
            ("STEP_4", 2.0, 5.0), # 2 min @ 5.0 BPM
            ("REST", 0.5, 0),
            ("STEP_5", 2.0, 4.5), # 2 min @ 4.5 BPM
        ]
        
        # Pacer Engine (New Feature)
        self.pacer = PacerEngine()
        self.pacer_active = True
        
        # Database
        self.db = DatabaseManager("hrv_data.db")
        self.current_user_id: Optional[int] = None
        self.current_session_id: Optional[int] = None
        self.users: List[Dict[str, Any]] = []
        
        # Configuration
        self.config = ProcessingConfig(
            window_size_seconds=60,
            artifact_threshold=3.0,
            filter_cutoff_low=5.0,
            filter_cutoff_high=15.0
        )
        
        # UI Elements Tags
        self.window_tag = "Primary Window"
        self.plot_tag = "HRV Plot"
        self.coherence_tag = "Coherence Score"
        self.pacer_tag = "Pacer Settings"
        self.assessment_status_tag = "Assessment Status"

    def _attach_shared_memory(self):
        """Attempts to attach to shared memory. Retries if not immediately available."""
        try:
            self.shm = shared_memory.SharedMemory(name=self.shm_name)
            self.ecg_display_buffer = np.ndarray((260,), dtype=np.int32, buffer=self.shm.buf)
            logger.info(f"Attached to shared memory: {self.shm_name}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Error attaching to shared memory: {e}")

    def setup_ui(self):
        dpg.create_context()
        dpg.create_viewport(title="Polar H10 HRVB", width=1280, height=800)
        dpg.setup_dearpygui()
        
        self._load_users()

        with dpg.window(tag=self.window_tag, label="HRV Biofeedback", no_title_bar=True):
            
            # --- Top Bar ---
            with dpg.group(horizontal=True):
                dpg.add_text("Polar Flow-Sync", color=(0, 191, 255))
                dpg.add_spacer(width=20)
                dpg.add_text("Status: ")
                dpg.add_text("Disconnected", tag="status_text", color=(255, 0, 0))
                dpg.add_spacer(width=20)
                dpg.add_text("Battery: ")
                dpg.add_text("N/A", tag="battery_text")
                
                dpg.add_spacer(width=50)
                dpg.add_combo(items=[u['username'] for u in self.users], tag="user_combo", width=200, callback=self._on_user_selected, default_value="Select User")
                dpg.add_button(label="+", callback=self.create_user_management_window, width=30)
                
                dpg.add_spacer(width=20)
                dpg.add_button(label="Connect", tag="connect_btn", callback=self.handle_connect_button, width=100)
                dpg.add_button(label="Start Session", tag="session_btn", callback=self.handle_session_toggle, show=False, width=120)
                dpg.add_button(label="History", callback=self.create_history_window, width=80)
                dpg.add_checkbox(label="Audio Feedback", callback=self.toggle_audio, default_value=False)

            dpg.add_separator()

            # --- Main Layout ---
            with dpg.group(horizontal=True, height=400):
                
                # --- Left Panel: Controls ---
                with dpg.child_window(width=250, height=-1, border=True):
                    dpg.add_text("Pacer Settings")
                    dpg.add_slider_float(label="Breathing Rate (BPM)", default_value=6.0, min_value=4.0, max_value=10.0, callback=self.update_pacer_rate, tag="pacer_slider")
                    
                    dpg.add_spacer(height=10)
                    dpg.add_text("Breathing Cycle (s)")
                    dpg.add_input_float(label="Inhale", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_inhale")
                    dpg.add_input_float(label="Hold (Full)", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_hold_full")
                    dpg.add_input_float(label="Exhale", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_exhale")
                    dpg.add_input_float(label="Hold (Empty)", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_hold_empty")
                    
                    dpg.add_spacer(height=20)
                    dpg.add_separator()
                    dpg.add_text("Resonance Assessment")
                    dpg.add_button(label="Start Assessment", callback=self.start_assessment_protocol)
                    dpg.add_text("Status: Idle", tag=self.assessment_status_tag)
                    dpg.add_progress_bar(tag="assessment_progress", default_value=0.0, width=-1, show=False)
                    
                    dpg.add_spacer(height=20)
                    dpg.add_separator()
                    dpg.add_text("Presets")
                    dpg.add_input_text(tag="preset_name_input", width=150, hint="Preset Name")
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Save", callback=self.handle_save_preset, width=70)
                        dpg.add_button(label="Load", callback=self.create_load_preset_window, width=70)

                # --- Center Panel: Visuals ---
                with dpg.child_window(width=-300, height=-1, border=False):
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=50)
                        with dpg.group():
                            dpg.add_text("COHERENCE", color=(150, 150, 150))
                            dpg.add_text("0.0", tag="coherence_display", color=(0, 255, 0))
                        dpg.add_spacer(width=100)
                        with dpg.group():
                            dpg.add_text("HEART RATE", color=(150, 150, 150))
                            dpg.add_text("0 BPM", tag="hr_display", color=(255, 50, 50))
                            
                    dpg.add_spacer(height=10)
                    
                    # Pacer Visuals (Circle)
                    with dpg.drawlist(width=600, height=300, tag="pacer_drawlist"):
                        self.pacer.setup_draw_layer("pacer_drawlist")

                # --- Right Panel: Metrics ---
                with dpg.child_window(width=-1, height=-1, border=True):
                    dpg.add_text("Session Metrics", color=(0, 255, 255))
                    dpg.add_separator()
                    with dpg.group(horizontal=True):
                        dpg.add_text("RMSSD:")
                        dpg.add_text("0 ms", tag="rmssd_display", color=(0, 255, 0))
                    with dpg.group(horizontal=True):
                        dpg.add_text("SDNN:")
                        dpg.add_text("0 ms", tag="sdnn_display", color=(255, 255, 0))
                    dpg.add_spacer(height=10)
                    dpg.add_text("Signal Quality")
                    dpg.add_progress_bar(tag="quality_bar", default_value=0.0, width=-1)

            # --- Bottom Section: Graphs ---
            with dpg.child_window(width=-1, height=-1, border=True):
                with dpg.tab_bar(tag="main_tab_bar"):
                    
                    # Tab 1: Main Biofeedback (HR + Pacer)
                    with dpg.tab(label="Biofeedback"):
                        with dpg.plot(label="Heart Rate & Pacer", height=300, width=-1, tag=self.plot_tag):
                            dpg.add_plot_legend()
                            dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="x_axis")
                            with dpg.plot_axis(dpg.mvYAxis, label="BPM", tag="y_axis"):
                                dpg.add_line_series([], [], label="Interpolated HR", tag="hr_series")
                                dpg.add_line_series([], [], label="Pacer", tag="pacer_series")
                                dpg.set_axis_limits("y_axis", 40, 120)
                                
                    # Tab 2: HRV Analysis (Tachogram, Poincaré)
                    with dpg.tab(label="HRV Analysis"):
                        with dpg.group(horizontal=True):
                            # RR Tachogram
                            with dpg.plot(label="RR Interval Tachogram", height=300, width=600):
                                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="rr_x_axis")
                                with dpg.plot_axis(dpg.mvYAxis, label="RR Interval (ms)", tag="rr_y_axis"):
                                    dpg.add_line_series([], [], label="RR Intervals", tag="rr_series")
                                    
                            # Poincaré Plot
                            with dpg.plot(label="Poincaré Plot", height=300, width=300):
                                dpg.add_plot_axis(dpg.mvXAxis, label="RR_n (ms)", tag="poincare_x_axis")
                                with dpg.plot_axis(dpg.mvYAxis, label="RR_n+1 (ms)", tag="poincare_y_axis"):
                                    dpg.add_scatter_series([], [], label="RR Pairs", tag="poincare_series")

                    # Tab 3: Metrics History
                    with dpg.tab(label="Metrics History"):
                        with dpg.group(horizontal=True):
                            with dpg.plot(label="RMSSD History", height=200, width=400):
                                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="rmssd_x_axis")
                                with dpg.plot_axis(dpg.mvYAxis, label="RMSSD (ms)", tag="rmssd_y_axis"):
                                    dpg.add_line_series([], [], label="RMSSD", tag="rmssd_series")
                                    
                            with dpg.plot(label="Coherence History", height=200, width=400):
                                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="coherence_x_axis")
                                with dpg.plot_axis(dpg.mvYAxis, label="Score", tag="coherence_y_axis"):
                                    dpg.add_line_series([], [], label="Coherence", tag="coherence_series")

                    # Tab 4: Raw ECG
                    with dpg.tab(label="Raw ECG"):
                        with dpg.plot(height=300, width=-1, no_mouse_pos=True):
                            dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="ecg_x_axis")
                            dpg.add_plot_axis(dpg.mvYAxis, label="Amplitude", tag="ecg_y_axis")
                            dpg.add_line_series(self.ecg_plot_data_x, self.ecg_plot_data_y, label="ECG", parent="ecg_y_axis", tag="ecg_series")

        dpg.set_primary_window(self.window_tag, True)

    def run(self):
        self.setup_ui()
        dpg.show_viewport()
        self.running = True
        
        # Start data processing thread
        threading.Thread(target=self.process_incoming_data, daemon=True).start()
        
        if self.auto_connect:
            logger.info("Auto-connecting to device...")
            self.start_stream()

        while dpg.is_dearpygui_running() and self.running:
            self.update_pacer()
            self.update_assessment()
            self.poll_ble_status()
            self.update_ecg_plot()
            
            # Update Pacer Visuals
            if self.pacer_active:
                vw = dpg.get_viewport_width()
                vh = dpg.get_viewport_height()
                if vw > 0 and vh > 0:
                    self.pacer.update(600, 300) # Fixed size for drawlist
            
            dpg.render_dearpygui_frame()
            
        # Cleanup
        if self.audio_enabled:
            self.audio_feedback.stop()
        
        if self.shm:
            self.shm.close()
            
        dpg.destroy_context()
        self.running = False
        try:
            self.math_control_pipe.send(IPCMessage(MSG_TERMINATE))
        except:
            pass

    def process_incoming_data(self):
        while self.running:
            try:
                if self.data_pipe.poll():
                    msg = self.data_pipe.recv()
                    if isinstance(msg, IPCMessage):
                        if msg.type == MSG_DATA_UPDATE:
                            self.handle_data_update(msg.payload)
                        elif msg.type == MSG_ASSESSMENT_RESULT:
                            self.handle_assessment_result(msg.payload)
                    elif isinstance(msg, ProcessedData):
                         # Handle new ProcessedData format if needed, or convert
                         self.handle_processed_data(msg)
                else:
                    time.sleep(0.01)
            except EOFError:
                break
            except Exception as e:
                logger.error(f"Error processing incoming data: {e}")
                time.sleep(0.1)

    def handle_processed_data(self, data: ProcessedData):
        # Adapter for new data format to old UI logic
        payload = {
            KEY_INTERPOLATED_HR: data.heart_rate,
            KEY_RMSSD: data.hrv_rmssd,
            KEY_COHERENCE: data.coherence_score,
            KEY_TIMESTAMP: data.timestamp,
            'rr_intervals': data.rr_intervals # Pass RR intervals for tachogram
        }
        self.handle_data_update(payload)
        
        # Update extra metrics
        dpg.set_value("sdnn_display", f"{data.hrv_sdnn:.1f} ms")
        dpg.set_value("quality_bar", data.quality_score)
        
        # Save to DB if recording
        if self.is_recording and self.current_session_id:
            self.db.save_hrv_data(self.current_session_id, data)

    def handle_data_update(self, payload):
        current_time = time.time() - self.start_time
        
        # Update displays
        if KEY_INTERPOLATED_HR in payload:
            hr_val = payload[KEY_INTERPOLATED_HR]
            if hr_val > 0:
                self.current_bpm = hr_val
                dpg.set_value("hr_display", f"{hr_val:.1f} BPM")
                
                # Update Audio
                if self.audio_enabled:
                    self.audio_feedback.update_hr(hr_val)
                
                # Update Plot Data
                self.interpolated_hr_x.append(current_time)
                self.interpolated_hr_y.append(hr_val)
                
                dpg.set_value("hr_series", [list(self.interpolated_hr_x), list(self.interpolated_hr_y)])
                
                # Auto-scroll X axis
                if len(self.interpolated_hr_x) > 0:
                    dpg.set_axis_limits("x_axis", max(0, current_time - 60), current_time + 5)

        if KEY_RMSSD in payload:
            self.current_rmssd = payload[KEY_RMSSD]
            dpg.set_value("rmssd_display", f"{self.current_rmssd:.1f} ms")
            
            # Update History Plot
            self.rmssd_history.append(self.current_rmssd)
            # We need a time history for this too, or just use same X
            # For simplicity, let's assume 1:1 with HR updates or just append current time
            if len(self.rmssd_history) > len(self.time_history):
                 self.time_history.append(current_time)
            
            # Update RMSSD Plot
            if len(self.time_history) > 0 and len(self.rmssd_history) > 0:
                 # Ensure lengths match
                 min_len = min(len(self.time_history), len(self.rmssd_history))
                 dpg.set_value("rmssd_series", [list(self.time_history)[-min_len:], list(self.rmssd_history)[-min_len:]])
                 dpg.fit_axis_data("rmssd_x_axis")
                 dpg.fit_axis_data("rmssd_y_axis")

        if KEY_COHERENCE in payload:
            self.current_coherence = payload[KEY_COHERENCE]
            dpg.set_value("coherence_display", f"{self.current_coherence:.1f}")
            
            # Update Coherence Plot
            self.coherence_history.append(self.current_coherence)
            if len(self.time_history) > 0 and len(self.coherence_history) > 0:
                 min_len = min(len(self.time_history), len(self.coherence_history))
                 dpg.set_value("coherence_series", [list(self.time_history)[-min_len:], list(self.coherence_history)[-min_len:]])
                 dpg.fit_axis_data("coherence_x_axis")
                 dpg.fit_axis_data("coherence_y_axis")

        # Handle RR Intervals for Tachogram & Poincaré
        if 'rr_intervals' in payload and payload['rr_intervals']:
            # We need to handle the case where rr_intervals is a list of floats
            # The payload might contain a single float or a list
            rr_data = payload['rr_intervals']
            if not isinstance(rr_data, list):
                rr_data = [rr_data]
                
            for rr in rr_data:
                self.rr_history.append(rr)
                
                # Poincaré Logic (RR_n vs RR_n+1)
                # We need at least 2 points in history to plot one point on Poincaré
                if len(self.rr_history) >= 2:
                    # Get the last two points
                    rr_n = self.rr_history[-2]
                    rr_n1 = self.rr_history[-1]
                    self.poincare_x.append(rr_n)
                    self.poincare_y.append(rr_n1)
            
            # Update Tachogram
            # Using index for X axis as it's a sequence of beats
            rr_indices = list(range(len(self.rr_history)))
            dpg.set_value("rr_series", [rr_indices, list(self.rr_history)])
            dpg.fit_axis_data("rr_x_axis")
            dpg.fit_axis_data("rr_y_axis")
            
            # Update Poincaré
            if len(self.poincare_x) > 0:
                dpg.set_value("poincare_series", [list(self.poincare_x), list(self.poincare_y)])
                dpg.fit_axis_data("poincare_x_axis")
                dpg.fit_axis_data("poincare_y_axis")

    def update_pacer(self):
        # Generate Pacer Sine Wave for Plot
        current_time = time.time() - self.start_time
        
        if len(self.interpolated_hr_x) > 0:
            center_hr = 70.0
            if len(self.interpolated_hr_y) > 10:
                center_hr = np.mean(self.interpolated_hr_y)
                
            amplitude = 10.0 
            t = np.linspace(max(0, current_time - 60), current_time + 5, 200)
            freq = self.pacer_rate / 60.0
            y = center_hr + amplitude * np.sin(2 * np.pi * freq * t)
            
            dpg.set_value("pacer_series", [list(t), list(y)])

    def start_stream(self):
        try:
            self.ble_control_pipe.send(BLECommand(command="connect"))
        except Exception as e:
            logger.error(f"Failed to send start stream command: {e}")

    def stop_stream(self):
        try:
            self.ble_control_pipe.send(BLECommand(command="disconnect"))
        except Exception as e:
            logger.error(f"Failed to send stop stream command: {e}")

    def toggle_audio(self, sender, app_data):
        self.audio_enabled = app_data
        if self.audio_enabled:
            self.audio_feedback.start()
        else:
            self.audio_feedback.stop()

    def update_pacer_rate(self, sender, app_data):
        self.pacer_rate = app_data
        try:
            self.math_control_pipe.send(IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: self.pacer_rate}))
            # Also update the visual pacer engine
            # Assuming 60/BPM = cycle duration
            cycle_duration = 60.0 / self.pacer_rate
            # Distribute evenly for now or keep ratio
            part = cycle_duration / 2.0
            self.pacer.set_timing(part, 0, part, 0)
        except Exception as e:
            logger.error(f"Failed to update pacer rate: {e}")

    def update_pacer_settings(self, sender, app_data):
        inhale = dpg.get_value("pacer_inhale")
        hold_full = dpg.get_value("pacer_hold_full")
        exhale = dpg.get_value("pacer_exhale")
        hold_empty = dpg.get_value("pacer_hold_empty")
        
        self.pacer.set_timing(inhale, hold_full, exhale, hold_empty)
        
        # Update BPM slider to match calculated BPM
        bpm = self.pacer.get_bpm()
        self.pacer_rate = bpm
        dpg.set_value("pacer_slider", bpm)
        
        # Notify math process
        try:
            self.math_control_pipe.send(IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: bpm}))
        except Exception as e:
            logger.error(f"Failed to update pacer rate: {e}")

    # --- Assessment Protocol ---
    
    def start_assessment_protocol(self):
        if self.assessment_active:
            return
            
        self.assessment_active = True
        self.assessment_step = 0
        self.assessment_timer = time.time()
        dpg.set_value(self.assessment_status_tag, "Starting Assessment...")
        dpg.configure_item("assessment_progress", show=True)
        self.process_assessment_step()

    def process_assessment_step(self):
        if not self.assessment_active:
            return
            
        if self.assessment_step >= len(self.assessment_steps):
            self.finish_assessment()
            return
            
        step_name, duration_min, bpm = self.assessment_steps[self.assessment_step]
        
        dpg.set_value(self.assessment_status_tag, f"Step: {step_name} ({duration_min} min)")
        
        if bpm > 0:
            self.pacer_rate = bpm
            dpg.set_value("pacer_slider", bpm)
            # Update visual pacer
            cycle = 60.0 / bpm
            self.pacer.set_timing(cycle/2, 0, cycle/2, 0)
            
            try:
                self.math_control_pipe.send(IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: bpm}))
                tag = f"{bpm}_BPM"
                self.math_control_pipe.send(IPCMessage(MSG_CMD_START_ASSESSMENT, {KEY_ASSESSMENT_TAG: tag}))
            except Exception as e:
                logger.error(f"Failed to send assessment commands: {e}")
        else:
            try:
                self.math_control_pipe.send(IPCMessage(MSG_CMD_STOP_ASSESSMENT))
            except Exception as e:
                logger.error(f"Failed to send stop assessment command: {e}")
            
        duration_sec = duration_min * 60
        self.assessment_step_end_time = time.time() + duration_sec

    def update_assessment(self):
        if self.assessment_active:
            remaining = self.assessment_step_end_time - time.time()
            
            # Update progress bar
            total_duration = sum(s[1] * 60 for s in self.assessment_steps)
            # This is rough, but okay for now
            
            if remaining <= 0:
                self.assessment_step += 1
                self.process_assessment_step()
            else:
                dpg.set_value(self.assessment_status_tag, f"Step: {self.assessment_steps[self.assessment_step][0]} - {int(remaining)}s remaining")

    def finish_assessment(self):
        self.assessment_active = False
        dpg.set_value(self.assessment_status_tag, "Assessment Complete. Analyzing...")
        dpg.configure_item("assessment_progress", show=False)
        try:
            self.math_control_pipe.send(IPCMessage(MSG_CMD_STOP_ASSESSMENT))
        except Exception as e:
            logger.error(f"Failed to send stop assessment command: {e}")

    def handle_assessment_result(self, payload):
        optimal_bpm = payload.get(KEY_OPTIMAL_BPM)
        dpg.set_value(self.assessment_status_tag, f"Assessment Complete. Optimal BPM: {optimal_bpm}")
        self.pacer_rate = optimal_bpm
        dpg.set_value("pacer_slider", optimal_bpm)
        cycle = 60.0 / optimal_bpm
        self.pacer.set_timing(cycle/2, 0, cycle/2, 0)
        try:
            self.math_control_pipe.send(IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: optimal_bpm}))
        except Exception as e:
            logger.error(f"Failed to update pacer rate: {e}")

    # --- User & Session Management ---

    def _load_users(self):
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT user_id, username FROM users")
            rows = cursor.fetchall()
            self.users = [{'user_id': r[0], 'username': r[1]} for r in rows]
        except Exception as e:
            logger.error(f"Error loading users: {e}")
            self.users = []

    def _on_user_selected(self, sender, app_data):
        username = app_data
        for user in self.users:
            if user['username'] == username:
                self.current_user_id = user['user_id']
                break

    def create_user_management_window(self):
        with dpg.window(label="User Management", modal=True, width=400, height=300):
            dpg.add_text("Create New User")
            dpg.add_input_text(label="Username", tag="new_username")
            dpg.add_input_text(label="Email", tag="new_email")
            dpg.add_button(label="Create", callback=self._create_user)

    def _create_user(self):
        username = dpg.get_value("new_username")
        email = dpg.get_value("new_email")
        if username:
            try:
                user_id = self.db.create_user(username, email)
                self.current_user_id = user_id
                self._load_users()
                dpg.configure_item("user_combo", items=[u['username'] for u in self.users])
                dpg.set_value("user_combo", username)
                dpg.delete_item(dpg.last_container())
            except Exception as e:
                logger.error(f"Error creating user: {e}")

    def handle_connect_button(self):
        if not self.is_connected:
            self.start_stream()
            dpg.set_value("status_text", "Connecting...")
            dpg.configure_item("status_text", color=(255, 255, 0))
        else:
            self.stop_stream()
            dpg.set_value("status_text", "Disconnecting...")

    def handle_session_toggle(self):
        if not self.is_recording:
            if self.current_user_id is None:
                return
            self.current_session_id = self.db.create_session(self.current_user_id)
            self.is_recording = True
            dpg.configure_item("session_btn", label="Stop Session")
        else:
            if self.current_session_id:
                self.db.end_session(self.current_session_id)
            self.is_recording = False
            self.current_session_id = None
            dpg.configure_item("session_btn", label="Start Session")

    def poll_ble_status(self):
        try:
            while self.ble_control_pipe.poll():
                msg = self.ble_control_pipe.recv()
                if isinstance(msg, dict):
                    if "status" in msg:
                        status = msg["status"]
                        if status == "connected":
                            self.is_connected = True
                            dpg.set_value("status_text", "Connected")
                            dpg.configure_item("status_text", color=(0, 255, 0))
                            dpg.configure_item("connect_btn", label="Disconnect")
                            dpg.configure_item("session_btn", show=True)
                        elif status == "disconnected":
                            self.is_connected = False
                            dpg.set_value("status_text", "Disconnected")
                            dpg.configure_item("status_text", color=(255, 0, 0))
                            dpg.configure_item("connect_btn", label="Connect")
                            dpg.configure_item("session_btn", show=False)
                            self.is_recording = False
                    if "battery" in msg:
                        dpg.set_value("battery_text", f"{msg['battery']}%")
        except Exception as e:
            logger.error(f"Error receiving BLE status: {e}")

    def update_ecg_plot(self):
        if self.shm is None:
            self._attach_shared_memory()
            return

        if self.ecg_display_buffer is not None:
            try:
                data = np.array(self.ecg_display_buffer)
                dpg.set_value("ecg_series", [self.ecg_plot_data_x, data])
            except Exception as e:
                logger.error(f"Error updating plot: {e}")

    def handle_save_preset(self):
        if self.current_user_id is None:
            return
        preset_name = dpg.get_value("preset_name_input")
        if not preset_name:
            return
        try:
            self.db.save_preset(self.current_user_id, preset_name, self.config)
        except Exception as e:
            logger.error(f"Error saving preset: {e}")

    def create_load_preset_window(self):
        if self.current_user_id is None:
            return
        try:
            presets = self.db.get_user_presets(self.current_user_id)
            if dpg.does_item_exist("load_preset_window"):
                dpg.delete_item("load_preset_window")
            with dpg.window(label="Load Preset", modal=True, width=300, height=200, tag="load_preset_window"):
                if not presets:
                    dpg.add_text("No presets found.")
                else:
                    for p in presets:
                        dpg.add_button(label=p['preset_name'], user_data=p['preset_name'], callback=self._on_preset_selected, width=-1)
                dpg.add_spacer(height=10)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("load_preset_window"))
        except Exception as e:
            logger.error(f"Error loading presets: {e}")

    def _on_preset_selected(self, sender, app_data, user_data):
        preset_name = user_data
        try:
            config = self.db.load_preset(self.current_user_id, preset_name)
            if config:
                self.config = config
                self.math_control_pipe.send(self.config)
                dpg.delete_item("load_preset_window")
        except Exception as e:
            logger.error(f"Error loading preset: {e}")

    def create_history_window(self):
        if self.current_user_id is None:
            return
        try:
            history = self.db.get_session_history(self.current_user_id, limit=20)
            if dpg.does_item_exist("history_window"):
                dpg.delete_item("history_window")
            with dpg.window(label="Session History", width=600, height=400, tag="history_window"):
                if not history:
                    dpg.add_text("No sessions found.")
                else:
                    with dpg.table(header_row=True, resizable=True, policy=dpg.mvTable_SizingStretchProp):
                        dpg.add_table_column(label="ID", width_fixed=True)
                        dpg.add_table_column(label="Start Time")
                        dpg.add_table_column(label="End Time")
                        dpg.add_table_column(label="Avg HR")
                        dpg.add_table_column(label="Avg RMSSD")
                        for session in history:
                            with dpg.table_row():
                                dpg.add_text(str(session['session_id']))
                                dpg.add_text(str(session['start_time']))
                                dpg.add_text(str(session['end_time']) if session['end_time'] else "Active")
                                dpg.add_text(f"{session['avg_hr']:.1f}" if session['avg_hr'] else "-")
                                dpg.add_text(f"{session['avg_rmssd']:.1f}" if session['avg_rmssd'] else "-")
        except Exception as e:
            logger.error(f"Error loading history: {e}")

def start_ui(data_pipe, ble_control_pipe, math_control_pipe, shm_name, auto_connect=False):
    ui = UIManager(data_pipe, ble_control_pipe, math_control_pipe, shm_name, auto_connect)
    ui.run()
