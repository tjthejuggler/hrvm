import os
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
    ACCChart, ECGChart,
    HRVTachogramChart, HRVPoincareChart, HRVRMSSDChart, HRVSDNNChart, HRVCoherenceChart,
)
from src.gui.counting_game import CountingGameWidget
from src.gui.rapid_change_game import RapidChangeWidget
from src.gui.resonance_breathing import ResonanceBreathingWidget
from src.gui.led_ball import LEDBallController
from src.database.db_manager import DatabaseManager
from src.gui.pacer import PacerEngine
from src.recording.session_recorder import RRRecorder
from src.ble.genki_manager import GenkiWaveManager
from src.gui.genki_bar import GenkiWaveBar
from src.gui.genki_charts import GenkiGyroChart, GenkiAccChart, GenkiGyroSumChart
from src.ble.pvs_manager import PolarVeritySenseManager
from src.gui.pvs_bar import PolarVeritySenseBar
from src.gui.pvs_charts import (
    PVSAccChart, PVSGyroChart, PVSMagChart, PVSPPIChart,
    PVSHeartRateChart,
)
from src.ble.ticwatch_manager import SingleTicWatchManager, PORT_LEFT, PORT_RIGHT
from src.gui.ticwatch_bar import TicWatchLeftBar, TicWatchRightBar
from src.gui.ticwatch_charts import (
    TicWatchLeftAccChart, TicWatchLeftGyroChart, TicWatchLeftMagChart,
    TicWatchRightAccChart, TicWatchRightGyroChart, TicWatchRightMagChart,
)
from src.gui.ltx_controller import LTXApp

logger = logging.getLogger(__name__)

_RECORDING_TYPES_FILE = "recording_types.json"
_DEFAULT_RECORDING_TYPES = ["chess", "meditation", "movie"]


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
        self.is_json_recording = False  # Flag for JSON recording

        # Recording type selection
        self._recording_types: List[str] = self._load_recording_types()
        self._recording_type: str = self._recording_types[0]
        self._rr_recorder: Optional[RRRecorder] = None  # Always-on RR recorder

        # HRV source tracking: "h10" | "pvs" | None
        # H10 is preferred; PVS is used as fallback when H10 is not connected
        # and PVS is streaming HR.
        self._hrv_source: Optional[str] = None
        self._pvs_hr_streaming = False  # True when PVS is connected AND streaming HR

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

        # Chart widgets — Polar H10 section
        self.biofeedback_chart = BiofeedbackChart()
        self.heartbeat_chart = HeartbeatChart()
        self.tachogram_chart = TachogramChart()
        self.poincare_chart = PoincareChart()
        self.rmssd_chart = RMSSDHistoryChart()
        self.sdnn_chart = SDNNHistoryChart()
        self.coherence_chart = CoherenceHistoryChart()
        self.acc_chart = ACCChart()
        self.ecg_chart = ECGChart()

        # HRV section charts (device-agnostic, H10 preferred / PVS fallback)
        self.hrv_tachogram_chart = HRVTachogramChart()
        self.hrv_poincare_chart = HRVPoincareChart()
        self.hrv_rmssd_chart = HRVRMSSDChart()
        self.hrv_sdnn_chart = HRVSDNNChart()
        self.hrv_coherence_chart = HRVCoherenceChart()

        # Counting game widget (shown only in counting mode)
        self.counting_game = CountingGameWidget()

        # Rapid Change game widget
        self.rapid_change_game = RapidChangeWidget()

        # Resonance Breathing app
        self.resonance_breathing = ResonanceBreathingWidget(db=self.db)

        # LTX controller app
        self.ltx_app = LTXApp()

        # Genki Wave manager + bar + charts
        self.genki_manager = GenkiWaveManager()
        self.genki_bar = GenkiWaveBar(self.genki_manager)
        self.genki_gyro_chart = GenkiGyroChart()
        self.genki_acc_chart = GenkiAccChart()
        self.genki_gyro_sum_chart = GenkiGyroSumChart()

        # Polar Verity Sense manager + bar + charts
        self.pvs_manager = PolarVeritySenseManager()
        self.pvs_bar = PolarVeritySenseBar(self.pvs_manager)
        self.pvs_bar.on_hr_streaming_changed = self._on_pvs_hr_streaming_changed
        self.pvs_acc_chart = PVSAccChart()
        self.pvs_gyro_chart = PVSGyroChart()
        self.pvs_mag_chart = PVSMagChart()
        self.pvs_ppi_chart = PVSPPIChart()
        self.pvs_hr_chart = PVSHeartRateChart()

        # TicWatch Left — independent device (port 5555)
        self.tw_left_manager  = SingleTicWatchManager("left", PORT_LEFT)
        self.tw_left_bar      = TicWatchLeftBar(self.tw_left_manager)
        self.tw_left_acc_chart   = TicWatchLeftAccChart()
        self.tw_left_gyro_chart  = TicWatchLeftGyroChart()
        self.tw_left_mag_chart   = TicWatchLeftMagChart()

        # TicWatch Right — independent device (port 5556)
        self.tw_right_manager = SingleTicWatchManager("right", PORT_RIGHT)
        self.tw_right_bar     = TicWatchRightBar(self.tw_right_manager)
        self.tw_right_acc_chart  = TicWatchRightAccChart()
        self.tw_right_gyro_chart = TicWatchRightGyroChart()
        self.tw_right_mag_chart  = TicWatchRightMagChart()

        # UI Element Tags
        self.window_tag = "Primary Window"
        self.assessment_status_tag = "Assessment Status"

    # ------------------------------------------------------------------
    # HRV source management
    # ------------------------------------------------------------------

    def _update_hrv_source(self):
        """Determine which device is the active HRV source and update the
        HRV section visibility accordingly.

        Priority: H10 > PVS.
        PVS is used as fallback only when H10 is not connected AND PVS is
        connected AND streaming HR (status message contains "HR").
        This correctly excludes SDK mode (ACC/GYR/MAG only) where PVS is
        connected but not streaming HR.
        """
        if self.is_connected:
            new_source = "h10"
        elif self._pvs_is_streaming_hr():
            new_source = "pvs"
        else:
            new_source = None

        if new_source != self._hrv_source:
            self._hrv_source = new_source
            self._refresh_hrv_section_visibility()

            self.resonance_breathing.set_hr_status(self._hrv_source is not None)

    def _pvs_is_streaming_hr(self) -> bool:
        """Return True if PVS is connected and actively streaming HR data.

        HR is available when the status message contains 'HR' — this is set
        by the manager when the BLE HR service subscription succeeds.
        PPI mode also provides HR via ppi_hr field.
        """
        if not self.pvs_manager.connected:
            return False
        status = self.pvs_manager.status_message
        return "HR" in status or "PPI" in status

    def _refresh_hrv_section_visibility(self):
        """Show or hide the HRV graphs section based on current HRV source."""
        if not dpg.does_item_exist("header_hrv"):
            return
        show = self._hrv_source is not None
        dpg.configure_item("header_hrv", show=show)

    def _on_pvs_hr_streaming_changed(self, is_streaming: bool):
        """Called by PolarVeritySenseBar when PVS HR streaming state changes."""
        self._pvs_hr_streaming = is_streaming
        self._update_hrv_source()

    # ------------------------------------------------------------------
    # UI Setup
    # ------------------------------------------------------------------

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

            # --- Recording Control Row (above all device rows) ---
            self._build_recording_row()
            dpg.add_separator()

            # --- Polar H10 Top Bar ---
            self._build_top_bar()
            dpg.add_separator()

            # --- Genki Wave Top Bar ---
            self.genki_bar.build()
            dpg.add_separator()

            # --- Polar Verity Sense Top Bar ---
            self.pvs_bar.build()
            dpg.add_separator()

            # --- TicWatch Left Top Bar ---
            self.tw_left_bar.build()
            dpg.add_separator()

            # --- TicWatch Right Top Bar ---
            self.tw_right_bar.build()
            dpg.add_separator()

            # --- Session Metrics Bar (large numbers, horizontal) ---
            self._build_metrics_bar()
            dpg.add_separator()

            # --- Charts Section (collapsible, no tabs) ---
            with dpg.child_window(width=-1, height=-1, border=True, tag="charts_area"):

                # --- Apps Section ---
                with dpg.theme(tag="apps_header_theme"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (100, 50, 200, 100))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (120, 70, 220, 150))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (140, 90, 240, 200))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))
                        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 10)

                with dpg.collapsing_header(label="APPS", tag="header_apps", default_open=True):
                    dpg.bind_item_theme(dpg.last_item(), "apps_header_theme")

                    with dpg.group(tag="apps_container"):
                        self.counting_game.build("apps_container")
                        self.rapid_change_game.build("apps_container")
                        self.resonance_breathing.build("apps_container")
                        dpg.add_spacer(height=10, parent="apps_container")
                        self.ltx_app.build("apps_container")

                # --- Graphs Section ---
                with dpg.theme(tag="graphs_header_theme"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (0, 100, 200, 100))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (20, 120, 220, 150))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (40, 140, 240, 200))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))
                        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 10)

                # Device subsection theme
                with dpg.theme(tag="device_subsection_theme"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (0, 80, 160, 80))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (10, 100, 180, 120))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (20, 120, 200, 160))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (200, 230, 255, 255))
                        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 8)

                # HRV subsection theme (green tint to distinguish from device sections)
                with dpg.theme(tag="hrv_subsection_theme"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (0, 120, 60, 100))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (10, 150, 80, 140))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (20, 180, 100, 180))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (180, 255, 200, 255))
                        dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 8)

                with dpg.collapsing_header(label="GRAPHS", tag="header_graphs",
                                           default_open=True):
                    dpg.bind_item_theme(dpg.last_item(), "graphs_header_theme")

                    # --- HRV Section (device-agnostic, H10 preferred / PVS fallback) ---
                    with dpg.collapsing_header(label="HRV", tag="header_hrv",
                                               default_open=True, show=False):
                        dpg.bind_item_theme(dpg.last_item(), "hrv_subsection_theme")

                        with dpg.group(tag="hrv_graphs_container"):
                            self.hrv_tachogram_chart.build("hrv_graphs_container")
                            self.hrv_poincare_chart.build("hrv_graphs_container")
                            self.hrv_rmssd_chart.build("hrv_graphs_container")
                            self.hrv_sdnn_chart.build("hrv_graphs_container")
                            self.hrv_coherence_chart.build("hrv_graphs_container")

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

                    # --- TicWatch Left Device Subsection ---
                    with dpg.collapsing_header(label="TicWatch Left",
                                               tag="header_ticwatch_left",
                                               default_open=True, show=False):
                        dpg.bind_item_theme(dpg.last_item(), "device_subsection_theme")

                        with dpg.group(tag="ticwatch_left_graphs_container"):
                            self.tw_left_acc_chart.build("ticwatch_left_graphs_container")
                            self.tw_left_gyro_chart.build("ticwatch_left_graphs_container")
                            self.tw_left_mag_chart.build("ticwatch_left_graphs_container")

                    # --- TicWatch Right Device Subsection ---
                    with dpg.collapsing_header(label="TicWatch Right",
                                               tag="header_ticwatch_right",
                                               default_open=True, show=False):
                        dpg.bind_item_theme(dpg.last_item(), "device_subsection_theme")

                        with dpg.group(tag="ticwatch_right_graphs_container"):
                            self.tw_right_acc_chart.build("ticwatch_right_graphs_container")
                            self.tw_right_gyro_chart.build("ticwatch_right_graphs_container")
                            self.tw_right_mag_chart.build("ticwatch_right_graphs_container")

                with dpg.theme(tag="theme_graphs_header"):
                    with dpg.theme_component(dpg.mvCollapsingHeader):
                        dpg.add_theme_color(dpg.mvThemeCol_Header, (0, 100, 150, 150))
                        dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (20, 120, 170, 170))
                        dpg.add_theme_color(dpg.mvThemeCol_Text, (220, 255, 255, 255))
                dpg.bind_item_theme("header_graphs", "theme_graphs_header")

        dpg.set_primary_window(self.window_tag, True)

    def _build_recording_row(self):
        """Top recording control row — sits above all device rows."""
        with dpg.group(horizontal=True):
            dpg.add_button(label="[REC]", tag="rec_btn",
                           callback=self.handle_recording_toggle, width=70)
            dpg.add_text("", tag="rec_status_text", color=(255, 80, 80))
            dpg.add_spacer(width=10)
            dpg.add_combo(
                items=self._recording_types,
                tag="rec_type_combo",
                default_value=self._recording_types[0],
                width=130,
                callback=self._on_recording_type_changed,
            )
            dpg.add_button(label="+ Add", callback=self.open_add_recording_type_popup,
                           width=60)
            dpg.add_spacer(width=30)
            dpg.add_button(label="⚙ Settings", callback=self.open_settings_popup, width=100)

    def _on_recording_type_changed(self, sender, app_data):
        self._recording_type = app_data

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
            dpg.add_button(label="History", callback=self.create_history_window, width=80)
            dpg.add_checkbox(label="Audio Feedback", callback=self.toggle_audio,
                             default_value=False)

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
            self._poll_ticwatch()

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
        self.tw_left_manager.stop()
        self.tw_right_manager.stop()
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
        """Handle ProcessedData from signal processor (Polar H10 source)."""
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

            if hr_val > 0:
                self.ltx_app.feed_h10_metrics(hr=hr_val)
            if data.rr_intervals:
             # Just pass the last one for real-time trigger check
                self.ltx_app.feed_h10_metrics(rr=data.rr_intervals[-1])

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
                self._update_hr_y_axis()

        # RMSSD / SDNN — update top-bar metrics (H10 is the primary source)
        self.current_rmssd = data.hrv_rmssd
        dpg.set_value("rmssd_display", f"{self.current_rmssd:.1f} ms")
        dpg.set_value("sdnn_display", f"{data.hrv_sdnn:.1f} ms")
        dpg.set_value("quality_bar", data.quality_score)

        # Coherence — forwarded to the Resonance Breathing app display
        self.current_coherence = data.coherence_score
        self.resonance_breathing.update_resonance_score(self.current_coherence)

        # H10-specific history charts (in the Polar H10 section)
        self.rmssd_chart.add_data(current_time, self.current_rmssd)
        self.rmssd_chart.update_plot()
        self.sdnn_chart.add_data(current_time, data.hrv_sdnn)
        self.sdnn_chart.update_plot()
        self.coherence_chart.add_data(current_time, self.current_coherence)
        self.coherence_chart.update_plot()

        # HRV section charts (H10 is the active source)
        self.hrv_rmssd_chart.add_data(current_time, self.current_rmssd)
        self.hrv_rmssd_chart.update_plot()
        self.hrv_sdnn_chart.add_data(current_time, data.hrv_sdnn)
        self.hrv_sdnn_chart.update_plot()
        self.hrv_coherence_chart.add_data(current_time, self.current_coherence)
        self.hrv_coherence_chart.update_plot()

        # RR intervals -> heartbeat chart, tachogram, poincaré, HRV section
        if data.rr_intervals:
            self.heartbeat_chart.add_beats(data.timestamp, data.rr_intervals,
                                           self.start_time)
            self.heartbeat_chart.update_plot(current_time)

            self.tachogram_chart.add_rr(data.rr_intervals, current_time)
            self.tachogram_chart.update_plot(current_time)

            self.poincare_chart.add_rr(data.rr_intervals)
            self.poincare_chart.update_plot()

            # HRV section charts
            self.hrv_tachogram_chart.add_rr(data.rr_intervals, current_time)
            self.hrv_tachogram_chart.update_plot(current_time)
            self.hrv_poincare_chart.add_rr(data.rr_intervals)
            self.hrv_poincare_chart.update_plot()

            # Feed RR intervals to counting game
            for rr in data.rr_intervals:
                self.counting_game.feed_rr(rr)
                self.resonance_breathing.feed_rr(rr)

            # Chess SessionRecorder — H10 source
            if self.is_json_recording and hasattr(self, '_session_recorder'):
                for rr in data.rr_intervals:
                    self._session_recorder.add_rr_interval(rr)

            # RR recorder — all recording types
            if self.is_json_recording and self._rr_recorder is not None:
                for rr in data.rr_intervals:
                    self._rr_recorder.add_rr(rr)

        if self.is_json_recording and hasattr(self, '_session_recorder') and hr_val > 0:
            self._session_recorder.add_hr_sample(int(hr_val))

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
                    self._update_hr_y_axis()

                if self.is_json_recording and hasattr(self, '_session_recorder'):
                    self._session_recorder.add_hr_sample(int(hr_val))

        if KEY_RMSSD in payload:
            self.current_rmssd = payload[KEY_RMSSD]
            dpg.set_value("rmssd_display", f"{self.current_rmssd:.1f} ms")

        if KEY_COHERENCE in payload:
            self.current_coherence = payload[KEY_COHERENCE]
            self.resonance_breathing.update_resonance_score(self.current_coherence)

        # H10-specific history charts
        self.rmssd_chart.add_data(current_time, self.current_rmssd)
        self.rmssd_chart.update_plot()
        self.sdnn_chart.add_data(current_time, 0.0)  # SDNN not in legacy payload
        self.sdnn_chart.update_plot()
        self.coherence_chart.add_data(current_time, self.current_coherence)
        self.coherence_chart.update_plot()

        # HRV section charts (H10 source)
        self.hrv_rmssd_chart.add_data(current_time, self.current_rmssd)
        self.hrv_rmssd_chart.update_plot()
        self.hrv_coherence_chart.add_data(current_time, self.current_coherence)
        self.hrv_coherence_chart.update_plot()

        if 'rr_intervals' in payload and payload['rr_intervals']:
            rr_data = payload['rr_intervals']
            if not isinstance(rr_data, list):
                rr_data = [rr_data]
            self.heartbeat_chart.add_beats(time.time(), rr_data, self.start_time)
            self.heartbeat_chart.update_plot(current_time)
            self.tachogram_chart.add_rr(rr_data, current_time)
            self.tachogram_chart.update_plot(current_time)
            self.poincare_chart.add_rr(rr_data)
            self.poincare_chart.update_plot()

            # HRV section charts
            self.hrv_tachogram_chart.add_rr(rr_data, current_time)
            self.hrv_tachogram_chart.update_plot(current_time)
            self.hrv_poincare_chart.add_rr(rr_data)
            self.hrv_poincare_chart.update_plot()

            # Feed RR intervals to counting game
            for rr in rr_data:
                self.counting_game.feed_rr(rr)
                self.resonance_breathing.feed_rr(rr) 

            if self.is_json_recording and hasattr(self, '_session_recorder'):
                for rr in rr_data:
                    self._session_recorder.add_rr_interval(rr)

            # RR recorder — all recording types
            if self.is_json_recording and self._rr_recorder is not None:
                for rr in rr_data:
                    self._rr_recorder.add_rr(rr)

    def handle_acc_data(self, batch: ACCBatch):
        """Handle accelerometer data from BLE."""
        current_time = time.time() - self.start_time
        self.acc_chart.add_samples(batch.timestamp_unix, batch.samples,
                                   batch.sample_rate, self.start_time)
        self.acc_chart.update_plot(current_time)

        # Batch samples are typically (N, 3). Take the last one.
        if len(batch.samples) > 0:
            last_sample = batch.samples[-1]
            self.ltx_app.feed_h10_acc(last_sample[0], last_sample[1], last_sample[2])

    def handle_ecg_data(self, batch: ECGBatch):
        """Handle ECG data from BLE PMD service."""
        current_time = time.time() - self.start_time
        samples = batch.samples.tolist() if hasattr(batch.samples, 'tolist') else list(batch.samples)
        self.ecg_chart.add_samples(batch.timestamp_unix, samples,
                                   batch.sample_rate, self.start_time)
        self.ecg_chart.update_plot(current_time)

    # --- Heartbeat Blink ---

    def _update_heartbeat_blink(self):
        """Update the heartbeat indicator circle color each frame."""
        if (self.counting_game.controller.state == "counting"):
            dpg.configure_item("hb_blink_circle", fill=(0, 0, 0, 255))
            return

        elapsed = time.time() - self._blink_time
        if elapsed < self._blink_duration:
            t = elapsed / self._blink_duration
            red = int(255 * (1.0 - t))
            dpg.configure_item("hb_blink_circle", fill=(red, 0, 0, 255))
        else:
            dpg.configure_item("hb_blink_circle", fill=(0, 0, 0, 255))

    def _update_hr_y_axis(self):
        """Set the HR chart Y-axis to ±10% of the visible data range."""
        hr_y = list(self.biofeedback_chart.hr_y)
        if len(hr_y) < 2:
            return
        data_min = min(hr_y)
        data_max = max(hr_y)
        margin = max((data_max - data_min) * 0.1, 2.0)  # at least 2 BPM margin
        dpg.set_axis_limits("bf_y_axis", data_min - margin, data_max + margin)

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
        """Toggle recording state.

        For all recording types: starts an RRRecorder that captures a flat list
        of RR values and saves them as {epoch_seconds}.json.

        For 'chess' type only: also runs the full chess-coach SessionRecorder
        (H10 path via signal processor, or PVS fallback).

        Uses the Polar H10 as the data source if connected; falls back to the
        Polar Verity Sense if it is streaming HR data.
        """
        rec_type = self._recording_type

        if not self.is_json_recording:
            has_source = self.is_connected or self._pvs_hr_streaming
            if not has_source:
                logger.warning("Recording requested but no HR source is active")
                return

            # --- Always start the RR recorder ---
            self._rr_recorder = RRRecorder(recording_type=rec_type)
            self._rr_recorder.start()

            # --- Chess-specific: full SessionRecorder ---
            if rec_type == "chess":
                if self.is_connected:
                    try:
                        self.math_control_pipe.send(IPCMessage(MSG_CMD_START_RECORDING))
                    except Exception as e:
                        logger.error(f"Failed to send start recording command: {e}")
                elif self._pvs_hr_streaming:
                    from src.recording.session_recorder import SessionRecorder
                    self._session_recorder = SessionRecorder(
                        device_name="Polar Verity Sense",
                        device_id="",
                    )
                    self._session_recorder.start()
                    logger.info("Chess SessionRecorder started via Polar Verity Sense")

            self.is_json_recording = True
            source_label = "H10" if self.is_connected else "PVS"
            dpg.configure_item("rec_btn", label="[STOP]")
            dpg.set_value("rec_status_text",
                          f"● {rec_type.capitalize()} ({source_label})")
            logger.info(f"Recording started: type={rec_type}, source={source_label}")

        else:
            # --- Stop recording ---
            self.is_json_recording = False
            dpg.configure_item("rec_btn", label="[REC]")
            dpg.set_value("rec_status_text", "")

            # Stop RR recorder (all types)
            if self._rr_recorder is not None:
                rr_path = self._rr_recorder.stop()
                if rr_path:
                    logger.info(f"RR recording saved to: {rr_path}")
                self._rr_recorder = None

            # Stop chess SessionRecorder if it was running
            if self.is_connected and rec_type == "chess":
                try:
                    self.math_control_pipe.send(IPCMessage(MSG_CMD_STOP_RECORDING))
                except Exception as e:
                    logger.error(f"Failed to send stop recording command: {e}")
            elif hasattr(self, '_session_recorder'):
                filepath = self._session_recorder.stop()
                if filepath:
                    logger.info(f"PVS chess recording saved to: {filepath}")
                del self._session_recorder

    def select_recording_folder(self):
        """Open a directory selector dialog."""
        if dpg.does_item_exist("folder_selection_modal"):
            dpg.delete_item("folder_selection_modal")

        with dpg.window(label="Select Recording Folder", modal=True,
                        tag="folder_selection_modal", width=400, height=150):
            dpg.add_text("Enter full path to recording folder:")
            dpg.add_input_text(tag="recording_folder_input", default_value=".", width=-1)
            dpg.add_text("(Default is current directory)", color=(150, 150, 150))

            with dpg.group(horizontal=True):
                dpg.add_button(label="Set", callback=self._set_recording_folder, width=80)
                dpg.add_button(label="Cancel",
                               callback=lambda: dpg.delete_item("folder_selection_modal"),
                               width=80)

    def _set_recording_folder(self):
        folder_path = dpg.get_value("recording_folder_input")
        logger.info(f"Recording folder set to: {folder_path}")
        dpg.delete_item("folder_selection_modal")

    def open_add_recording_type_popup(self):
        """Open a small popup to add a custom recording type label."""
        if dpg.does_item_exist("add_rec_type_popup"):
            dpg.delete_item("add_rec_type_popup")
        with dpg.window(label="Add Recording Type", modal=True,
                        tag="add_rec_type_popup", width=300, height=110,
                        no_resize=True):
            dpg.add_input_text(tag="new_rec_type_input", hint="e.g. yoga",
                               width=-1)
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Add", callback=self._confirm_add_recording_type,
                               width=80)
                dpg.add_button(label="Cancel",
                               callback=lambda: dpg.delete_item("add_rec_type_popup"),
                               width=80)

    def _load_recording_types(self) -> list:
        """Load recording types from file, falling back to defaults."""
        import json as _json
        try:
            if os.path.exists(_RECORDING_TYPES_FILE):
                with open(_RECORDING_TYPES_FILE, "r") as f:
                    data = _json.load(f)
                    if isinstance(data, list) and data:
                        return data
        except Exception as e:
            logger.error(f"Failed to load recording types: {e}")
        return list(_DEFAULT_RECORDING_TYPES)

    def _save_recording_types(self):
        """Persist the current recording types list to file."""
        import json as _json
        try:
            with open(_RECORDING_TYPES_FILE, "w") as f:
                _json.dump(self._recording_types, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save recording types: {e}")

    def _confirm_add_recording_type(self):
        label = dpg.get_value("new_rec_type_input").strip()
        if label and label not in self._recording_types:
            self._recording_types.append(label)
            dpg.configure_item("rec_type_combo", items=self._recording_types)
            self._save_recording_types()
            logger.info(f"Added recording type: {label}")
        dpg.delete_item("add_rec_type_popup")

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
                            dpg.configure_item("header_polar_h10", show=True)
                            self._update_hrv_source()
                        elif status == "disconnected":
                            self.is_connected = False
                            dpg.set_value("status_text", "Disconnected")
                            dpg.configure_item("status_text", color=(255, 0, 0))
                            dpg.configure_item("connect_btn", label="Connect")
                            dpg.configure_item("session_btn", show=False)
                            self.is_recording = False
                            dpg.configure_item("header_polar_h10", show=False)
                            self._update_hrv_source()
                        elif status == "reconnecting":
                            self.is_connected = False
                            dpg.set_value("status_text", "Reconnecting...")
                            dpg.configure_item("status_text", color=(255, 165, 0))
                            dpg.configure_item("connect_btn", label="Disconnect")
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

            self.ltx_app.feed_genki_data(samples)

    # --- TicWatch Polling ---

    def _poll_ticwatch(self):
        """Poll each TicWatch independently each frame."""
        self.tw_left_bar.poll_status()
        left_samples = self.tw_left_manager.poll()
        if left_samples:
            self.tw_left_acc_chart.add_samples(left_samples)
            self.tw_left_acc_chart.update_plot()
            self.tw_left_gyro_chart.add_samples(left_samples)
            self.tw_left_gyro_chart.update_plot()
            self.tw_left_mag_chart.add_samples(left_samples)
            self.tw_left_mag_chart.update_plot()
            self.ltx_app.feed_ticwatch_data("left", left_samples)

        self.tw_right_bar.poll_status()
        right_samples = self.tw_right_manager.poll()
        if right_samples:
            self.tw_right_acc_chart.add_samples(right_samples)
            self.tw_right_acc_chart.update_plot()
            self.tw_right_gyro_chart.add_samples(right_samples)
            self.tw_right_gyro_chart.update_plot()
            self.tw_right_mag_chart.add_samples(right_samples)
            self.tw_right_mag_chart.update_plot()
            self.ltx_app.feed_ticwatch_data("right", right_samples)

    # --- Polar Verity Sense Polling ---

    def _poll_pvs(self):
        """Poll the Polar Verity Sense manager for status and data each frame."""
        self.pvs_bar.poll_status()
        # Keep HRV source in sync with PVS connection state each frame
        self._update_hrv_source()

        samples = self.pvs_manager.poll()
        if not samples:
            return

        # Always update PVS device charts
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

        self.ltx_app.feed_pvs_data(samples)

        # When PVS is the active HRV source (H10 not connected and PVS streaming HR),
        # feed HR/PPI data into the HRV section charts and top-bar metrics.
        pvs_is_active_hrv = not self.is_connected and self._pvs_is_streaming_hr()
        if pvs_is_active_hrv:
            # Ensure _hrv_source is set (may not be set yet on first frame)
            if self._hrv_source != "pvs":
                self._hrv_source = "pvs"
                self._refresh_hrv_section_visibility()

            current_time = time.time() - self.start_time
            for s in samples:
                # Derive HR from PPI or BLE HR service
                hr = s.ppi_hr if (s.ppi_hr is not None and s.ppi_hr != 0) else s.hr_bpm
                if hr is not None and hr > 0:
                    self.current_bpm = hr
                    dpg.set_value("hr_display", f"{hr:.0f} BPM")
                    if self.audio_enabled:
                        self.audio_feedback.update_hr(hr)
                    self.rapid_change_game.feed_hr(hr)

                    # JSON recording via PVS
                    if self.is_json_recording and hasattr(self, '_session_recorder'):
                        self._session_recorder.add_hr_sample(int(hr))

                # Feed PPI as RR intervals into HRV charts
                if s.ppi_ms is not None and 300 < s.ppi_ms < 2000:
                    rr = float(s.ppi_ms)
                    self.hrv_tachogram_chart.add_rr([rr], current_time)
                    self.hrv_poincare_chart.add_rr([rr])
                    self.counting_game.feed_rr(rr)

                    if self.is_json_recording and hasattr(self, '_session_recorder'):
                        self._session_recorder.add_rr_interval(rr)

                    # RR recorder — all recording types
                    if self.is_json_recording and self._rr_recorder is not None:
                        self._rr_recorder.add_rr(rr)

            self.hrv_tachogram_chart.update_plot(current_time)
            self.hrv_poincare_chart.update_plot()

            # Compute simple rolling RMSSD/SDNN from PPI data for top-bar display
            self._update_hrv_metrics_from_pvs_ppi(samples, current_time)

    def _update_hrv_metrics_from_pvs_ppi(self, samples, current_time: float):
        """Compute and display RMSSD/SDNN from recent PPI samples (PVS fallback).

        Uses a simple rolling buffer of the last 60 PPI values.
        """
        if not hasattr(self, '_pvs_ppi_buffer'):
            self._pvs_ppi_buffer: deque = deque(maxlen=60)

        for s in samples:
            if s.ppi_ms is not None and 300 < s.ppi_ms < 2000:
                self._pvs_ppi_buffer.append(float(s.ppi_ms))

        if len(self._pvs_ppi_buffer) < 3:
            return

        arr = np.array(list(self._pvs_ppi_buffer))
        diffs = np.diff(arr)
        rmssd = float(np.sqrt(np.mean(diffs ** 2)))
        sdnn = float(np.std(arr))

        self.current_rmssd = rmssd
        dpg.set_value("rmssd_display", f"{rmssd:.1f} ms")
        dpg.set_value("sdnn_display", f"{sdnn:.1f} ms")

        self.hrv_rmssd_chart.add_data(current_time, rmssd)
        self.hrv_rmssd_chart.update_plot()
        self.hrv_sdnn_chart.add_data(current_time, sdnn)
        self.hrv_sdnn_chart.update_plot()

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
