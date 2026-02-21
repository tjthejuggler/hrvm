import socket
import struct
import json
import os
import time
import copy
import logging
import dearpygui.dearpygui as dpg
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

CONFIG_FILE = "ltx_config.json"

# --- Protocol & Networking ---

class LTXSender:
    """Handles raw UDP communication with a specific LTX LED Ball."""
    def __init__(self, ip: str, port: int = 41412):
        self.ip = ip
        self.port = port

    def send_color(self, r: int, g: int, b: int):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # 8-byte Header: 66, 0, 0, 0 (!bIBH)
                udp_header = struct.pack("!bIBH", 66, 0, 0, 0)
                # Command: 0x0a + R + G + B
                color_data = struct.pack("!BBBB", 0x0a, r, g, b)
                sock.sendto(udp_header + color_data, (self.ip, self.port))
            finally:
                sock.close()
        except Exception:
            pass 

# --- Data & Logic ---

class LTXApp:
    """
    The main controller for the LTX App.
    Manages Profiles, IPs, Triggers, Visual Feedback, and the GUI.
    """
    
    DEV_H10 = "Polar H10"
    DEV_PVS = "Polar Verity Sense"
    DEV_GENKI = "Genki Wave"

    METRIC_DEFS = {
        DEV_H10: {
            "HR": None,
            "RR": None,
            "Accelerometer": ["Acc X", "Acc Y", "Acc Z"], 
            "ECG": None
        },
        DEV_PVS: {
            "HR": None,
            "Accelerometer": ["Acc X", "Acc Y", "Acc Z"],
            "Gyroscope": ["Gyro X", "Gyro Y", "Gyro Z"],
            "Magnetometer": ["Mag X", "Mag Y", "Mag Z"]
        },
        DEV_GENKI: {
            "Accelerometer": ["Acc X", "Acc Y", "Acc Z"],
            "Gyroscope": ["Gyro X", "Gyro Y", "Gyro Z"],
            "Magnetometer": ["Mag X", "Mag Y", "Mag Z"],
            "Pitch": None,
            "Roll": None
        }
    }

    def __init__(self):
        # Profile System State
        self.profiles: Dict[str, Dict] = {}
        self.active_profile: str = "Default"
        
        # Working State (Synced to Active Profile)
        self.ips: List[str] = []
        self.triggers: List[Dict] = []
        
        # Runtime Objects
        self.senders: Dict[str, LTXSender] = {}
        self._last_trigger_times: Dict[int, float] = {} 
        self._editing_idx: Optional[int] = None
        
        self.data_state = {
            self.DEV_H10: {},
            self.DEV_PVS: {},
            self.DEV_GENKI: {}
        }

        self._load_config()

        # GUI Tags
        self.tag_node = "ltx_app_node"
        self.tag_profile_combo = "ltx_profile_combo"
        self.tag_ip_list = "ltx_ip_list"
        self.tag_ip_input = "ltx_ip_input"
        self.tag_trigger_table = "ltx_trigger_table"
        
        # Modal Tags
        self.tag_modal = "ltx_trigger_modal"
        self.tag_combo_dev = "ltx_m_dev"
        self.tag_combo_metric = "ltx_m_metric"
        self.tag_group_axes = "ltx_m_axes_group"
        self.tag_chk_x = "ltx_m_x"
        self.tag_chk_y = "ltx_m_y"
        self.tag_chk_z = "ltx_m_z"
        self.tag_input_thresh = "ltx_m_thresh"
        self.tag_combo_op = "ltx_m_op"
        self.tag_color_picker = "ltx_m_color"
        self.tag_target_balls = "ltx_m_targets" 

    # --- Persistence & Profiles ---

    def _load_config(self):
        if not os.path.exists(CONFIG_FILE):
            self._create_default_profile()
            return
        
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                
                # Migrate Legacy Format to Profile Format
                if "profiles" in data:
                    self.profiles = data["profiles"]
                    self.active_profile = data.get("active_profile", "Default")
                else:
                    self.profiles = {
                        "Default": {
                            "ips": data.get("ips", []),
                            "triggers": data.get("triggers", [])
                        }
                    }
                    self.active_profile = "Default"

        except Exception as e:
            logger.error(f"Failed to load LTX config: {e}")
            self._create_default_profile()

        # Failsafe
        if self.active_profile not in self.profiles:
            if not self.profiles:
                self._create_default_profile()
            else:
                self.active_profile = list(self.profiles.keys())[0]

        self._load_active_profile()

    def _create_default_profile(self):
        self.profiles = {
            "Default": {
                "ips": ["10.122.252.133"],
                "triggers": []
            }
        }
        self.active_profile = "Default"
        self._load_active_profile()

    def _load_active_profile(self):
        """Loads data from the active profile dict into working variables."""
        prof = self.profiles[self.active_profile]
        self.ips = copy.deepcopy(prof.get("ips", []))
        self.triggers = copy.deepcopy(prof.get("triggers", []))
        self._update_senders()

    def _save_config(self):
        """Syncs working variables back to active profile dict, then saves to file."""
        self.profiles[self.active_profile] = {
            "ips": copy.deepcopy(self.ips),
            "triggers": copy.deepcopy(self.triggers)
        }
        
        data = {
            "active_profile": self.active_profile,
            "profiles": self.profiles
        }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save LTX config: {e}")

    def _update_senders(self):
        self.senders = {ip: LTXSender(ip) for ip in self.ips}

    # --- Data Ingestion ---

    def feed_h10_metrics(self, hr: float = None, rr: float = None):
        if hr is not None: self.data_state[self.DEV_H10]["HR"] = hr
        if rr is not None: self.data_state[self.DEV_H10]["RR"] = rr
        self._check_triggers(self.DEV_H10)

    def feed_h10_acc(self, x, y, z):
        self.data_state[self.DEV_H10]["Acc X"] = x
        self.data_state[self.DEV_H10]["Acc Y"] = y
        self.data_state[self.DEV_H10]["Acc Z"] = z
        self._check_triggers(self.DEV_H10)

    def feed_pvs_data(self, samples: List):
        if not samples: return
        s = samples[-1]
        ds = self.data_state[self.DEV_PVS]
        
        if s.hr_bpm and s.hr_bpm > 0: ds["HR"] = s.hr_bpm
        elif s.ppi_hr and s.ppi_hr > 0: ds["HR"] = s.ppi_hr
        
        if s.acc: ds["Acc X"], ds["Acc Y"], ds["Acc Z"] = s.acc
        if s.gyro: ds["Gyro X"], ds["Gyro Y"], ds["Gyro Z"] = s.gyro
        if s.mag: ds["Mag X"], ds["Mag Y"], ds["Mag Z"] = s.mag
        
        self._check_triggers(self.DEV_PVS)

    def feed_genki_data(self, samples: List):
        if not samples: return
        s = samples[-1]
        ds = self.data_state[self.DEV_GENKI]
        
        if s.acc: ds["Acc X"], ds["Acc Y"], ds["Acc Z"] = s.acc
        if s.gyro: ds["Gyro X"], ds["Gyro Y"], ds["Gyro Z"] = s.gyro
        if s.mag: ds["Mag X"], ds["Mag Y"], ds["Mag Z"] = s.mag
        
        self._check_triggers(self.DEV_GENKI)

    # --- Trigger Logic ---

    def _check_triggers(self, device_updated: str):
        now = time.time()
        
        for idx, trig in enumerate(self.triggers):
            if trig.get('device') != device_updated:
                continue
            
            metric_name = trig.get('metric')
            axes = trig.get('axes', [])
            
            dev_def = self.METRIC_DEFS.get(device_updated, {})
            metric_info = dev_def.get(metric_name)
            
            current_val = 0.0
            data_map = self.data_state[device_updated]

            # 1. Scalar
            if metric_info is None: 
                val = data_map.get(metric_name)
                if val is None: continue
                current_val = float(val)

            # 2. Vector (Manhattan Sum of selected axes)
            else:
                valid_read = False
                temp_sum = 0.0
                
                if 'x' in axes and len(metric_info) > 0:
                    v = data_map.get(metric_info[0])
                    if v is not None: 
                        temp_sum += abs(v)
                        valid_read = True
                
                if 'y' in axes and len(metric_info) > 1:
                    v = data_map.get(metric_info[1])
                    if v is not None: 
                        temp_sum += abs(v)
                        valid_read = True
                
                if 'z' in axes and len(metric_info) > 2:
                    v = data_map.get(metric_info[2])
                    if v is not None: 
                        temp_sum += abs(v)
                        valid_read = True
                
                if not valid_read:
                    continue
                current_val = temp_sum

            # Condition
            threshold = float(trig.get('threshold', 0.0))
            op = trig.get('operator', '>')
            triggered = False
            
            if op == ">":
                if current_val > threshold: triggered = True
            elif op == "<":
                if current_val < threshold: triggered = True
            
            # Rate limiting (100ms)
            last_time = self._last_trigger_times.get(idx, 0)
            if triggered and (now - last_time > 0.1):
                self._execute_command(trig)
                self._last_trigger_times[idx] = now

    def _execute_command(self, trigger):
        color = trigger.get('color', [255, 255, 255, 255])
        r, g, b = int(color[0]), int(color[1]), int(color[2])
        target_indices = trigger.get('targets', [])

        for idx in target_indices:
            # 1. Update Visual Circles
            if 0 <= idx < 3:
                vis_tag = f"ltx_ball_vis_{idx}"
                if dpg.does_item_exist(vis_tag):
                    dpg.configure_item(vis_tag, fill=color)

            # 2. Send UDP
            if 0 <= idx < len(self.ips):
                ip = self.ips[idx]
                if ip in self.senders:
                    self.senders[ip].send_color(r, g, b)

    # --- GUI Construction ---

    def build(self, parent_tag):
        with dpg.tree_node(label="LTX Controller", parent=parent_tag, tag=self.tag_node):
            
            # --- PROFILE MANAGEMENT ROW ---
            with dpg.group(horizontal=True):
                dpg.add_text("Config Profile:")
                dpg.add_combo(items=list(self.profiles.keys()), default_value=self.active_profile, 
                              tag=self.tag_profile_combo, width=200, callback=self._cb_profile_changed)
                dpg.add_button(label="Save As New...", callback=self._cb_open_save_as_modal)
                dpg.add_button(label="Delete", callback=self._cb_delete_profile)
                
            dpg.add_separator()
            
            # --- VISUAL FEEDBACK (3 Circles) ---
            with dpg.group(horizontal=True):
                dpg.add_text("   Ball 1        Ball 2        Ball 3", color=(200,200,200))

            with dpg.drawlist(width=400, height=70):
                y_pos = 35
                radius = 25
                x_start = 40
                gap = 100

                for i in range(3):
                    center_x = x_start + (i * gap)
                    dpg.draw_circle(center=(center_x, y_pos), radius=radius+2, 
                                    color=(255, 255, 255, 255), thickness=2)
                    dpg.draw_circle(center=(center_x, y_pos), radius=radius, 
                                    fill=(30, 30, 30, 255), tag=f"ltx_ball_vis_{i}")
                    dpg.draw_text(pos=(center_x-5, y_pos-10), text=str(i+1), size=20, color=(255,255,255,255))
            
            dpg.add_separator()

            with dpg.group(horizontal=True):
                # --- Left Col: IP Management ---
                with dpg.child_window(width=220, height=300):
                    dpg.add_text("LED Balls (IPs)", color=(0, 255, 255))
                    dpg.add_separator()
                    dpg.add_input_text(tag=self.tag_ip_input, hint="10.122.252.XXX", width=-1)
                    dpg.add_button(label="Add IP", callback=self._cb_add_ip, width=-1)
                    dpg.add_spacer(height=5)
                    dpg.add_listbox(items=self.ips, tag=self.tag_ip_list, width=-1, num_items=10)
                    dpg.add_button(label="Remove Selected", callback=self._cb_remove_ip, width=-1)

                # --- Right Col: Triggers ---
                with dpg.child_window(width=-1, height=300):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Triggers & Commands", color=(0, 255, 0))
                        dpg.add_button(label="+ Add Trigger", callback=lambda s, a, u: self._open_trigger_modal(None))
                    
                    dpg.add_separator()
                    
                    with dpg.table(tag=self.tag_trigger_table, header_row=True, 
                                   resizable=True, policy=dpg.mvTable_SizingStretchProp,
                                   scrollY=True):
                        dpg.add_table_column(label="Dev")
                        dpg.add_table_column(label="Metric")
                        dpg.add_table_column(label="Axes", width_fixed=True)
                        dpg.add_table_column(label="Cond")
                        dpg.add_table_column(label="Color", width_fixed=True)
                        dpg.add_table_column(label="Targets")
                        dpg.add_table_column(label="Actions", width_fixed=True, init_width_or_weight=80)
            
            dpg.add_separator()
            self._render_trigger_table()

    def _render_all_ui(self):
        """Updates IP List and Trigger Table after a profile load."""
        if dpg.does_item_exist(self.tag_ip_list):
            dpg.configure_item(self.tag_ip_list, items=self.ips)
        self._render_trigger_table()

    def _render_trigger_table(self):
        if dpg.does_item_exist(self.tag_trigger_table):
            children = dpg.get_item_children(self.tag_trigger_table, slot=1)
            if children:
                for child in children: dpg.delete_item(child)
        
        for idx, trig in enumerate(self.triggers):
            with dpg.table_row(parent=self.tag_trigger_table):
                dpg.add_text(trig['device'])
                dpg.add_text(trig['metric'])
                
                axes = trig.get('axes', [])
                dpg.add_text("+".join([a.upper() for a in axes]) if axes else "-")
                
                dpg.add_text(f"{trig['operator']} {trig['threshold']}")
                
                # FIXED: removed invalid args
                dpg.add_color_button(trig['color'], width=30, height=20, no_drag_drop=True)
                
                t_str = ",".join([str(i+1) for i in trig['targets']])
                dpg.add_text(f"#{t_str}")
                
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Edit", user_data=idx, callback=lambda s, a, u: self._open_trigger_modal(u))
                    dpg.add_button(label="Del", user_data=idx, callback=self._cb_del_trigger)

    # --- Profile Callbacks ---

    def _cb_profile_changed(self, sender, app_data):
        self.active_profile = app_data
        self._load_active_profile()
        self._render_all_ui()
        self._save_config()

    def _cb_open_save_as_modal(self):
        if dpg.does_item_exist("ltx_save_as_modal"):
            dpg.delete_item("ltx_save_as_modal")
            
        with dpg.window(label="Save Profile As...", modal=True, tag="ltx_save_as_modal", width=300, height=130):
            dpg.add_spacer(height=5)
            dpg.add_input_text(label="Name", tag="ltx_new_profile_name", width=200)
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=self._cb_confirm_save_as, width=80)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item("ltx_save_as_modal"), width=80)

    def _cb_confirm_save_as(self):
        new_name = dpg.get_value("ltx_new_profile_name").strip()
        if new_name:
            self.active_profile = new_name
            # Copy state to new profile immediately
            self.profiles[new_name] = {
                "ips": copy.deepcopy(self.ips),
                "triggers": copy.deepcopy(self.triggers)
            }
            # Update Combo
            dpg.configure_item(self.tag_profile_combo, items=list(self.profiles.keys()))
            dpg.set_value(self.tag_profile_combo, new_name)
            self._save_config()
        dpg.delete_item("ltx_save_as_modal")

    def _cb_delete_profile(self):
        if self.active_profile in self.profiles:
            del self.profiles[self.active_profile]
            
        if not self.profiles:
            self._create_default_profile()
            
        self.active_profile = list(self.profiles.keys())[0]
        self._load_active_profile()
        
        dpg.configure_item(self.tag_profile_combo, items=list(self.profiles.keys()))
        dpg.set_value(self.tag_profile_combo, self.active_profile)
        self._render_all_ui()
        self._save_config()

    # --- Config Callbacks ---

    def _cb_add_ip(self, sender, app_data):
        ip = dpg.get_value(self.tag_ip_input).strip()
        if ip and ip not in self.ips:
            self.ips.append(ip)
            self._update_senders()
            dpg.configure_item(self.tag_ip_list, items=self.ips)
            dpg.set_value(self.tag_ip_input, "")
            self._save_config()

    def _cb_remove_ip(self, sender, app_data):
        selected = dpg.get_value(self.tag_ip_list)
        if selected and selected in self.ips:
            self.ips.remove(selected)
            self._update_senders()
            dpg.configure_item(self.tag_ip_list, items=self.ips)
            self._save_config()

    def _cb_del_trigger(self, sender, app_data, user_data):
        idx = user_data
        if 0 <= idx < len(self.triggers):
            self.triggers.pop(idx)
            self._render_trigger_table()
            self._save_config()

    # --- Modal Logic (Add / Edit Trigger) ---

    def _open_trigger_modal(self, trigger_idx: Optional[int]):
        """Opens modal for Adding (trigger_idx=None) or Editing."""
        self._editing_idx = trigger_idx

        if dpg.does_item_exist(self.tag_modal):
            dpg.delete_item(self.tag_modal)
        
        title = "Edit Trigger" if trigger_idx is not None else "Add Trigger"
        
        with dpg.window(label=title, modal=True, tag=self.tag_modal, width=400, height=520):
            dpg.add_text("1. Input Signal")
            dpg.add_combo(items=[self.DEV_H10, self.DEV_PVS, self.DEV_GENKI], 
                          label="Device", tag=self.tag_combo_dev,
                          callback=self._cb_device_selected)
            dpg.add_combo(items=[], label="Metric", tag=self.tag_combo_metric,
                          callback=self._cb_metric_selected)
            
            with dpg.group(horizontal=True, tag=self.tag_group_axes, show=False):
                dpg.add_text("Sum Axes: ")
                dpg.add_checkbox(label="X", tag=self.tag_chk_x, default_value=True)
                dpg.add_checkbox(label="Y", tag=self.tag_chk_y, default_value=True)
                dpg.add_checkbox(label="Z", tag=self.tag_chk_z, default_value=True)
            
            dpg.add_separator()
            dpg.add_text("2. Condition")
            with dpg.group(horizontal=True):
                dpg.add_combo(items=[">", "<"], default_value=">", width=50, tag=self.tag_combo_op)
                dpg.add_input_float(default_value=0.0, width=150, tag=self.tag_input_thresh, step=10.0)
            
            dpg.add_separator()
            dpg.add_text("3. Command")
            dpg.add_color_picker(display_type=dpg.mvColorEdit_uint8, tag=self.tag_color_picker, 
                                 height=100, width=200, no_alpha=True, default_value=(255, 0, 0, 255))
            
            dpg.add_text("Target Balls:")
            with dpg.group(horizontal=True, tag=self.tag_target_balls):
                for i in range(3):
                    dpg.add_checkbox(label=f"Ball {i+1}", tag=f"ltx_chk_{i}", default_value=True)

            dpg.add_separator()
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=self._cb_save_trigger, width=80)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item(self.tag_modal), width=80)

        # Pre-fill data if Editing
        if self._editing_idx is not None:
            t = self.triggers[self._editing_idx]
            
            dpg.set_value(self.tag_combo_dev, t['device'])
            self._cb_device_selected(None, t['device'])  # Populate metrics
            
            dpg.set_value(self.tag_combo_metric, t['metric'])
            self._cb_metric_selected(None, t['metric'])  # Show/Hide axes
            
            axes = t.get('axes', [])
            dpg.set_value(self.tag_chk_x, 'x' in axes)
            dpg.set_value(self.tag_chk_y, 'y' in axes)
            dpg.set_value(self.tag_chk_z, 'z' in axes)
            
            dpg.set_value(self.tag_combo_op, t['operator'])
            dpg.set_value(self.tag_input_thresh, float(t['threshold']))
            dpg.set_value(self.tag_color_picker, t['color'])
            
            targets = t.get('targets', [])
            for i in range(3):
                dpg.set_value(f"ltx_chk_{i}", i in targets)

    def _cb_device_selected(self, sender, app_data):
        dev = app_data
        metrics = list(self.METRIC_DEFS.get(dev, {}).keys())
        dpg.configure_item(self.tag_combo_metric, items=metrics)
        dpg.configure_item(self.tag_group_axes, show=False)
        if metrics:
            # Only auto-select if we aren't pre-filling an edit
            if dpg.get_value(self.tag_combo_metric) not in metrics:
                dpg.set_value(self.tag_combo_metric, metrics[0])
            self._cb_metric_selected(None, dpg.get_value(self.tag_combo_metric))

    def _cb_metric_selected(self, sender, app_data):
        dev = dpg.get_value(self.tag_combo_dev)
        metric = app_data
        info = self.METRIC_DEFS.get(dev, {}).get(metric)
        is_vector = isinstance(info, list)
        dpg.configure_item(self.tag_group_axes, show=is_vector)

    def _cb_save_trigger(self):
        dev = dpg.get_value(self.tag_combo_dev)
        met = dpg.get_value(self.tag_combo_metric)
        op = dpg.get_value(self.tag_combo_op)
        thresh = dpg.get_value(self.tag_input_thresh)
        color = dpg.get_value(self.tag_color_picker)
        
        targets = []
        for i in range(3):
            chk_tag = f"ltx_chk_{i}"
            if dpg.does_item_exist(chk_tag) and dpg.get_value(chk_tag):
                targets.append(i)
        
        axes = []
        if dpg.is_item_shown(self.tag_group_axes):
            if dpg.get_value(self.tag_chk_x): axes.append("x")
            if dpg.get_value(self.tag_chk_y): axes.append("y")
            if dpg.get_value(self.tag_chk_z): axes.append("z")
        
        if not dev or not met: return

        new_trig = {
            "device": dev,
            "metric": met,
            "axes": axes,
            "operator": op,
            "threshold": thresh,
            "color": color,
            "targets": targets
        }
        
        if self._editing_idx is not None:
            self.triggers[self._editing_idx] = new_trig
        else:
            self.triggers.append(new_trig)
            
        self._editing_idx = None
        self._save_config()
        self._render_trigger_table()
        dpg.delete_item(self.tag_modal)