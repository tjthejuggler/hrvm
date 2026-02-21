import socket
import struct
import json
import os
import time
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
    Manages IPs, Triggers, and the GUI.
    """
    
    # Device Constants
    DEV_H10 = "Polar H10"
    DEV_PVS = "Polar Verity Sense"
    DEV_GENKI = "Genki Wave"

    # Metric Definition:
    # Key = Display Name
    # Value = List of internal keys if Vector (X, Y, Z), or None if Scalar
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
            # Adding P/R as scalars for simplicity or vectors if preferred
            "Pitch": None,
            "Roll": None
        }
    }

    def __init__(self):
        self.ips: List[str] = []
        self.triggers: List[Dict] = []
        self.senders: Dict[str, LTXSender] = {}
        self._last_trigger_times: Dict[int, float] = {} 
        
        # Flattened Data State (Internal Keys)
        # E.g. "Acc X", "HR"
        self.data_state = {
            self.DEV_H10: {},
            self.DEV_PVS: {},
            self.DEV_GENKI: {}
        }

        self._load_config()

        # GUI Tags
        self.tag_node = "ltx_app_node"
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

    # --- Persistence ---

    def _load_config(self):
        if not os.path.exists(CONFIG_FILE):
            self.ips = ["10.122.252.133"] 
            self._update_senders()
            return
        
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                self.ips = data.get("ips", [])
                self.triggers = data.get("triggers", [])
                self._update_senders()
        except Exception as e:
            logger.error(f"Failed to load LTX config: {e}")

    def _save_config(self):
        data = {
            "ips": self.ips,
            "triggers": self.triggers
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
            
            # --- Value Calculation ---
            metric_name = trig.get('metric') # e.g., "Accelerometer" or "HR"
            axes = trig.get('axes', []) # e.g., ["x", "y"] or []
            
            dev_def = self.METRIC_DEFS.get(device_updated, {})
            metric_info = dev_def.get(metric_name)
            
            current_val = 0.0
            data_map = self.data_state[device_updated]

            # 1. Scalar Case (HR, RR)
            if metric_info is None: 
                # Internal key same as display name for scalars in my setup
                val = data_map.get(metric_name)
                if val is None: continue
                current_val = float(val)

            # 2. Vector Case (Acc, Gyro) - Sum of Absolute Values (Manhattan)
            else:
                # metric_info is list ["Acc X", "Acc Y", "Acc Z"]
                # axes is list ["x", "y", "z"] from user selection
                
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

            # --- Condition Check ---
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
        for ip_idx in target_indices:
            if 0 <= ip_idx < len(self.ips):
                ip = self.ips[ip_idx]
                if ip in self.senders:
                    self.senders[ip].send_color(r, g, b)

    # --- GUI Construction ---

    def build(self, parent_tag):
        with dpg.tree_node(label="LTX Controller", parent=parent_tag, tag=self.tag_node):
            
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
                        dpg.add_button(label="+ Add Trigger", callback=self._cb_open_modal)
                    
                    dpg.add_separator()
                    
                    with dpg.table(tag=self.tag_trigger_table, header_row=True, 
                                   resizable=True, policy=dpg.mvTable_SizingStretchProp,
                                   scrollY=True):
                        dpg.add_table_column(label="Dev", width_fixed=True)
                        dpg.add_table_column(label="Metric", width_fixed=True)
                        dpg.add_table_column(label="Axes", width_fixed=True)
                        dpg.add_table_column(label="Cond", width_fixed=True)
                        dpg.add_table_column(label="Color", width_fixed=True)
                        dpg.add_table_column(label="Targets")
                        dpg.add_table_column(label="Action", width_fixed=True)
            
            dpg.add_separator()
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
                
                # Axes column
                axes = trig.get('axes', [])
                if axes:
                    dpg.add_text("+".join([a.upper() for a in axes]))
                else:
                    dpg.add_text("-")
                
                dpg.add_text(f"{trig['operator']} {trig['threshold']}")
                
                dpg.add_color_button(trig['color'], no_inputs=True, no_tooltip=True, width=30, height=20)
                
                t_str = ",".join([str(i+1) for i in trig['targets']])
                dpg.add_text(f"#{t_str}")
                
                dpg.add_button(label="Del", user_data=idx, callback=self._cb_del_trigger, width=40)

    # --- Callbacks ---

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

    # --- Modal Logic (Add Trigger) ---

    def _cb_open_modal(self):
        if dpg.does_item_exist(self.tag_modal):
            dpg.delete_item(self.tag_modal)
        
        with dpg.window(label="Add Trigger", modal=True, tag=self.tag_modal, width=400, height=500):
            dpg.add_text("1. Input Signal")
            
            # Device Selection
            dpg.add_combo(items=[self.DEV_H10, self.DEV_PVS, self.DEV_GENKI], 
                          label="Device", tag=self.tag_combo_dev,
                          callback=self._cb_device_selected)
            
            # Metric Selection
            dpg.add_combo(items=[], label="Metric", tag=self.tag_combo_metric,
                          callback=self._cb_metric_selected)
            
            # Axes Selection (Hidden by default)
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
                if not self.ips:
                    dpg.add_text("(No IPs added yet)", color=(150,150,150))
                for i, ip in enumerate(self.ips):
                    dpg.add_checkbox(label=f"#{i+1}", tag=f"ltx_chk_{i}", default_value=True)

            dpg.add_separator()
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=self._cb_save_trigger, width=80)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item(self.tag_modal), width=80)

    def _cb_device_selected(self, sender, app_data):
        dev = app_data
        metrics = list(self.METRIC_DEFS.get(dev, {}).keys())
        dpg.configure_item(self.tag_combo_metric, items=metrics)
        dpg.configure_item(self.tag_group_axes, show=False) # Hide until metric chosen
        
        if metrics:
            dpg.set_value(self.tag_combo_metric, metrics[0])
            self._cb_metric_selected(None, metrics[0])

    def _cb_metric_selected(self, sender, app_data):
        dev = dpg.get_value(self.tag_combo_dev)
        metric = app_data
        
        info = self.METRIC_DEFS.get(dev, {}).get(metric)
        
        # If info is a list (vector), show axes checkboxes
        is_vector = isinstance(info, list)
        dpg.configure_item(self.tag_group_axes, show=is_vector)

    def _cb_save_trigger(self):
        dev = dpg.get_value(self.tag_combo_dev)
        met = dpg.get_value(self.tag_combo_metric)
        op = dpg.get_value(self.tag_combo_op)
        thresh = dpg.get_value(self.tag_input_thresh)
        color = dpg.get_value(self.tag_color_picker)
        
        # Get targets
        targets = []
        for i in range(len(self.ips)):
            chk_tag = f"ltx_chk_{i}"
            if dpg.does_item_exist(chk_tag) and dpg.get_value(chk_tag):
                targets.append(i)
        
        # Get Axes if applicable
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
        
        self.triggers.append(new_trig)
        self._save_config()
        self._render_trigger_table()
        dpg.delete_item(self.tag_modal)