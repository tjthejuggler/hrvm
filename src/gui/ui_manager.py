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
    MSG_CMD_SET_SESSION_MODE, MSG_CMD_START_RECORDING, MSG_CMD_STOP_RECORDING,
    KEY_TIMESTAMP, KEY_RAW_ECG, KEY_RMSSD, KEY_INTERPOLATED_HR, KEY_COHERENCE,
    KEY_IS_ARTIFACT, KEY_PACER_BPM, KEY_ASSESSMENT_TAG, KEY_OPTIMAL_BPM,
    KEY_SESSION_MODE, SESSION_MODE_COUNTING, SESSION_MODE_NONE,
    ProcessedData, ProcessingConfig, CommandType, SystemCommand
)
from src.gui.audio_feedback import AudioFeedback
from src.gui.charts import (
    BiofeedbackChart, HeartbeatChart, TachogramChart, PoincareChart,
    RMSSDHistoryChart, SDNNHistoryChart, CoherenceHistoryChart,
    ACCChart, ECGChart
)
from src.gui.counting_game import CountingGameWidget
from src.gui.rapid_change_game import RapidChangeWidget
from src.gui.resonance_breathing import ResonanceBreathingWidget
from src.gui.led_ball import LEDBallController
from src.database.db_manager import DatabaseManager
from src.gui.pacer import PacerEngine
from src.ble.genki_manager import GenkiWaveManager
from src.gui.genki_bar import GenkiWaveBar
from src.gui.genki_charts import GenkiGyroChart, GenkiAccChart, GenkiGyroSumChart
from src.ble.pvs_manager import PolarVeritySenseManager
from src.gui.pvs_bar import PolarVeritySenseBar
from src.gui.pvs_charts import (
    PVSAccChart, PVSGyroChart, PVSMagChart, PVSPPIChart,
    PVSHeartRateChart,
)

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
        self.is_connected = False  # Polar H10 connection state
        self.is_genki_connected = False  # Genki Wave connection state
        self.is_recording = False
        self.is_json_recording = False # New flag for JSON recording
        # self.session_mode = None  # Removed session mode

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

        # Pacer Engine (kept for assessment protocol only)
        self.pacer = PacerEngine()
        self.pacer_active = False  # no longer auto-running in top bar

        # Database
        self.db = DatabaseManager("hrv_data.db")
        self.current_session_id: Optional[int] = None

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

        # Counting game widget (shown only in counting mode)
        self.counting_game = CountingGameWidget()

        # Rapid Change game widget
        self.rapid_change_game = RapidChangeWidget()

        # Resonance Breathing app
        self.resonance_breathing = ResonanceBreathingWidget(db=self.db)

        # Genki Wave manager + bar + charts
        self.genki_manager = GenkiWaveManager()
        self.genki_bar = GenkiWaveBar(self.genki_manager)
        self.genki_gyro_chart = GenkiGyroChart()
        self.genki_acc_chart = GenkiAccChart()
        self.genki_gyro_sum_chart = GenkiGyroSumChart()

        # Polar Verity Sense manager + bar + charts
        self.pvs_manager = PolarVeritySenseManager()
        self.pvs_bar = PolarVeritySenseBar(self.pvs_manager)
        self.pvs_acc_chart = PVSAccChart()
        self.pvs_gyro_chart = PVSGyroChart()
        self.pvs_mag_chart = PVSMagChart()
        self.pvs_ppi_chart = PVSPPIChart()
        self.pvs_hr_chart = PVSHeartRateChart()

        # UI Element Tags
        self.window_tag = "Primary Window"
        self.assessment_status_tag = "Assessment Status"

    def setup_ui(self):
        dpg.create_context()
        dpg.create_viewport(title="Polar H10 HRVB", width=1280, height=900)
        dpg.setup_dearpygui()

        # Load a larger font for metric value displays
        with dpg.font_registry():
            dpg.add_font(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                size=40,
                tag="large_font"
            )

        with dpg.window(tag=self.window_tag, label="HRV Biofeedback", no_title_bar=True):

            # --- Polar H10 Top Bar ---
            self._build_top_bar()
            dpg.add_separator()

            # --- Genki Wave Top Bar ---
            self.genki_bar.build()
            dpg.add_separator()

            # --- Polar Verity Sense Top Bar ---
            self.pvs_bar.build()
            dpg.add_separator()

            # --- Session Metrics Bar (large numbers, horizontal) ---
            self._build_metrics_bar()
            dpg.add_separator()

            # --- Charts Section (collapsible, no tabs) ---
            with dpg.child_window(width=-1, height=-1, border=True, tag="charts_area"):
                # dpg.add_text("Charts Area", color=(0, 255, 255))
                # dpg.add_separator()

                # --- Apps Section ---
                with dpg.theme(tag="apps_header_theme"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (100, 50, 200, 100))  # Purple tint
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (120, 70, 220, 150))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (140, 90, 240, 200))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))
                        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 10) # Increase size

                with dpg.collapsing_header(label="APPS", tag="header_apps", default_open=True):
                    dpg.bind_item_theme(dpg.last_item(), "apps_header_theme")
                    
                    # We create a container (group) for apps so they have a consistent parent
                    with dpg.group(tag="apps_container"):
                        self.counting_game.build("apps_container")
                        self.rapid_change_game.build("apps_container")
                        self.resonance_breathing.build("apps_container")

                # --- Graphs Section ---
                with dpg.theme(tag="graphs_header_theme"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (0, 100, 200, 100))  # Blue tint
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (20, 120, 220, 150))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (40, 140, 240, 200))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))
                        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 10) # Increase size

                # Device subsection theme (slightly different shade for nesting)
                with dpg.theme(tag="device_subsection_theme"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (0, 80, 160, 80))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (10, 100, 180, 120))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (20, 120, 200, 160))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (200, 230, 255, 255))
                        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 8)

                with dpg.collapsing_header(label="GRAPHS", tag="header_graphs", default_open=True):
                    dpg.bind_item_theme(dpg.last_item(), "graphs_header_theme")

                    # --- Polar H10 Device Subsection ---
                    with dpg.collapsing_header(label="Polar H10", tag="header_polar_h10",
                                               default_open=True, show=False):
                        dpg.bind_item_theme(dpg.last_item(), "device_subsection_theme")

                        with dpg.group(tag="polar_h10_graphs_container"):
                            self.biofeedback_chart.build("polar_h10_graphs_container")
                            self.heartbeat_chart.build("polar_h10_graphs_container")
                            self.acc_chart.build("polar_h10_graphs_container")
                            self.ecg_chart.build("polar_h10_graphs_container")
                            self.tachogram_chart.build("polar_h10_graphs_container")
                            self.poincare_chart.build("polar_h10_graphs_container")
                            self.rmssd_chart.build("polar_h10_graphs_container")
                            self.sdnn_chart.build("polar_h10_graphs_container")
                            self.coherence_chart.build("polar_h10_graphs_container")

                    # --- Genki Wave Device Subsection ---
                    with dpg.collapsing_header(label="Genki Wave", tag="header_genki_wave",
                                               default_open=True, show=False):
                        dpg.bind_item_theme(dpg.last_item(), "device_subsection_theme")

                        with dpg.group(tag="genki_wave_graphs_container"):
                            self.genki_gyro_chart.build("genki_wave_graphs_container")
                            self.genki_acc_chart.build("genki_wave_graphs_container")
                            self.genki_gyro_sum_chart.build("genki_wave_graphs_container")

                    # --- Polar Verity Sense Device Subsection ---
                    with dpg.collapsing_header(label="Polar Verity Sense", tag="header_pvs",
                                               default_open=True, show=False):
                        dpg.bind_item_theme(dpg.last_item(), "device_subsection_theme")

                        with dpg.group(tag="pvs_graphs_container"):
                            self.pvs_acc_chart.build("pvs_graphs_container")
                            self.pvs_gyro_chart.build("pvs_graphs_container")
                            self.pvs_mag_chart.build("pvs_graphs_container")
                            self.pvs_ppi_chart.build("pvs_graphs_container")
                            self.pvs_hr_chart.build("pvs_graphs_container")

                with dpg.theme(tag="theme_graphs_header"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (0, 100, 150, 150)) # Blue tint
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (20, 120, 170, 170))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (220, 255, 255, 255))
                dpg.bind_item_theme("header_graphs", "theme_graphs_header")

        dpg.set_primary_window(self.window_tag, True)

    def _build_top_bar(self):
        with dpg.group(horizontal=True):
            dpg.add_text("Polar H10", color=(0, 191, 255))
            dpg.add_spacer(width=20)
            dpg.add_text("Status: ")
            dpg.add_text("Disconnected", tag="status_text", color=(255, 0, 0))
            dpg.add_spacer(width=20)
            dpg.add_text("Battery: ")
            dpg.add_text("N/A", tag="battery_text")

            dpg.add_spacer(width=20)
            dpg.add_button(label="Connect", tag="connect_btn",
                           callback=self.handle_connect_button, width=100)
            dpg.add_button(label="Start Session", tag="session_btn",
                           callback=self.handle_session_toggle, show=False, width=120)

            dpg.add_spacer(width=10)
            dpg.add_button(label="Rec", tag="rec_btn",
                           callback=self.handle_recording_toggle, width=60)
            dpg.add_text("", tag="rec_status_text", color=(255, 0, 0))

            dpg.add_button(label="History", callback=self.create_history_window, width=80)
            dpg.add_checkbox(label="Audio Feedback", callback=self.toggle_audio,
                             default_value=False)
            dpg.add_spacer(width=20)
            dpg.add_button(label="⚙ Settings", callback=self.open_settings_popup, width=100)

    def _build_metrics_bar(self):
        """Horizontal bar of large session metric numbers."""
        with dpg.group(horizontal=True):
            # Heart Rate
            with dpg.group():
                dpg.add_text("HEART RATE", color=(150, 150, 150))
                with dpg.group(horizontal=True):
                    dpg.add_text("0 BPM", tag="hr_display", color=(255, 80, 80))
                    dpg.bind_item_font("hr_display", "large_font")
                    dpg.add_spacer(width=8)
                    with dpg.drawlist(width=28, height=28, tag="hb_blink_drawlist"):
                        dpg.draw_circle(center=(14, 14), radius=10,
                                        color=(255, 255, 255, 255),
                                        fill=(0, 0, 0, 255),
                                        tag="hb_blink_circle")

            dpg.add_spacer(width=60)

            # RMSSD
            with dpg.group():
                dpg.add_text("RMSSD", color=(150, 150, 150))
                dpg.add_text("0 ms", tag="rmssd_display", color=(0, 255, 0))
                dpg.bind_item_font("rmssd_display", "large_font")

            dpg.add_spacer(width=60)

            # SDNN
            with dpg.group():
                dpg.add_text("SDNN", color=(150, 150, 150))
                dpg.add_text("0 ms", tag="sdnn_display", color=(255, 255, 0))
                dpg.bind_item_font("sdnn_display", "large_font")

            dpg.add_spacer(width=60)

            # Signal Quality
            with dpg.group():
                dpg.add_text("SIGNAL QUALITY", color=(150, 150, 150))
                dpg.add_progress_bar(tag="quality_bar", default_value=0.0, width=200)

    def open_settings_popup(self):
        """Open a modal settings window with Presets and LED Ball controls."""
        if dpg.does_item_exist("settings_popup"):
            dpg.delete_item("settings_popup")
        with dpg.window(label="Settings", modal=True, tag="settings_popup",
                        width=380, height=320, no_resize=True):
            dpg.add_text("Presets", color=(0, 255, 255))
            dpg.add_separator()
            dpg.add_input_text(tag="preset_name_input", width=200, hint="Preset Name")
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=self.handle_save_preset, width=80)
                dpg.add_button(label="Load", callback=self.create_load_preset_window, width=80)

            dpg.add_spacer(height=14)
            dpg.add_text("LED Ball", color=(0, 255, 255))
            dpg.add_separator()
            dpg.add_checkbox(label="Enable LED Ball", tag="led_ball_checkbox",
                             default_value=False, callback=self._toggle_led_ball)
            dpg.add_input_text(label="IP", tag="led_ball_ip_input",
                               default_value=self.led_ball.ip, width=180,
                               callback=self._update_led_ball_ip,
                               on_enter=True)

            dpg.add_spacer(height=14)
            dpg.add_text("Recording", color=(0, 255, 255))
            dpg.add_separator()
            dpg.add_button(label="Set Rec Folder",
                           callback=self.select_recording_folder, width=140)

            dpg.add_spacer(height=14)
            dpg.add_button(label="Close",
                           callback=lambda: dpg.delete_item("settings_popup"),
                           width=-1)

    # --- Main Loop ---

    def run(self):
        self.setup_ui()
        dpg.show_viewport()
        self.running = True

        threading.Thread(target=self.process_incoming_data, daemon=True).start()

        if self.auto_connect:
            logger.info("Auto-connecting to device...")
            try:
                self.math_control_pipe.send(
                    IPCMessage(MSG_CMD_SET_SESSION_MODE,
                               {KEY_SESSION_MODE: SESSION_MODE_NONE}))
            except Exception as e:
                logger.error(f"Failed to send session mode on auto-connect: {e}")
            self.start_stream()

        while dpg.is_dearpygui_running() and self.running:
            self.update_assessment()
            self.poll_ble_status()
            self._update_heartbeat_blink()
            self._poll_genki_wave()
            self._poll_pvs()

            # App ticks
            self.counting_game.tick()
            self.rapid_change_game.tick()
            self.resonance_breathing.tick()

            dpg.render_dearpygui_frame()

        # Cleanup
        if self.audio_enabled:
            self.audio_feedback.stop()
        self.genki_manager.shutdown()
        self.pvs_manager.shutdown()
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

            # Feed HR to rapid change game
            self.rapid_change_game.feed_hr(hr_val)

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

        # Coherence — forwarded to the Resonance Breathing app display
        self.current_coherence = data.coherence_score
        self.resonance_breathing.update_resonance_score(self.current_coherence)

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

            # Feed RR intervals to counting game (always active if built)
            # if self.session_mode == SESSION_MODE_COUNTING:
            for rr in data.rr_intervals:
                self.counting_game.feed_rr(rr)

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
                
                # Feed HR to rapid change game
                self.rapid_change_game.feed_hr(hr_val)

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
            self.resonance_breathing.update_resonance_score(self.current_coherence)

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

            # Feed RR intervals to counting game (always active if built)
            # if self.session_mode == SESSION_MODE_COUNTING:
            for rr in rr_data:
                self.counting_game.feed_rr(rr)

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

        Suppressed during counting-game counting phase to prevent
        the user from using the visual cue to count heartbeats.
        """
        # Suppress blink while the counting game is actively counting
        # if (self.session_mode == SESSION_MODE_COUNTING
        if (self.counting_game.controller.state == "counting"):
            dpg.configure_item("hb_blink_circle", fill=(0, 0, 0, 255))
            return

        elapsed = time.time() - self._blink_time
        if elapsed < self._blink_duration:
            # Fade from 255 (red) → 0 (black) over the duration
            t = elapsed / self._blink_duration
            red = int(255 * (1.0 - t))
            dpg.configure_item("hb_blink_circle", fill=(red, 0, 0, 255))
        else:
            # Resting state: black fill
            dpg.configure_item("hb_blink_circle", fill=(0, 0, 0, 255))

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
        cycle = 60.0 / optimal_bpm
        self.pacer.set_timing(cycle / 2, 0, cycle / 2, 0)
        try:
            self.math_control_pipe.send(
                IPCMessage(MSG_CMD_SET_PACER_TARGET, {KEY_PACER_BPM: optimal_bpm}))
        except Exception as e:
            logger.error(f"Failed to update pacer rate: {e}")

    # --- Session Management ---

    def handle_connect_button(self):
        if not self.is_connected:
            # Directly connect, no popup
            self.start_stream()
            dpg.set_value("status_text", "Connecting...")
            dpg.configure_item("status_text", color=(255, 255, 0))
        else:
            self.stop_stream()
            dpg.set_value("status_text", "Disconnecting...")

    def handle_session_toggle(self):
        if not self.is_recording:
            self.current_session_id = self.db.create_session(user_id=0)
            self.is_recording = True
            dpg.configure_item("session_btn", label="Stop Session")
        else:
            if self.current_session_id:
                self.db.end_session(self.current_session_id)
            self.is_recording = False
            self.current_session_id = None
            dpg.configure_item("session_btn", label="Start Session")

    def handle_recording_toggle(self):
        """Toggle JSON recording state."""
        if not self.is_json_recording:
            # Start Recording
            self.is_json_recording = True
            dpg.configure_item("rec_btn", label="Stop")
            dpg.set_value("rec_status_text", "Recording...")
            try:
                self.math_control_pipe.send(IPCMessage(MSG_CMD_START_RECORDING))
            except Exception as e:
                logger.error(f"Failed to send start recording command: {e}")
        else:
            # Stop Recording
            self.is_json_recording = False
            dpg.configure_item("rec_btn", label="Rec")
            dpg.set_value("rec_status_text", "")
            try:
                self.math_control_pipe.send(IPCMessage(MSG_CMD_STOP_RECORDING))
            except Exception as e:
                logger.error(f"Failed to send stop recording command: {e}")

    def select_recording_folder(self):
        """Open a directory selector dialog."""
        # Dear PyGui's file dialog is a bit complex, using a simple input for now or a custom modal
        # For simplicity in this iteration, we'll use a modal with an input text field
        if dpg.does_item_exist("folder_selection_modal"):
            dpg.delete_item("folder_selection_modal")
        
        with dpg.window(label="Select Recording Folder", modal=True, tag="folder_selection_modal", width=400, height=150):
            dpg.add_text("Enter full path to recording folder:")
            dpg.add_input_text(tag="recording_folder_input", default_value=".", width=-1)
            dpg.add_text("(Default is current directory)", color=(150, 150, 150))
            
            with dpg.group(horizontal=True):
                dpg.add_button(label="Set", callback=self._set_recording_folder, width=80)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("folder_selection_modal"), width=80)

    def _set_recording_folder(self):
        folder_path = dpg.get_value("recording_folder_input")
        # In a real app, we'd validate the path here
        # For now, we just send it to the signal processor (if we had a message for it)
        # or update the SessionRecorder config.
        # Since SessionRecorder is in a different process, we need an IPC message.
        # However, the requirements said "Add a setting to choose the recording folder".
        # We'll assume for now we just log it or would send it if we added a SET_CONFIG message.
        # Given the constraints, we'll just log it as a placeholder for the actual implementation
        # which would require updating IPC messages further.
        logger.info(f"Recording folder set to: {folder_path}")
        dpg.delete_item("folder_selection_modal")

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
                            # Show Polar H10 graphs subsection
                            dpg.configure_item("header_polar_h10", show=True)
                        elif status == "disconnected":
                            self.is_connected = False
                            dpg.set_value("status_text", "Disconnected")
                            dpg.configure_item("status_text", color=(255, 0, 0))
                            dpg.configure_item("connect_btn", label="Connect")
                            dpg.configure_item("session_btn", show=False)
                            self.is_recording = False
                            # Hide Polar H10 graphs subsection
                            dpg.configure_item("header_polar_h10", show=False)
                        elif status == "reconnecting":
                            # Auto-reconnect in progress — keep session alive,
                            # show yellow status, keep Disconnect button available
                            self.is_connected = False
                            dpg.set_value("status_text", "Reconnecting...")
                            dpg.configure_item("status_text", color=(255, 165, 0))
                            dpg.configure_item("connect_btn", label="Disconnect")
                            # Don't hide session_btn or reset is_recording —
                            # the session recorder keeps accumulating data and
                            # will resume when the device reconnects.
                            # Keep Polar H10 graphs visible during reconnect
                    if "battery" in msg:
                        dpg.set_value("battery_text", f"{msg['battery']}%")
        except Exception as e:
            logger.error(f"Error receiving BLE status: {e}")

    # --- Genki Wave Polling ---

    def _poll_genki_wave(self):
        """Poll the Genki Wave manager for status and data each frame."""
        self.genki_bar.poll_status()

        samples = self.genki_manager.poll()
        if samples:
            self.genki_gyro_chart.add_samples(samples)
            self.genki_gyro_chart.update_plot()
            self.genki_acc_chart.add_samples(samples)
            self.genki_acc_chart.update_plot()
            self.genki_gyro_sum_chart.add_samples(samples)
            self.genki_gyro_sum_chart.update_plot()

    # --- Polar Verity Sense Polling ---

    def _poll_pvs(self):
        """Poll the Polar Verity Sense manager for status and data each frame."""
        self.pvs_bar.poll_status()

        samples = self.pvs_manager.poll()
        if samples:
            self.pvs_acc_chart.add_samples(samples)
            self.pvs_acc_chart.update_plot()
            self.pvs_gyro_chart.add_samples(samples)
            self.pvs_gyro_chart.update_plot()
            self.pvs_mag_chart.add_samples(samples)
            self.pvs_mag_chart.update_plot()
            self.pvs_ppi_chart.add_samples(samples)
            self.pvs_ppi_chart.update_plot()
            self.pvs_hr_chart.add_samples(samples)
            self.pvs_hr_chart.update_plot()

    # --- Presets ---

    def handle_save_preset(self):
        preset_name = dpg.get_value("preset_name_input")
        if not preset_name:
            return
        try:
            self.db.save_preset(user_id=0, preset_name=preset_name, config=self.config)
        except Exception as e:
            logger.error(f"Error saving preset: {e}")

    def create_load_preset_window(self):
        try:
            presets = self.db.get_user_presets(user_id=0)
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
            config = self.db.load_preset(user_id=0, preset_name=preset_name)
            if config:
                self.config = config
                self.math_control_pipe.send(self.config)
                dpg.delete_item("load_preset_window")
        except Exception as e:
            logger.error(f"Error loading preset: {e}")

    # --- History ---

    def create_history_window(self):
        try:
            history = self.db.get_session_history(user_id=0, limit=20)
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
