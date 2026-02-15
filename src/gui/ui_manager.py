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
    IPCMessage, BLECommand, ACCBatch, ECGBatch, MSG_TERMINATE, MSG_DATA_UPDATE,
    MSG_HEARTBEAT_BLINK,
    MSG_CMD_START_STREAM, MSG_CMD_STOP_STREAM, MSG_CMD_SET_PACER_TARGET,
    MSG_CMD_START_ASSESSMENT, MSG_CMD_STOP_ASSESSMENT, MSG_ASSESSMENT_RESULT,
    KEY_TIMESTAMP, KEY_RAW_ECG, KEY_RMSSD, KEY_INTERPOLATED_HR, KEY_COHERENCE,
    KEY_IS_ARTIFACT, KEY_PACER_BPM, KEY_ASSESSMENT_TAG, KEY_OPTIMAL_BPM,
    ProcessedData, ProcessingConfig, CommandType, SystemCommand
)
from src.gui.audio_feedback import AudioFeedback
from src.gui.charts import (
    BiofeedbackChart, HeartbeatChart, TachogramChart, PoincareChart,
    RMSSDHistoryChart, SDNNHistoryChart, CoherenceHistoryChart,
    ACCChart, ECGChart
)
from src.gui.led_ball import LEDBallController
from src.database.db_manager import DatabaseManager
from src.gui.pacer import PacerEngine

logger = logging.getLogger(__name__)


class UIManager:
    def __init__(self, data_pipe: Connection, ble_control_pipe: Connection,
                 math_control_pipe: Connection, shm_name: str, auto_connect=False):
        self.data_pipe = data_pipe
        self.ble_control_pipe = ble_control_pipe
        self.math_control_pipe = math_control_pipe
        self.shm_name = shm_name
        self.auto_connect = auto_connect
        self.running = False

        # Audio Feedback
        self.audio_feedback = AudioFeedback()
        self.audio_enabled = False

        # LED Ball Control
        self.led_ball = LEDBallController()
        self.led_ball_enabled = False

        # Shared memory for ECG display (legacy, kept for compatibility)
        self.shm: Optional[shared_memory.SharedMemory] = None
        self.ecg_display_buffer: Optional[np.ndarray] = None

        # State
        self.current_bpm = 0.0
        self.current_rmssd = 0.0
        self.current_coherence = 0.0
        self.pacer_rate = 6.0
        self.start_time = time.time()
        self.is_connected = False
        self.is_recording = False

        # Heartbeat blink indicator state
        self._blink_time = 0.0       # time.time() when last blink was triggered
        self._blink_duration = 0.15  # seconds for the red flash to fade back to black

        # Assessment State
        self.assessment_active = False
        self.assessment_step = 0
        self.assessment_timer = 0.0
        self.assessment_step_end_time = 0.0
        self.assessment_steps = [
            ("BASELINE", 2.0, 0),
            ("STEP_1", 2.0, 6.5),
            ("REST", 0.5, 0),
            ("STEP_2", 2.0, 6.0),
            ("REST", 0.5, 0),
            ("STEP_3", 2.0, 5.5),
            ("REST", 0.5, 0),
            ("STEP_4", 2.0, 5.0),
            ("REST", 0.5, 0),
            ("STEP_5", 2.0, 4.5),
        ]

        # Pacer Engine
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

        # Chart widgets (collapsible)
        self.biofeedback_chart = BiofeedbackChart()
        self.heartbeat_chart = HeartbeatChart()
        self.tachogram_chart = TachogramChart()
        self.poincare_chart = PoincareChart()
        self.rmssd_chart = RMSSDHistoryChart()
        self.sdnn_chart = SDNNHistoryChart()
        self.coherence_chart = CoherenceHistoryChart()
        self.acc_chart = ACCChart()
        self.ecg_chart = ECGChart()

        # UI Element Tags
        self.window_tag = "Primary Window"
        self.assessment_status_tag = "Assessment Status"

    def setup_ui(self):
        dpg.create_context()
        dpg.create_viewport(title="Polar H10 HRVB", width=1280, height=900)
        dpg.setup_dearpygui()

        self._load_users()

        with dpg.window(tag=self.window_tag, label="HRV Biofeedback", no_title_bar=True):

            # --- Top Bar ---
            self._build_top_bar()
            dpg.add_separator()

            # --- Main Layout ---
            with dpg.group(horizontal=True, height=350):
                self._build_left_panel()
                self._build_center_panel()
                self._build_right_panel()

            dpg.add_separator()

            # --- Charts Section (collapsible, no tabs) ---
            with dpg.child_window(width=-1, height=-1, border=True, tag="charts_area"):
                dpg.add_text("Charts", color=(0, 255, 255))
                dpg.add_separator()
                self.biofeedback_chart.build("charts_area")
                self.heartbeat_chart.build("charts_area")
                self.acc_chart.build("charts_area")
                self.ecg_chart.build("charts_area")
                self.tachogram_chart.build("charts_area")
                self.poincare_chart.build("charts_area")
                self.rmssd_chart.build("charts_area")
                self.sdnn_chart.build("charts_area")
                self.coherence_chart.build("charts_area")

        dpg.set_primary_window(self.window_tag, True)

    def _build_top_bar(self):
        with dpg.group(horizontal=True):
            dpg.add_text("Polar Flow-Sync", color=(0, 191, 255))
            dpg.add_spacer(width=20)
            dpg.add_text("Status: ")
            dpg.add_text("Disconnected", tag="status_text", color=(255, 0, 0))
            dpg.add_spacer(width=20)
            dpg.add_text("Battery: ")
            dpg.add_text("N/A", tag="battery_text")

            dpg.add_spacer(width=50)
            dpg.add_combo(
                items=[u['username'] for u in self.users],
                tag="user_combo", width=200,
                callback=self._on_user_selected,
                default_value="Select User"
            )
            dpg.add_button(label="+", callback=self.create_user_management_window, width=30)

            dpg.add_spacer(width=20)
            dpg.add_button(label="Connect", tag="connect_btn",
                           callback=self.handle_connect_button, width=100)
            dpg.add_button(label="Start Session", tag="session_btn",
                           callback=self.handle_session_toggle, show=False, width=120)
            dpg.add_button(label="History", callback=self.create_history_window, width=80)
            dpg.add_checkbox(label="Audio Feedback", callback=self.toggle_audio,
                             default_value=False)

    def _build_left_panel(self):
        with dpg.child_window(width=250, height=-1, border=True):
            dpg.add_text("Pacer Settings")
            dpg.add_slider_float(
                label="Breathing Rate (BPM)", default_value=6.0,
                min_value=4.0, max_value=10.0,
                callback=self.update_pacer_rate, tag="pacer_slider"
            )

            dpg.add_spacer(height=10)
            dpg.add_text("Breathing Cycle (s)")
            dpg.add_input_float(label="Inhale", default_value=4.0, step=0.5,
                                width=100, callback=self.update_pacer_settings,
                                tag="pacer_inhale")
            dpg.add_input_float(label="Hold (Full)", default_value=4.0, step=0.5,
                                width=100, callback=self.update_pacer_settings,
                                tag="pacer_hold_full")
            dpg.add_input_float(label="Exhale", default_value=4.0, step=0.5,
                                width=100, callback=self.update_pacer_settings,
                                tag="pacer_exhale")
            dpg.add_input_float(label="Hold (Empty)", default_value=4.0, step=0.5,
                                width=100, callback=self.update_pacer_settings,
                                tag="pacer_hold_empty")

            dpg.add_spacer(height=20)
            dpg.add_separator()
            dpg.add_text("Resonance Assessment")
            dpg.add_button(label="Start Assessment",
                           callback=self.start_assessment_protocol)
            dpg.add_text("Status: Idle", tag=self.assessment_status_tag)
            dpg.add_progress_bar(tag="assessment_progress", default_value=0.0,
                                 width=-1, show=False)

            dpg.add_spacer(height=20)
            dpg.add_separator()
            dpg.add_text("Presets")
            dpg.add_input_text(tag="preset_name_input", width=150, hint="Preset Name")
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=self.handle_save_preset, width=70)
                dpg.add_button(label="Load", callback=self.create_load_preset_window,
                               width=70)

            dpg.add_spacer(height=20)
            dpg.add_separator()
            dpg.add_text("LED Ball")
            dpg.add_checkbox(label="Enable LED Ball", tag="led_ball_checkbox",
                             default_value=False, callback=self._toggle_led_ball)
            dpg.add_input_text(label="IP", tag="led_ball_ip_input",
                               default_value=self.led_ball.ip, width=150,
                               callback=self._update_led_ball_ip,
                               on_enter=True)

    def _build_center_panel(self):
        with dpg.child_window(width=-300, height=-1, border=False):
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=50)
                with dpg.group():
                    dpg.add_text("COHERENCE", color=(150, 150, 150))
                    dpg.add_text("0.0", tag="coherence_display", color=(0, 255, 0))
                dpg.add_spacer(width=100)
                with dpg.group():
                    dpg.add_text("HEART RATE", color=(150, 150, 150))
                    with dpg.group(horizontal=True):
                        dpg.add_text("0 BPM", tag="hr_display", color=(255, 50, 50))
                        dpg.add_spacer(width=10)
                        # Heartbeat blink indicator (small circle)
                        with dpg.drawlist(width=20, height=20, tag="hb_blink_drawlist"):
                            dpg.draw_circle(center=(10, 10), radius=8,
                                            color=(255, 255, 255, 255),
                                            fill=(0, 0, 0, 255),
                                            tag="hb_blink_circle")

            dpg.add_spacer(height=10)

            # Pacer Visuals (Circle)
            with dpg.drawlist(width=600, height=300, tag="pacer_drawlist"):
                self.pacer.setup_draw_layer("pacer_drawlist")

    def _build_right_panel(self):
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

    # --- Main Loop ---

    def run(self):
        self.setup_ui()
        dpg.show_viewport()
        self.running = True

        threading.Thread(target=self.process_incoming_data, daemon=True).start()

        if self.auto_connect:
            logger.info("Auto-connecting to device...")
            self.start_stream()

        while dpg.is_dearpygui_running() and self.running:
            self.update_pacer()
            self.update_assessment()
            self.poll_ble_status()
            self._update_heartbeat_blink()

            # Update Pacer Visuals
            if self.pacer_active:
                self.pacer.update(600, 300)

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
        except Exception:
            pass

    # --- Data Processing ---

    def process_incoming_data(self):
        while self.running:
            try:
                if self.data_pipe.poll():
                    msg = self.data_pipe.recv()
                    if isinstance(msg, ACCBatch):
                        self.handle_acc_data(msg)
                    elif isinstance(msg, ECGBatch):
                        self.handle_ecg_data(msg)
                    elif isinstance(msg, IPCMessage):
                        if msg.type == MSG_DATA_UPDATE:
                            self.handle_data_update(msg.payload)
                        elif msg.type == MSG_HEARTBEAT_BLINK:
                            self._blink_time = time.time()
                            if self.led_ball_enabled:
                                self.led_ball.blink(self._blink_duration)
                        elif msg.type == MSG_ASSESSMENT_RESULT:
                            self.handle_assessment_result(msg.payload)
                    elif isinstance(msg, ProcessedData):
                        self.handle_processed_data(msg)
                else:
                    time.sleep(0.01)
            except EOFError:
                break
            except Exception as e:
                logger.error(f"Error processing incoming data: {e}")
                time.sleep(0.1)

    def handle_processed_data(self, data: ProcessedData):
        """Handle ProcessedData from signal processor."""
        current_time = time.time() - self.start_time

        # Update HR display
        hr_val = data.heart_rate
        if hr_val > 0:
            self.current_bpm = hr_val
            dpg.set_value("hr_display", f"{hr_val:.1f} BPM")
            if self.audio_enabled:
                self.audio_feedback.update_hr(hr_val)

            # Biofeedback chart
            self.biofeedback_chart.hr_x.append(current_time)
            self.biofeedback_chart.hr_y.append(hr_val)
            dpg.set_value("hr_series", [
                list(self.biofeedback_chart.hr_x),
                list(self.biofeedback_chart.hr_y)
            ])
            if len(self.biofeedback_chart.hr_x) > 0:
                dpg.set_axis_limits("bf_x_axis",
                                    max(0, current_time - 60), current_time + 5)

        # RMSSD
        self.current_rmssd = data.hrv_rmssd
        dpg.set_value("rmssd_display", f"{self.current_rmssd:.1f} ms")
        dpg.set_value("sdnn_display", f"{data.hrv_sdnn:.1f} ms")
        dpg.set_value("quality_bar", data.quality_score)

        # Coherence
        self.current_coherence = data.coherence_score
        dpg.set_value("coherence_display", f"{self.current_coherence:.1f}")

        # Metrics history
        self.rmssd_chart.add_data(current_time, self.current_rmssd)
        self.rmssd_chart.update_plot()
        self.sdnn_chart.add_data(current_time, data.hrv_sdnn)
        self.sdnn_chart.update_plot()
        self.coherence_chart.add_data(current_time, self.current_coherence)
        self.coherence_chart.update_plot()

        # RR intervals -> heartbeat chart, tachogram, poincaré
        if data.rr_intervals:
            self.heartbeat_chart.add_beats(data.timestamp, data.rr_intervals,
                                           self.start_time)
            self.heartbeat_chart.update_plot(current_time)

            self.tachogram_chart.add_rr(data.rr_intervals)
            self.tachogram_chart.update_plot()

            self.poincare_chart.add_rr(data.rr_intervals)
            self.poincare_chart.update_plot()

        # Save to DB if recording
        if self.is_recording and self.current_session_id:
            self.db.save_hrv_data(self.current_session_id, data)

    def handle_data_update(self, payload):
        """Handle legacy IPCMessage DATA_UPDATE payloads."""
        current_time = time.time() - self.start_time

        if KEY_INTERPOLATED_HR in payload:
            hr_val = payload[KEY_INTERPOLATED_HR]
            if hr_val > 0:
                self.current_bpm = hr_val
                dpg.set_value("hr_display", f"{hr_val:.1f} BPM")
                if self.audio_enabled:
                    self.audio_feedback.update_hr(hr_val)
                self.biofeedback_chart.hr_x.append(current_time)
                self.biofeedback_chart.hr_y.append(hr_val)
                dpg.set_value("hr_series", [
                    list(self.biofeedback_chart.hr_x),
                    list(self.biofeedback_chart.hr_y)
                ])
                if len(self.biofeedback_chart.hr_x) > 0:
                    dpg.set_axis_limits("bf_x_axis",
                                        max(0, current_time - 60), current_time + 5)

        if KEY_RMSSD in payload:
            self.current_rmssd = payload[KEY_RMSSD]
            dpg.set_value("rmssd_display", f"{self.current_rmssd:.1f} ms")

        if KEY_COHERENCE in payload:
            self.current_coherence = payload[KEY_COHERENCE]
            dpg.set_value("coherence_display", f"{self.current_coherence:.1f}")

        # Metrics history
        self.rmssd_chart.add_data(current_time, self.current_rmssd)
        self.rmssd_chart.update_plot()
        self.sdnn_chart.add_data(current_time, 0.0)  # SDNN not in legacy payload
        self.sdnn_chart.update_plot()
        self.coherence_chart.add_data(current_time, self.current_coherence)
        self.coherence_chart.update_plot()

        if 'rr_intervals' in payload and payload['rr_intervals']:
            rr_data = payload['rr_intervals']
            if not isinstance(rr_data, list):
                rr_data = [rr_data]
            self.heartbeat_chart.add_beats(time.time(), rr_data, self.start_time)
            self.heartbeat_chart.update_plot(current_time)
            self.tachogram_chart.add_rr(rr_data)
            self.tachogram_chart.update_plot()
            self.poincare_chart.add_rr(rr_data)
            self.poincare_chart.update_plot()

    def handle_acc_data(self, batch: ACCBatch):
        """Handle accelerometer data from BLE."""
        current_time = time.time() - self.start_time
        self.acc_chart.add_samples(batch.timestamp_unix, batch.samples,
                                   batch.sample_rate, self.start_time)
        self.acc_chart.update_plot(current_time)

    def handle_ecg_data(self, batch: ECGBatch):
        """Handle ECG data from BLE PMD service."""
        current_time = time.time() - self.start_time
        samples = batch.samples.tolist() if hasattr(batch.samples, 'tolist') else list(batch.samples)
        self.ecg_chart.add_samples(batch.timestamp_unix, samples,
                                   batch.sample_rate, self.start_time)
        self.ecg_chart.update_plot(current_time)

    # --- Heartbeat Blink ---

    def _update_heartbeat_blink(self):
        """Update the heartbeat indicator circle color each frame.

        On blink: fill flashes red, then fades linearly back to black
        over self._blink_duration seconds.  The external LED ball is
        driven via blink() in process_incoming_data (once per beat).
        """
        elapsed = time.time() - self._blink_time
        if elapsed < self._blink_duration:
            # Fade from 255 (red) → 0 (black) over the duration
            t = elapsed / self._blink_duration
            red = int(255 * (1.0 - t))
            dpg.configure_item("hb_blink_circle", fill=(red, 0, 0, 255))
        else:
            # Resting state: black fill
            dpg.configure_item("hb_blink_circle", fill=(0, 0, 0, 255))

    # --- Pacer ---

    def update_pacer(self):
        current_time = time.time() - self.start_time
        if len(self.biofeedback_chart.hr_x) > 0:
            center_hr = 70.0
            if len(self.biofeedback_chart.hr_y) > 10:
                center_hr = np.mean(self.biofeedback_chart.hr_y)
            amplitude = 10.0
            t = np.linspace(max(0, current_time - 60), current_time + 5, 200)
            freq = self.pacer_rate / 60.0
            y = center_hr + amplitude * np.sin(2 * np.pi * freq * t)
            dpg.set_value("pacer_series", [list(t), list(y)])

    # --- BLE Control ---

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

    def _toggle_led_ball(self, sender, app_data):
        self.led_ball_enabled = app_data
        logger.info(f"LED ball {'enabled' if app_data else 'disabled'}")

    def _update_led_ball_ip(self, sender, app_data):
        ip = app_data.strip()
        if ip:
            self.led_ball.set_ip(ip)
            logger.info(f"LED ball IP set to {ip}")

    def update_pacer_rate(self, sender, app_data):
        self.pacer_rate = app_data
        try:
            self.math_control_pipe.send(
                IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: self.pacer_rate}))
            cycle_duration = 60.0 / self.pacer_rate
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
        bpm = self.pacer.get_bpm()
        self.pacer_rate = bpm
        dpg.set_value("pacer_slider", bpm)
        try:
            self.math_control_pipe.send(
                IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: bpm}))
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
        dpg.set_value(self.assessment_status_tag,
                      f"Step: {step_name} ({duration_min} min)")
        if bpm > 0:
            self.pacer_rate = bpm
            dpg.set_value("pacer_slider", bpm)
            cycle = 60.0 / bpm
            self.pacer.set_timing(cycle / 2, 0, cycle / 2, 0)
            try:
                self.math_control_pipe.send(
                    IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: bpm}))
                tag = f"{bpm}_BPM"
                self.math_control_pipe.send(
                    IPCMessage(MSG_CMD_START_ASSESSMENT, {KEY_ASSESSMENT_TAG: tag}))
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
            if remaining <= 0:
                self.assessment_step += 1
                self.process_assessment_step()
            else:
                step_name = self.assessment_steps[self.assessment_step][0]
                dpg.set_value(self.assessment_status_tag,
                              f"Step: {step_name} - {int(remaining)}s remaining")

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
        dpg.set_value(self.assessment_status_tag,
                      f"Assessment Complete. Optimal BPM: {optimal_bpm}")
        self.pacer_rate = optimal_bpm
        dpg.set_value("pacer_slider", optimal_bpm)
        cycle = 60.0 / optimal_bpm
        self.pacer.set_timing(cycle / 2, 0, cycle / 2, 0)
        try:
            self.math_control_pipe.send(
                IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: optimal_bpm}))
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
                dpg.configure_item("user_combo",
                                   items=[u['username'] for u in self.users])
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

    # --- Presets ---

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
            with dpg.window(label="Load Preset", modal=True, width=300, height=200,
                            tag="load_preset_window"):
                if not presets:
                    dpg.add_text("No presets found.")
                else:
                    for p in presets:
                        dpg.add_button(label=p['preset_name'],
                                       user_data=p['preset_name'],
                                       callback=self._on_preset_selected, width=-1)
                dpg.add_spacer(height=10)
                dpg.add_button(label="Cancel",
                               callback=lambda: dpg.delete_item("load_preset_window"))
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

    # --- History ---

    def create_history_window(self):
        if self.current_user_id is None:
            return
        try:
            history = self.db.get_session_history(self.current_user_id, limit=20)
            if dpg.does_item_exist("history_window"):
                dpg.delete_item("history_window")
            with dpg.window(label="Session History", width=600, height=400,
                            tag="history_window"):
                if not history:
                    dpg.add_text("No sessions found.")
                else:
                    with dpg.table(header_row=True, resizable=True,
                                   policy=dpg.mvTable_SizingStretchProp):
                        dpg.add_table_column(label="ID", width_fixed=True)
                        dpg.add_table_column(label="Start Time")
                        dpg.add_table_column(label="End Time")
                        dpg.add_table_column(label="Avg HR")
                        dpg.add_table_column(label="Avg RMSSD")
                        for session in history:
                            with dpg.table_row():
                                dpg.add_text(str(session['session_id']))
                                dpg.add_text(str(session['start_time']))
                                end = session['end_time']
                                dpg.add_text(str(end) if end else "Active")
                                avg_hr = session['avg_hr']
                                dpg.add_text(f"{avg_hr:.1f}" if avg_hr else "-")
                                avg_rmssd = session['avg_rmssd']
                                dpg.add_text(f"{avg_rmssd:.1f}" if avg_rmssd else "-")
        except Exception as e:
            logger.error(f"Error loading history: {e}")


def start_ui(data_pipe, ble_control_pipe, math_control_pipe, shm_name,
             auto_connect=False):
    ui = UIManager(data_pipe, ble_control_pipe, math_control_pipe, shm_name,
                   auto_connect)
    ui.run()
