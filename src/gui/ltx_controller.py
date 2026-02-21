import json
import os
import copy
import logging
import dearpygui.dearpygui as dpg
from typing import List, Dict, Optional

# Import modules
from src.gui.ltx.ltx_defs import *
from src.gui.ltx.ltx_engine import LTXEngine

logger = logging.getLogger(__name__)
CONFIG_FILE = "ltx_config.json"

class LTXApp:
    def __init__(self):
        self.engine = LTXEngine()
        
        # Profile State
        self.profiles: Dict[str, Dict] = {}
        self.active_profile: str = "Default"
        self.ips: List[str] = []
        self.triggers: List[Dict] = []
        
        # Data Cache
        self.data_state = {DEV_H10: {}, DEV_PVS: {}, DEV_GENKI: {}}
        
        self._load_config()
        self._editing_idx: Optional[int] = None
        
        # UI Tags
        self.tag_node = "ltx_app_node"
        self.tag_ip_list = "ltx_ip_list"
        self.tag_trigger_table = "ltx_trigger_table"
        self.tag_modal = "ltx_trig_modal"

    # --- Data Ingestion ---
    def feed_h10_metrics(self, hr=None, rr=None):
        if hr: self.data_state[DEV_H10]["HR"] = hr
        if rr: self.data_state[DEV_H10]["RR"] = rr
        self._tick_engine()

    def feed_h10_acc(self, x, y, z):
        self.data_state[DEV_H10]["Acc X"], self.data_state[DEV_H10]["Acc Y"], self.data_state[DEV_H10]["Acc Z"] = x, y, z
        self._tick_engine()

    def feed_pvs_data(self, samples):
        if not samples: return
        s = samples[-1]
        ds = self.data_state[DEV_PVS]
        if s.hr_bpm: ds["HR"] = s.hr_bpm
        if s.acc: ds["Acc X"], ds["Acc Y"], ds["Acc Z"] = s.acc
        if s.gyro: ds["Gyro X"], ds["Gyro Y"], ds["Gyro Z"] = s.gyro
        if s.mag: ds["Mag X"], ds["Mag Y"], ds["Mag Z"] = s.mag
        self._tick_engine()

    def feed_genki_data(self, samples):
        if not samples: return
        s = samples[-1]
        ds = self.data_state[DEV_GENKI]
        if s.acc: ds["Acc X"], ds["Acc Y"], ds["Acc Z"] = s.acc
        if s.gyro: ds["Gyro X"], ds["Gyro Y"], ds["Gyro Z"] = s.gyro
        if s.mag: ds["Mag X"], ds["Mag Y"], ds["Mag Z"] = s.mag
        self._tick_engine()

    def _tick_engine(self):
        self.engine.tick(self.triggers, self.data_state)
        self._update_visuals()

    def _update_visuals(self):
        if not dpg.is_dearpygui_running(): return
        for i in range(3):
            tag = f"ltx_vis_{i}"
            if dpg.does_item_exist(tag):
                c = self.engine.get_ball_color(i)
                dpg.configure_item(tag, fill=(c[0], c[1], c[2], 255))

    # --- Persistence ---
    def _load_config(self):
        if not os.path.exists(CONFIG_FILE):
            self._create_default_profile()
            return
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                self.profiles = data.get("profiles", {})
                self.active_profile = data.get("active_profile", "Default")
                if not self.profiles: self._create_default_profile()
        except:
            self._create_default_profile()
        self._load_active_profile()

    def _create_default_profile(self):
        self.profiles = {"Default": {"ips": ["10.122.252.133"], "triggers": []}}
        self.active_profile = "Default"
        self._load_active_profile()

    def _load_active_profile(self):
        prof = self.profiles.get(self.active_profile, {})
        self.ips = copy.deepcopy(prof.get("ips", []))
        self.triggers = copy.deepcopy(prof.get("triggers", []))
        self.engine.update_ips(self.ips)

    def _save_config(self):
        self.profiles[self.active_profile] = {
            "ips": copy.deepcopy(self.ips),
            "triggers": copy.deepcopy(self.triggers)
        }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"active_profile": self.active_profile, "profiles": self.profiles}, f, indent=2)
            logger.info("LTX Config saved.")
        except Exception as e:
            logger.error(f"Failed to save LTX config: {e}")
        self.engine.update_ips(self.ips)

    # --- GUI Build ---
    def build(self, parent):
        with dpg.tree_node(label="LTX Controller", parent=parent, tag=self.tag_node):
            
            # 1. Profile Row
            with dpg.group(horizontal=True):
                dpg.add_text("Profile:")
                dpg.add_combo(list(self.profiles.keys()), default_value=self.active_profile, width=200, 
                              callback=self._cb_profile_changed, tag="ltx_prof_combo")
                dpg.add_button(label="Save As...", callback=self._cb_save_as)
                dpg.add_button(label="Delete", callback=self._cb_delete_prof)

            dpg.add_separator()

            # 2. Visuals
            with dpg.group(horizontal=True):
                dpg.add_text("   Ball 1        Ball 2        Ball 3", color=(150,150,150))
            with dpg.drawlist(width=400, height=70):
                for i in range(3):
                    x = 40 + (i*100)
                    dpg.draw_circle((x,35), 27, color=(255,255,255), thickness=2)
                    dpg.draw_circle((x,35), 25, fill=(30,30,30), tag=f"ltx_vis_{i}")
                    dpg.draw_text((x-5, 25), str(i+1), size=20)

            dpg.add_separator()

            # 3. Columns
            with dpg.group(horizontal=True):
                # IPs
                with dpg.child_window(width=200, height=350):
                    dpg.add_text("IP Addresses", color=(0,255,255))
                    dpg.add_input_text(tag="ltx_ip_in", hint="10.122.252.xxx", width=-1)
                    dpg.add_button(label="Add", callback=self._cb_add_ip, width=-1)
                    dpg.add_listbox(self.ips, tag=self.tag_ip_list, width=-1, num_items=10)
                    dpg.add_button(label="Remove", callback=self._cb_rem_ip, width=-1)

                # Triggers
                with dpg.child_window(width=-1, height=350):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Logic & Actions", color=(0,255,0))
                        dpg.add_button(label="+ Add Trigger", callback=lambda s,a,u: self._open_modal(None))
                    
                    with dpg.table(tag=self.tag_trigger_table, header_row=True, resizable=True, 
                                   policy=dpg.mvTable_SizingStretchProp, scrollY=True):
                        dpg.add_table_column(label="Input", width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column(label="Check", width_fixed=True, init_width_or_weight=80)
                        dpg.add_table_column(label="Gesture", width_fixed=True, init_width_or_weight=80)
                        dpg.add_table_column(label="Action")
                        dpg.add_table_column(label="Edit", width_fixed=True, init_width_or_weight=90)
            
            self._render_table()

    def _render_table(self):
        if dpg.does_item_exist(self.tag_trigger_table):
            children = dpg.get_item_children(self.tag_trigger_table, slot=1)
            if children:
                for child in children: dpg.delete_item(child)
            
        for idx, t in enumerate(self.triggers):
            with dpg.table_row(parent=self.tag_trigger_table):
                # Input
                axes = t.get('axes',[])
                lbl = f"{t['device']}\n{t['metric']} ({'+'.join(axes) if axes else '-'})"
                dpg.add_text(lbl)
                
                # Check
                dpg.add_text(f"{t.get('operator','>')} {t.get('threshold',0)}")
                
                # Gesture
                gest_val = t.get('gesture', GestureType.INSTANT.value)
                dpg.add_text(gest_val)
                
                # Action
                act_type = t.get('action_type', ActionType.SET_COLOR.value)
                targets = ",".join([str(i+1) for i in t.get('targets',[])])
                
                with dpg.group():
                    dpg.add_text(f"{act_type} -> Balls [{targets}]")
                    if act_type == ActionType.SET_COLOR.value:
                        dpg.add_color_button(t.get('color'), width=30, height=15, no_drag_drop=True)
                    elif act_type in [ActionType.NEXT_IN_CYCLE.value, ActionType.PREV_IN_CYCLE.value]:
                        dpg.add_text(f"Cycle: {t.get('cycle_name')}")

                # Controls
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Edit", user_data=idx, callback=lambda s,a,u: self._open_modal(u))
                    dpg.add_button(label="X", user_data=idx, callback=self._cb_del_trig)

    # --- Callbacks ---
    def _cb_profile_changed(self, s, a):
        self.active_profile = a
        self._load_active_profile()
        dpg.configure_item(self.tag_ip_list, items=self.ips)
        self._render_table()
        self._save_config()

    def _cb_save_as(self, s, a, u):
        if dpg.does_item_exist("ltx_save_as_win"): dpg.delete_item("ltx_save_as_win")
        with dpg.window(modal=True, label="Save As", width=300, height=100, tag="ltx_save_as_win"):
            dpg.add_input_text(tag="ltx_new_prof_name")
            dpg.add_button(label="Save", callback=self._cb_do_save_as)

    def _cb_do_save_as(self, s, a, u):
        name = dpg.get_value("ltx_new_prof_name")
        if name:
            self.profiles[name] = copy.deepcopy(self.profiles[self.active_profile])
            self.active_profile = name
            dpg.configure_item("ltx_prof_combo", items=list(self.profiles.keys()), default_value=name)
            self._save_config()
            dpg.delete_item("ltx_save_as_win")

    def _cb_delete_prof(self, s, a, u):
        if len(self.profiles) > 1:
            del self.profiles[self.active_profile]
            self.active_profile = list(self.profiles.keys())[0]
            self._load_active_profile()
            dpg.configure_item("ltx_prof_combo", items=list(self.profiles.keys()), default_value=self.active_profile)
            dpg.configure_item(self.tag_ip_list, items=self.ips)
            self._render_table()
            self._save_config()

    def _cb_add_ip(self, s, a, u):
        ip = dpg.get_value("ltx_ip_in")
        if ip and ip not in self.ips:
            self.ips.append(ip)
            dpg.configure_item(self.tag_ip_list, items=self.ips)
            self._save_config()
            self.engine.update_ips(self.ips)

    def _cb_rem_ip(self, s, a, u):
        sel = dpg.get_value(self.tag_ip_list)
        if sel in self.ips:
            self.ips.remove(sel)
            dpg.configure_item(self.tag_ip_list, items=self.ips)
            self._save_config()
            self.engine.update_ips(self.ips)

    def _cb_del_trig(self, s, a, u):
        if 0 <= u < len(self.triggers):
            self.triggers.pop(u)
            self._render_table()
            self._save_config()

    # --- Trigger Editor Modal ---
    def _open_modal(self, idx):
        self._editing_idx = idx
        if dpg.does_item_exist(self.tag_modal): dpg.delete_item(self.tag_modal)
        
        with dpg.window(label="Trigger Editor", modal=True, tag=self.tag_modal, width=450, height=600):
            # 1. INPUT
            dpg.add_text("1. Signal Input", color=(0,255,255))
            dpg.add_combo([DEV_H10, DEV_PVS, DEV_GENKI], label="Device", tag="m_dev", callback=self._m_dev_sel)
            dpg.add_combo([], label="Metric", tag="m_met", callback=self._m_met_sel)
            with dpg.group(horizontal=True, tag="m_axes_grp", show=False):
                dpg.add_text("Axes Sum: ")
                dpg.add_checkbox(label="X", tag="m_ax_x", default_value=True)
                dpg.add_checkbox(label="Y", tag="m_ax_y", default_value=True)
                dpg.add_checkbox(label="Z", tag="m_ax_z", default_value=True)
            
            dpg.add_separator()
            
            # 2. LOGIC
            dpg.add_text("2. Condition & Gesture", color=(0,255,255))
            with dpg.group(horizontal=True):
                dpg.add_combo([">", "<"], default_value=">", width=50, tag="m_op")
                dpg.add_input_float(default_value=0, width=120, tag="m_thresh", step=10)
            
            dpg.add_text("Gesture Type:")
            gestures = [g.value for g in GestureType]
            dpg.add_combo(gestures, default_value=GestureType.INSTANT.value, tag="m_gest")
            
            dpg.add_separator()

            # 3. ACTION
            dpg.add_text("3. Action", color=(0,255,255))
            actions = [a.value for a in ActionType]
            dpg.add_combo(actions, default_value=ActionType.SET_COLOR.value, tag="m_act", callback=self._m_act_sel)
            
            # Dynamic Action Params
            with dpg.group(tag="grp_color"):
                dpg.add_color_picker(display_type=dpg.mvColorEdit_uint8, tag="m_col", height=100, no_alpha=True, default_value=(255,0,0,255))
            with dpg.group(tag="grp_cycle", show=False):
                dpg.add_combo(list(self.engine.cycles.keys()), label="Select Cycle", tag="m_cyc_name", default_value="Traffic")

            dpg.add_text("Target Balls:")
            with dpg.group(horizontal=True):
                for i in range(3): dpg.add_checkbox(label=f"Ball {i+1}", tag=f"m_tgt_{i}", default_value=True)

            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save", callback=self._save_trigger, width=80)
                dpg.add_button(label="Cancel", callback=lambda: dpg.delete_item(self.tag_modal), width=80)

        if idx is not None: self._populate_modal(self.triggers[idx])

    def _m_dev_sel(self, s, a):
        mets = list(METRIC_DEFS.get(a, {}).keys())
        dpg.configure_item("m_met", items=mets)
        if mets: 
            dpg.set_value("m_met", mets[0])
            self._m_met_sel(None, mets[0])

    def _m_met_sel(self, s, a):
        dev = dpg.get_value("m_dev")
        info = METRIC_DEFS.get(dev, {}).get(a)
        dpg.configure_item("m_axes_grp", show=isinstance(info, list))

    def _m_act_sel(self, s, a):
        dpg.configure_item("grp_color", show=(a == ActionType.SET_COLOR.value))
        is_cycle = (a == ActionType.NEXT_IN_CYCLE.value or a == ActionType.PREV_IN_CYCLE.value)
        dpg.configure_item("grp_cycle", show=is_cycle)

    def _save_trigger(self, s, a, u):
        dev = dpg.get_value("m_dev")
        if not dev: return

        t = {
            "device": dev,
            "metric": dpg.get_value("m_met"),
            "axes": [ax for ax in ['x','y','z'] if dpg.get_value(f"m_ax_{ax}")],
            "operator": dpg.get_value("m_op"),
            "threshold": dpg.get_value("m_thresh"),
            "gesture": dpg.get_value("m_gest"),
            "action_type": dpg.get_value("m_act"),
            "color": dpg.get_value("m_col"),
            "cycle_name": dpg.get_value("m_cyc_name"),
            "targets": [i for i in range(3) if dpg.get_value(f"m_tgt_{i}")]
        }

        if self._editing_idx is not None:
            self.triggers[self._editing_idx] = t
            logger.info(f"Trigger {self._editing_idx} updated.")
        else:
            self.triggers.append(t)
            logger.info("New Trigger added.")
        
        self._save_config()
        self._render_table()
        dpg.delete_item(self.tag_modal)

    def _populate_modal(self, t):
        dpg.set_value("m_dev", t['device'])
        self._m_dev_sel(None, t['device'])
        dpg.set_value("m_met", t['metric'])
        self._m_met_sel(None, t['metric'])
        
        for ax in ['x','y','z']: dpg.set_value(f"m_ax_{ax}", ax in t.get('axes',[]))
        
        dpg.set_value("m_op", t.get('operator','>'))
        dpg.set_value("m_thresh", float(t.get('threshold',0)))
        dpg.set_value("m_gest", t.get('gesture', GestureType.INSTANT.value))
        
        act = t.get('action_type', ActionType.SET_COLOR.value)
        dpg.set_value("m_act", act)
        self._m_act_sel(None, act)
        
        dpg.set_value("m_col", t.get('color', [255,0,0,255]))
        dpg.set_value("m_cyc_name", t.get('cycle_name', "Traffic"))
        
        tgts = t.get('targets',[])
        for i in range(3): dpg.set_value(f"m_tgt_{i}", i in tgts)