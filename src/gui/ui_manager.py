import dearpygui.dearpygui as dpg
import numpy as np
from multiprocessing import shared_memory
from multiprocessing.connection import Connection
from typing import Optional, Dict, Any, List
from collections import deque
import time
import threading
from dataclasses import asdict

from src.utils.ipc import ProcessedData, BLECommand, ProcessingConfig, CommandType, SystemCommand
from src.database.db_manager import DatabaseManager
from src.gui.pacer import PacerEngine

class UIManager:
    """
    Manages Dear PyGui interface and coordinates with other processes.
    """
    
    def __init__(self, data_pipe: Connection, ble_control_pipe: Connection,
                 math_control_pipe: Connection, shm_name: str, auto_connect: bool = False):
        self.data_pipe = data_pipe
        self.ble_control_pipe = ble_control_pipe
        self.math_control_pipe = math_control_pipe
        self.shm_name = shm_name
        self.auto_connect = auto_connect
        
        # Shared memory for ECG display
        self.shm: Optional[shared_memory.SharedMemory] = None
        self.ecg_display_buffer: Optional[np.ndarray] = None
        
        # UI state
        self.current_user_id: Optional[int] = None
        self.current_session_id: Optional[int] = None
        self.is_connected = False
        self.is_recording = False
        self.users: List[Dict[str, Any]] = []
        
        # Pacer Engine
        self.pacer = PacerEngine()
        self.pacer_active = True
        
        # Display buffers
        self.ecg_plot_data_x = np.linspace(0, 2, 260) # 2 seconds at 130Hz
        self.ecg_plot_data_y = np.zeros(260)
        
        # Metric History
        self.max_history = 600  # ~10 minutes at 1Hz
        self.time_history = deque(maxlen=self.max_history)
        self.hr_history = deque(maxlen=self.max_history)
        self.rmssd_history = deque(maxlen=self.max_history)
        self.sdnn_history = deque(maxlen=self.max_history)
        self.coherence_history = deque(maxlen=self.max_history)
        self.start_time = None
        
        # Metrics
        self.latest_hr = 0.0
        self.latest_rmssd = 0.0
        self.latest_sdnn = 0.0
        self.latest_coherence = 0.0
        
        # Database
        self.db = DatabaseManager("hrv_data.db")
        
        # Configuration
        self.config = ProcessingConfig(
            window_size_seconds=60,
            artifact_threshold=3.0,
            filter_cutoff_low=5.0,
            filter_cutoff_high=15.0
        )

    def _attach_shared_memory(self):
        """Attempts to attach to shared memory. Retries if not immediately available."""
        try:
            self.shm = shared_memory.SharedMemory(name=self.shm_name)
            self.ecg_display_buffer = np.ndarray((260,), dtype=np.int32, buffer=self.shm.buf)
            print(f"Attached to shared memory: {self.shm_name}")
        except FileNotFoundError:
            # print(f"Shared memory {self.shm_name} not found yet.")
            pass
        except Exception as e:
            print(f"Error attaching to shared memory: {e}")

    def setup_ui(self) -> None:
        """Initialize Dear PyGui windows and widgets."""
        print("[DEBUG] Setting up GUI...")
        try:
            dpg.create_context()
            dpg.create_viewport(title="Polar Flow-Sync HRV Dashboard", width=1280, height=800)
            dpg.setup_dearpygui()
            
            # Load users for dropdown
            self._load_users()
            
            # Theme
            with dpg.theme() as global_theme:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 5)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
                    dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (20, 20, 20, 255))
                    dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (40, 40, 40, 255))
            dpg.bind_theme(global_theme)

        except Exception as e:
            print(f"[ERROR] GUI setup failed: {e}")
            raise

        # --- Main Window (Full Screen) ---
        with dpg.window(tag="Primary Window", no_title_bar=True):
            
            # Top Bar (Menu & Status)
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

            dpg.add_separator()

            # --- Main Layout: Vertical Split (Top: Biofeedback, Bottom: History) ---
            
            # --- Top Section: Biofeedback & Controls (50% Height) ---
            with dpg.group(horizontal=True, height=400):
                
                # --- Left Panel: Controls & Settings (20%) ---
                with dpg.child_window(width=250, height=-1, border=True):
                    dpg.add_text("Controls", color=(0, 255, 255))
                    dpg.add_separator()
                    
                    dpg.add_text("Pacer Settings")
                    # dpg.add_slider_float(label="BPM", default_value=6.0, min_value=1.0, max_value=15.0, callback=lambda s, a: self.pacer.set_bpm(a), width=150)
                    
                    dpg.add_text("Breathing Cycle (s)")
                    dpg.add_input_float(label="Inhale", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_inhale")
                    dpg.add_input_float(label="Hold (Full)", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_hold_full")
                    dpg.add_input_float(label="Exhale", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_exhale")
                    dpg.add_input_float(label="Hold (Empty)", default_value=4.0, step=0.5, width=100, callback=self.update_pacer_settings, tag="pacer_hold_empty")
                    
                    dpg.add_text("Calculated BPM: ", tag="pacer_bpm_display")
                    
                    dpg.add_spacer(height=20)
                    dpg.add_text("Resonance Assessment")
                    dpg.add_button(label="Start Assessment", callback=self.start_resonance_assessment, width=-1)
                    dpg.add_text("Status: Idle", tag="assessment_status", color=(150, 150, 150))
                    dpg.add_progress_bar(tag="assessment_progress", default_value=0.0, width=-1, show=False)

                    dpg.add_spacer(height=20)
                    dpg.add_separator()
                    dpg.add_text("Signal Settings")
                    dpg.add_slider_int(label="Window (s)", default_value=self.config.window_size_seconds, min_value=10, max_value=300, callback=self.handle_settings_change, tag="window_size_slider", width=150)
                    dpg.add_slider_float(label="Artifact Thresh", default_value=self.config.artifact_threshold, min_value=1.0, max_value=10.0, callback=self.handle_settings_change, tag="artifact_threshold_slider", width=150)
                    
                    dpg.add_spacer(height=20)
                    dpg.add_separator()
                    dpg.add_text("Presets")
                    dpg.add_input_text(tag="preset_name_input", width=150, hint="Preset Name")
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Save", callback=self.handle_save_preset, width=70)
                        dpg.add_button(label="Load", callback=self.create_load_preset_window, width=70)

                # --- Center Panel: Pacer & Current Metrics (60%) ---
                with dpg.child_window(width=-300, height=-1, border=False):
                    
                    # Top: Coherence Score & HR
                    with dpg.group(horizontal=True):
                        dpg.add_spacer(width=50)
                        with dpg.group():
                            dpg.add_text("COHERENCE", color=(150, 150, 150))
                            dpg.add_text("0.0", tag="coherence_big_display", color=(0, 255, 0))
                        
                        dpg.add_spacer(width=100)
                        with dpg.group():
                            dpg.add_text("HEART RATE", color=(150, 150, 150))
                            dpg.add_text("0 BPM", tag="hr_big_display", color=(255, 50, 50))

                    dpg.add_spacer(height=10)

                    # Middle: Pacer Visuals
                    # We use a drawlist for custom drawing
                    with dpg.drawlist(width=600, height=300, tag="pacer_drawlist"):
                        self.pacer.setup_draw_layer("pacer_drawlist")

                # --- Right Panel: Session Stats (20%) ---
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

            # --- Bottom Section: Full Width Graphs (50% Height) ---
            # Stacked Collapsing Headers instead of Tabs
            with dpg.child_window(width=-1, height=-1, border=True):
                
                with dpg.collapsing_header(label="Heart Rate & HRV", default_open=True):
                    # HR Plot
                    with dpg.plot(label="Heart Rate (BPM)", height=200, width=-1, no_mouse_pos=True):
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="hr_x_axis")
                        dpg.add_plot_axis(dpg.mvYAxis, label="BPM", tag="hr_y_axis")
                        dpg.add_line_series([], [], label="HR", parent="hr_y_axis", tag="hr_series")

                    dpg.add_spacer(height=5)

                    # HRV Plot
                    with dpg.plot(label="HRV (RMSSD/SDNN)", height=200, width=-1, no_mouse_pos=True):
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="rmssd_x_axis")
                        dpg.add_plot_axis(dpg.mvYAxis, label="ms", tag="rmssd_y_axis")
                        dpg.add_line_series([], [], label="RMSSD", parent="rmssd_y_axis", tag="rmssd_series")
                        dpg.add_line_series([], [], label="SDNN", parent="rmssd_y_axis", tag="sdnn_series")

                with dpg.collapsing_header(label="Coherence", default_open=True):
                    with dpg.plot(label="Coherence Trend", height=200, width=-1):
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="coherence_x_axis")
                        dpg.add_plot_axis(dpg.mvYAxis, label="Score", tag="coherence_y_axis")
                        dpg.add_line_series([], [], parent="coherence_y_axis", tag="coherence_series")

                with dpg.collapsing_header(label="ECG Raw", default_open=True):
                    with dpg.plot(height=200, width=-1, no_mouse_pos=True):
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="ecg_x_axis")
                        dpg.add_plot_axis(dpg.mvYAxis, label="Amplitude", tag="ecg_y_axis")
                        dpg.add_line_series(self.ecg_plot_data_x, self.ecg_plot_data_y, label="ECG", parent="ecg_y_axis", tag="ecg_series")

        dpg.set_primary_window("Primary Window", True)
        # dpg.toggle_viewport_fullscreen() # Optional: Start fullscreen

    def _load_users(self):
        """Load users from DB to populate combo box."""
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT user_id, username FROM users")
            rows = cursor.fetchall()
            self.users = [{'user_id': r[0], 'username': r[1]} for r in rows]
        except Exception as e:
            print(f"Error loading users: {e}")
            self.users = []

    def _on_user_selected(self, sender, app_data):
        username = app_data
        for user in self.users:
            if user['username'] == username:
                self.current_user_id = user['user_id']
                print(f"Selected user: {username} (ID: {self.current_user_id})")
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
                dpg.delete_item(dpg.last_container()) # Close window
                print(f"Created user {username}")
            except Exception as e:
                print(f"Error creating user: {e}")

    def handle_connect_button(self):
        if not self.is_connected:
            # Send connect command
            cmd = BLECommand(command="connect", params={})
            self.ble_control_pipe.send(cmd)
            dpg.set_value("status_text", "Connecting...")
            dpg.configure_item("status_text", color=(255, 255, 0))
        else:
            # Send disconnect command
            cmd = BLECommand(command="disconnect", params={})
            self.ble_control_pipe.send(cmd)
            dpg.set_value("status_text", "Disconnecting...")

    def handle_session_toggle(self):
        if not self.is_recording:
            if self.current_user_id is None:
                print("Please select a user first.")
                return
            
            # Start Session
            self.current_session_id = self.db.create_session(self.current_user_id)
            self.is_recording = True
            dpg.configure_item("session_btn", label="Stop Session")
            print(f"Started session {self.current_session_id}")
        else:
            # Stop Session
            if self.current_session_id:
                self.db.end_session(self.current_session_id)
                print(f"Ended session {self.current_session_id}")
            
            self.is_recording = False
            self.current_session_id = None
            dpg.configure_item("session_btn", label="Start Session")

    def handle_settings_change(self, sender, app_data):
        # Update config object
        if sender == "window_size_slider":
            self.config.window_size_seconds = app_data
        elif sender == "artifact_threshold_slider":
            self.config.artifact_threshold = app_data
            
        # Send update to Process 2
        self.math_control_pipe.send(self.config)
        print(f"Sent config update: {self.config}")

    def update_pacer_settings(self, sender, app_data):
        """Callback for pacer timing inputs."""
        inhale = dpg.get_value("pacer_inhale")
        hold_full = dpg.get_value("pacer_hold_full")
        exhale = dpg.get_value("pacer_exhale")
        hold_empty = dpg.get_value("pacer_hold_empty")
        
        self.pacer.set_timing(inhale, hold_full, exhale, hold_empty)
        
        # Update BPM display
        bpm = self.pacer.get_bpm()
        dpg.set_value("pacer_bpm_display", f"Calculated BPM: {bpm:.1f}")

    def handle_save_preset(self):
        if self.current_user_id is None:
            print("Please select a user first.")
            return
        
        preset_name = dpg.get_value("preset_name_input")
        if not preset_name:
            print("Please enter a preset name.")
            return
            
        try:
            self.db.save_preset(self.current_user_id, preset_name, self.config)
            print(f"Saved preset '{preset_name}' for user {self.current_user_id}")
        except Exception as e:
            print(f"Error saving preset: {e}")

    def create_load_preset_window(self):
        if self.current_user_id is None:
            print("Please select a user first.")
            return
            
        try:
            presets = self.db.get_user_presets(self.current_user_id)
            
            if dpg.does_item_exist("load_preset_window"):
                dpg.delete_item("load_preset_window")
                
            with dpg.window(label="Load Preset", modal=True, width=300, height=200, tag="load_preset_window"):
                if not presets:
                    dpg.add_text("No presets found for this user.")
                else:
                    for p in presets:
                        dpg.add_button(label=p['preset_name'], user_data=p['preset_name'], callback=self._on_preset_selected, width=-1)
                
                dpg.add_spacer(height=10)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("load_preset_window"))
        except Exception as e:
            print(f"Error loading presets: {e}")

    def _on_preset_selected(self, sender, app_data, user_data):
        preset_name = user_data
        try:
            config = self.db.load_preset(self.current_user_id, preset_name)
            if config:
                self.config = config
                # Update UI elements
                dpg.set_value("window_size_slider", self.config.window_size_seconds)
                dpg.set_value("artifact_threshold_slider", self.config.artifact_threshold)
                
                # Send update to Process 2
                self.math_control_pipe.send(self.config)
                print(f"Loaded preset '{preset_name}': {self.config}")
                
                dpg.delete_item("load_preset_window")
        except Exception as e:
            print(f"Error loading preset: {e}")

    def create_history_window(self):
        if self.current_user_id is None:
            print("Please select a user first.")
            return
            
        try:
            history = self.db.get_session_history(self.current_user_id, limit=20)
            
            if dpg.does_item_exist("history_window"):
                dpg.delete_item("history_window")
                
            with dpg.window(label="Session History", width=600, height=400, tag="history_window"):
                if not history:
                    dpg.add_text("No sessions found for this user.")
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
            print(f"Error loading history: {e}")

    def start_resonance_assessment(self):
        """Initiates the resonance frequency assessment."""
        print("Starting Resonance Assessment...")
        cmd = SystemCommand(command=CommandType.START_RESONANCE_ASSESSMENT)
        # We send this to the Math process via the control pipe
        # Note: The math process needs to handle this command type
        # For now, we just update the UI to show it's active
        dpg.configure_item("assessment_progress", show=True)
        dpg.set_value("assessment_status", "Status: Initializing...")
        
        # Send command to the math process
        self.math_control_pipe.send(cmd)

    def update_ecg_plot(self):
        """Read from shared memory and update plot."""
        if self.shm is None:
            self._attach_shared_memory()
            return

        if self.ecg_display_buffer is not None:
            try:
                data = np.array(self.ecg_display_buffer)
                dpg.set_value("ecg_series", [self.ecg_plot_data_x, data])
            except Exception as e:
                print(f"Error updating plot: {e}")

    def update_metrics_display(self, data: ProcessedData):
        dpg.set_value("hr_big_display", f"{data.heart_rate:.1f} BPM")
        dpg.set_value("coherence_big_display", f"{data.coherence_score:.1f}")
        dpg.set_value("rmssd_display", f"{data.hrv_rmssd:.1f} ms")
        dpg.set_value("sdnn_display", f"{data.hrv_sdnn:.1f} ms")
        dpg.set_value("quality_bar", data.quality_score)
        
        # Update history
        if self.start_time is None:
            self.start_time = data.timestamp
            
        current_time = data.timestamp - self.start_time
        
        self.time_history.append(current_time)
        self.hr_history.append(data.heart_rate)
        self.rmssd_history.append(data.hrv_rmssd)
        self.sdnn_history.append(data.hrv_sdnn)
        self.coherence_history.append(data.coherence_score)
        
        # Update plots
        t_data = list(self.time_history)
        dpg.set_value("hr_series", [t_data, list(self.hr_history)])
        dpg.set_value("rmssd_series", [t_data, list(self.rmssd_history)])
        dpg.set_value("sdnn_series", [t_data, list(self.sdnn_history)])
        dpg.set_value("coherence_series", [t_data, list(self.coherence_history)])
        
        # Fit axes
        for axis in ["hr_x_axis", "hr_y_axis", "rmssd_x_axis", "rmssd_y_axis", "coherence_x_axis", "coherence_y_axis"]:
            dpg.fit_axis_data(axis)

        # Update Assessment Status if active
        if data.is_assessing:
            dpg.set_value("assessment_status", f"Status: {data.assessment_stage}")
            dpg.set_value("assessment_progress", data.assessment_progress)
            dpg.configure_item("assessment_progress", show=True)
        else:
            dpg.set_value("assessment_status", "Status: Idle")
            dpg.configure_item("assessment_progress", show=False)

    def poll_data_pipe(self):
        """Check for new data from Process 2."""
        try:
            while self.data_pipe.poll():
                try:
                    data = self.data_pipe.recv()
                    if isinstance(data, ProcessedData):
                        self.update_metrics_display(data)
                        
                        # If recording, save to DB
                        if self.is_recording and self.current_session_id:
                            self.db.save_hrv_data(self.current_session_id, data)
                except EOFError:
                    break
                except Exception as e:
                    print(f"Error receiving data: {e}")
                    break
        except Exception as e:
            print(f"Critical error in data pipe polling: {e}")

    def poll_ble_status(self):
        """Check for status updates from BLE process."""
        try:
            while self.ble_control_pipe.poll():
                try:
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
                except EOFError:
                    break
                except Exception as e:
                    print(f"Error receiving BLE status: {e}")
                    break
        except Exception as e:
            print(f"Critical error in BLE status polling: {e}")

    def run(self):
        print("[DEBUG] UIManager.run() called, about to call setup_ui()...")
        self.setup_ui()
        print("[DEBUG] setup_ui() completed, showing viewport...")
        dpg.show_viewport()
        
        # Auto-connect if requested
        if self.auto_connect:
            print("[DEBUG] Auto-connect enabled, sending connect command...")
            cmd = BLECommand(command="connect", params={})
            self.ble_control_pipe.send(cmd)
        
        # Main render loop
        while dpg.is_dearpygui_running():
            self.poll_data_pipe()
            self.poll_ble_status()
            self.update_ecg_plot()
            
            # Update Pacer
            if self.pacer_active:
                # Get viewport dimensions for centering
                # Note: dpg.get_viewport_width() might return 0 initially
                vw = dpg.get_viewport_width()
                vh = dpg.get_viewport_height()
                if vw > 0 and vh > 0:
                    # We want to center it in the "Center Panel"
                    # Center Panel is roughly 50% of width, offset by Left Panel (250px)
                    # But we are drawing into a drawlist of fixed size (600x400)
                    # So we just update the pacer with the drawlist dimensions
                    self.pacer.update(600, 400)
            
            dpg.render_dearpygui_frame()
            
        dpg.destroy_context()
        
        # Cleanup
        if self.shm:
            self.shm.close()
