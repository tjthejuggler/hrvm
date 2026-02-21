"""
Resonance Breathing App & Automated RF Assessment System.
"""
import time
import logging
import json
import os
import numpy as np
import dearpygui.dearpygui as dpg

# Import from our new decoupled clinical math engine
from src.processing.resonance_math import PhysiologicalMath, ContinuousPacer, ContinuousSlidingMath

logger = logging.getLogger(__name__)

_PACER_W = 1200
_PACER_H = 60
_HISTORY_FILE = "rf_history.json"

class ResonanceBreathingWidget:
    
    STATE_IDLE = "IDLE"
    STATE_BASELINE = "BASELINE"
    STATE_TESTING = "TESTING"
    STATE_TESTING_CONT = "TESTING_CONTINUOUS"
    STATE_WASHOUT = "WASHOUT"
    STATE_COMPLETE = "COMPLETE"

    def __init__(self, db=None):
        self.db = db
        self._built = False
        self._hr_connected = False
        
        self.math = PhysiologicalMath()
        self.continuous_pacer = None
        
        self.state = self.STATE_IDLE
        self.assessment_start_time = 0.0
        self.block_start_time = 0.0
        self.grid_index = 0
        self.total_assessment_duration = 0.0
        
        self.TARGETED_KEY = "Targeted Micro-Adjustment (Requires History, ~10 mins)"
        self.CONTINUOUS_KEY = "Quick Scan Sliding Window (Continuous, ~16 mins)"
        
        self.presets = {
            self.CONTINUOUS_KEY: {"type": "continuous"},
            "Express Sweep (Fresh, ~8 mins)": {"type": "stepped", "base": 60, "wash": 30, "test": 60, "grid": [(6.5, 1.0), (6.0, 1.0), (5.5, 1.0), (5.0, 1.0), (4.5, 1.0)]},
            "Standard Sweep (Fresh, ~18 mins)": {"type": "stepped", "base": 120, "wash": 60, "test": 120, "grid": [(6.5, 1.0), (6.0, 1.0), (5.5, 1.0), (5.0, 1.0), (4.5, 1.0)]},
            "Deep Calibration Sweep (Fresh, ~42 mins)": {"type": "stepped", "base": 120, "wash": 60, "test": 180, "grid": [(6.5, 1.0), (6.5, 1.5), (6.0, 1.0), (6.0, 1.5), (6.0, 2.0), (5.5, 1.0), (5.5, 1.5), (5.5, 2.0), (5.0, 1.0), (5.0, 1.5), (5.0, 2.0), (4.5, 1.0), (4.5, 1.5)]},
            self.TARGETED_KEY: {"type": "stepped"} 
        }
        
        self.m_in, self.m_hi, self.m_ex, self.m_he = 4.0, 0.0, 6.0, 0.0
        self.baseline_metrics = {}
        self.leaderboard = []
        self._epoch_rr, self._epoch_ts = [], []
        
        self.history = []
        self.historical_optimal = None
        self._load_history()

    def _load_history(self):
        if os.path.exists(_HISTORY_FILE):
            try:
                with open(_HISTORY_FILE, "r") as f:
                    self.history = json.load(f)
                    if self.history: self.historical_optimal = self.history[-1].get("best", None)
            except Exception as e: logger.error(f"Failed to load RF history: {e}")

    def _save_history(self, best_node, leaderboard):
        self.history.append({"timestamp": time.time(), "best": best_node, "leaderboard": leaderboard})
        try:
            with open(_HISTORY_FILE, "w") as f: json.dump(self.history, f, indent=4)
            self.historical_optimal = best_node
        except Exception as e: logger.error(f"Failed to save RF history: {e}")

    def build(self, parent: str) -> None:
        if self._built: return

        # Register a medium font for the info labels if not already registered
        if not dpg.does_item_exist("rb_medium_font"):
            with dpg.font_registry():
                dpg.add_font(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    size=22,
                    tag="rb_medium_font"
                )

        with dpg.tree_node(label="Resonance Frequency Assessment", parent=parent, default_open=True, tag="rb_node"):
            dpg.add_text("Resonance Frequency Assessment", color=(100, 220, 180))
            dpg.add_text("⚠️ HR Device Required (Connect Polar H10 or PVS)", tag="rb_hr_warning_text", color=(255, 50, 50))
            
            if self.historical_optimal:
                b = self.historical_optimal
                hist_text = f"Historical Optimal: {b['bpm']} BPM (1:{b.get('ratio', 'Cont')}) - Score: {b['score']:.1f}"
                dpg.add_text(hist_text, tag="rb_history_display", color=(255, 215, 0))
            else:
                dpg.add_text("Historical Optimal: None (Run a Global Sweep)", tag="rb_history_display", color=(150, 150, 150))
                
            dpg.add_separator()
            
            with dpg.tab_bar():
                with dpg.tab(label="Virtual Clinician (Assessment)"):
                    dpg.add_spacer(height=10)
                    with dpg.group(horizontal=True):
                        dpg.add_text("Protocol Selection:")
                        dpg.add_combo(list(self.presets.keys()), default_value=self.CONTINUOUS_KEY, width=350, tag="rb_preset_combo")
                    dpg.add_spacer(height=10)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="START PROTOCOL", tag="rb_start_assess_btn", callback=self._start_assessment, width=200, height=40, enabled=False)
                        dpg.add_button(label="CANCEL", tag="rb_stop_assess_btn", callback=self._stop_assessment, width=100, height=40, show=False)
                    dpg.add_spacer(height=10)
                    with dpg.group(tag="rb_status_group", show=True):
                        with dpg.group(horizontal=True):
                            dpg.add_text("STATUS: ")
                            dpg.add_text("SYSTEM IDLE", tag="rb_state_text", color=(255, 200, 0))
                        with dpg.group(horizontal=True):
                            dpg.add_text("TOTAL PROGRESS: ")
                            dpg.add_text("00:00 / 00:00", tag="rb_total_time_text", color=(100, 200, 255))
                        dpg.add_text("Waiting to begin...", tag="rb_instruction_text", color=(200, 200, 200))
                        dpg.add_progress_bar(tag="rb_progress_bar", default_value=0.0, width=_PACER_W)
                    dpg.add_spacer(height=10)

                with dpg.tab(label="Manual Breathing"):
                    dpg.add_spacer(height=10)
                    with dpg.group(horizontal=True):
                        dpg.add_input_float(label="Inhale", tag="rb_m_in", default_value=4.0, step=0.5, width=120, callback=self._update_manual_pacer)
                        dpg.add_input_float(label="Hold", tag="rb_m_hi", default_value=0.0, step=0.5, width=120, callback=self._update_manual_pacer)
                        dpg.add_input_float(label="Exhale", tag="rb_m_ex", default_value=6.0, step=0.5, width=120, callback=self._update_manual_pacer)
                        dpg.add_input_float(label="Hold", tag="rb_m_he", default_value=0.0, step=0.5, width=120, callback=self._update_manual_pacer)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Start Manual", tag="rb_manual_start_btn", callback=self._toggle_manual, width=120, enabled=False)
                        dpg.add_text("Coherence Score: ")
                        dpg.add_text("0.0", tag="rb_manual_coherence", color=(0, 255, 0))
                
                with dpg.tab(label="Assessment Leaderboard", tag="rb_leaderboard_tab"):
                    dpg.add_spacer(height=10)
                    with dpg.table(header_row=True, tag="rb_leaderboard_table", borders_innerH=True, borders_outerH=True):
                        dpg.add_table_column(label="Rank")
                        dpg.add_table_column(label="BPM")
                        dpg.add_table_column(label="Protocol/Ratio")
                        dpg.add_table_column(label="Score")
                        dpg.add_table_column(label="Phase/PLV")
                        dpg.add_table_column(label="PT Amp")
                        dpg.add_table_column(label="LFnu")

            dpg.add_separator()
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True, tag="rb_timing_group", show=False):
                with dpg.group():
                    dpg.add_text("INHALE", color=(150, 150, 200))
                    dpg.add_text("--s", tag="rb_inhale_sec_text", color=(100, 220, 255))
                    dpg.bind_item_font("rb_inhale_sec_text", "rb_medium_font")
                dpg.add_spacer(width=40)
                with dpg.group():
                    dpg.add_text("EXHALE", color=(150, 150, 200))
                    dpg.add_text("--s", tag="rb_exhale_sec_text", color=(100, 220, 255))
                    dpg.bind_item_font("rb_exhale_sec_text", "rb_medium_font")
                dpg.add_spacer(width=40)
                with dpg.group():
                    dpg.add_text("RATE", color=(150, 150, 200))
                    dpg.add_text("-- BPM", tag="rb_bpm_text", color=(200, 200, 255))
                    dpg.bind_item_font("rb_bpm_text", "rb_medium_font")
                dpg.add_spacer(width=40)
                with dpg.group():
                    dpg.add_text("RATIO", color=(150, 150, 200))
                    dpg.add_text("1:--", tag="rb_ratio_text", color=(200, 200, 255))
                    dpg.bind_item_font("rb_ratio_text", "rb_medium_font")
                dpg.add_spacer(width=40)
                with dpg.group():
                    dpg.add_text("TIME LEFT", color=(150, 150, 200))
                    dpg.add_text("--:--", tag="rb_time_left_text", color=(255, 220, 100))
                    dpg.bind_item_font("rb_time_left_text", "rb_medium_font")
            dpg.add_text("", tag="rb_pacer_timing_text", show=False)
            with dpg.drawlist(width=_PACER_W, height=_PACER_H, tag="rb_pacer_drawlist", show=False): pass 

        self._built = True
        self.set_hr_status(self._hr_connected) 
        if self.history and self.history[-1].get("leaderboard"):
            self.leaderboard = self.history[-1]["leaderboard"]
            self._update_leaderboard_ui()

    # -------------------------------------------------------------------------
    # HARDWARE & DATA INGESTION
    # -------------------------------------------------------------------------
    def set_hr_status(self, is_connected: bool):
        self._hr_connected = is_connected
        if not self._built: return
        if not is_connected:
            dpg.set_value("rb_hr_warning_text", "⚠️ HR Device Required (Connect Polar H10 or Verity Sense)")
            dpg.configure_item("rb_start_assess_btn", enabled=False)
            dpg.configure_item("rb_manual_start_btn", enabled=False)
            if self.state not in [self.STATE_IDLE, self.STATE_COMPLETE, "MANUAL_ACTIVE"]:
                self._stop_assessment()
                self._update_hud("ABORTED", "HR Device Disconnected.", 0.0, (255, 0, 0))
            elif self.state == "MANUAL_ACTIVE":
                self._toggle_manual() 
        else:
            dpg.set_value("rb_hr_warning_text", "")
            if self.state in [self.STATE_IDLE, self.STATE_COMPLETE]:
                dpg.configure_item("rb_start_assess_btn", enabled=True)
                dpg.configure_item("rb_manual_start_btn", enabled=True)

    def feed_rr(self, rr_ms: float):
        """Called by ui_manager for every heartbeat to fill our data arrays."""
        if not self._built: return
        if self.state in [self.STATE_BASELINE, self.STATE_TESTING, self.STATE_TESTING_CONT]:
            ts = time.time() - self._epoch_start_time
            self._epoch_rr.append(rr_ms)
            self._epoch_ts.append(ts)

    def update_resonance_score(self, score: float) -> None:
        """MISSING METHOD RESTORED: Satisfies ui_manager.py payload update"""
        self._latest_coherence = score
        if self._built and dpg.does_item_exist("rb_manual_coherence"):
            dpg.set_value("rb_manual_coherence", f"{score:.1f}")

    # -------------------------------------------------------------------------
    # UI DRAWING
    # -------------------------------------------------------------------------
    def _draw_horizontal_pacer(self, phase_rad: float, phase_text: str):
        dpg.delete_item("rb_pacer_drawlist", children_only=True)
        mod_phase = phase_rad % (2 * np.pi)
        
        if phase_text == "MANUAL":
            t_cycle = phase_rad
            i, hi, e = self.m_in, self.m_hi, self.m_ex
            if t_cycle < i: txt, vol, col = "INHALE", t_cycle / i, (0, 200, 255, 255) 
            elif t_cycle < i + hi: txt, vol, col = "HOLD FULL", 1.0, (0, 150, 200, 255) 
            elif t_cycle < i + hi + e: txt, vol, col = "EXHALE", 1.0 - ((t_cycle - i - hi) / e), (0, 200, 255, 255) 
            else: txt, vol, col = "HOLD EMPTY", 0.0, (80, 80, 80, 255)
        else:
            if mod_phase < np.pi: txt, vol, col = "INHALE", mod_phase / np.pi, (0, 200, 255, 255)
            else: txt, vol, col = "EXHALE", 1.0 - ((mod_phase - np.pi) / np.pi), (0, 200, 255, 255)

        dpg.draw_rectangle((0, 0), (_PACER_W, _PACER_H), color=(50, 50, 50), fill=(30, 30, 30), parent="rb_pacer_drawlist")
        dpg.draw_rectangle((0, 0), (_PACER_W * vol, _PACER_H), color=(0,0,0,0), fill=col, parent="rb_pacer_drawlist")
        dpg.draw_text((_PACER_W / 2 - 30, _PACER_H / 2 - 10), txt, size=20, color=(255, 255, 255), parent="rb_pacer_drawlist")

    # -------------------------------------------------------------------------
    # STATE MACHINE TICK
    # -------------------------------------------------------------------------
    def tick(self) -> None:
        if not self._built: return
        current_time = time.time()
        
        if self.state not in [self.STATE_IDLE, self.STATE_COMPLETE, "MANUAL_ACTIVE"]:
            tot = current_time - self.assessment_start_time
            dpg.set_value("rb_total_time_text", f"{self._fmt_time(tot)} / {self._fmt_time(self.total_assessment_duration)}")

        if self.state == self.STATE_TESTING_CONT:
            dpg.configure_item("rb_pacer_drawlist", show=True)
            dpg.configure_item("rb_timing_group", show=True)
            
            bpm, phase, _ = self.continuous_pacer.evaluate(current_time - self.block_start_time)
            cur_bpm = bpm[0]
            cycle = 60.0 / cur_bpm
            i_sec = cycle / 2.0
            e_sec = cycle / 2.0
            rem = self.test_duration - (current_time - self.block_start_time)
            dpg.set_value("rb_inhale_sec_text", f"{i_sec:.1f}s")
            dpg.set_value("rb_exhale_sec_text", f"{e_sec:.1f}s")
            dpg.set_value("rb_bpm_text", f"{cur_bpm:.2f} BPM")
            dpg.set_value("rb_ratio_text", "1:1")
            dpg.set_value("rb_time_left_text", self._fmt_time(max(0, rem)))
            self._draw_horizontal_pacer(phase[0], "RADIAN")
            
        elif self.state == self.STATE_TESTING:
            dpg.configure_item("rb_pacer_drawlist", show=True)
            dpg.configure_item("rb_timing_group", show=True)
            bpm, ratio = self.assessment_grid[self.grid_index]
            cycle = 60.0 / bpm
            i_sec = cycle / (1.0 + ratio)
            e_sec = cycle - i_sec
            rem = self.test_duration - (current_time - self.block_start_time)
            
            t_cycle = (current_time - self.block_start_time) % cycle
            phase_rad = (t_cycle / cycle) * 2 * np.pi
            dpg.set_value("rb_inhale_sec_text", f"{i_sec:.1f}s")
            dpg.set_value("rb_exhale_sec_text", f"{e_sec:.1f}s")
            dpg.set_value("rb_bpm_text", f"{bpm:.1f} BPM")
            dpg.set_value("rb_ratio_text", f"1:{ratio}")
            dpg.set_value("rb_time_left_text", self._fmt_time(max(0, rem)))
            self._draw_horizontal_pacer(phase_rad, "RADIAN")
            
        elif self.state == "MANUAL_ACTIVE":
            dpg.configure_item("rb_pacer_drawlist", show=True)
            dpg.configure_item("rb_timing_group", show=True)
            c = self.m_in + self.m_hi + self.m_ex + self.m_he
            dpg.set_value("rb_inhale_sec_text", f"{self.m_in:.1f}s")
            dpg.set_value("rb_exhale_sec_text", f"{self.m_ex:.1f}s")
            if c > 0:
                bpm_manual = 60.0 / c
                dpg.set_value("rb_bpm_text", f"{bpm_manual:.1f} BPM")
            else:
                dpg.set_value("rb_bpm_text", "-- BPM")
            ratio_manual = round(self.m_ex / self.m_in, 1) if self.m_in > 0 else "--"
            dpg.set_value("rb_ratio_text", f"1:{ratio_manual}")
            dpg.set_value("rb_time_left_text", "--:--")
            if c > 0: self._draw_horizontal_pacer((current_time - self.block_start_time) % c, "MANUAL")
        else:
            dpg.configure_item("rb_pacer_drawlist", show=False)
            dpg.configure_item("rb_timing_group", show=False)

        if self.state not in [self.STATE_IDLE, self.STATE_COMPLETE, "MANUAL_ACTIVE"]:
            block_elapsed = current_time - self.block_start_time
            
            if self.state == self.STATE_BASELINE:
                rem = self.baseline_duration - block_elapsed
                self._update_hud("STAGE 1: BASELINE", f"Breathe normally: {int(rem)}s", block_elapsed / self.baseline_duration, (0, 150, 255))
                if block_elapsed >= self.baseline_duration: self._process_baseline()
            
            elif self.state == self.STATE_TESTING_CONT:
                rem = self.test_duration - block_elapsed
                self._update_hud("SLIDING PROTOCOL ACTIVE", f"Follow the wave. Time: {int(rem)}s", block_elapsed / self.test_duration, (0, 255, 100))
                if block_elapsed >= self.test_duration: self._finish_continuous_assessment()
            
            elif self.state == self.STATE_TESTING:
                rem = self.test_duration - block_elapsed
                bpm, ratio = self.assessment_grid[self.grid_index]
                self._update_hud(f"TESTING: {bpm} BPM (1:{ratio})", f"Follow the bar: {int(rem)}s", block_elapsed / self.test_duration, (0, 255, 100))
                if block_elapsed >= self.test_duration: self._process_testing_block(bpm, ratio)
            
            elif self.state == self.STATE_WASHOUT:
                rem = self.washout_duration - block_elapsed
                self._update_hud("WASHOUT / REST", f"Breathe normally: {int(rem)}s", block_elapsed / self.washout_duration, (255, 150, 0))
                if block_elapsed >= self.washout_duration:
                    self.grid_index += 1
                    if self.grid_index >= len(self.assessment_grid): self._finish_stepped_assessment()
                    else: self._start_testing_block()

    # -------------------------------------------------------------------------
    # PROTOCOL LOGIC
    # -------------------------------------------------------------------------
    def _start_assessment(self, sender=None, app_data=None):
        if not self._hr_connected: return
        preset_key = dpg.get_value("rb_preset_combo")
        config = self.presets[preset_key]
        self.active_protocol_type = config["type"]
        
        if self.active_protocol_type == "continuous":
            self.continuous_pacer = ContinuousPacer()
            self.baseline_duration = 120.0
            self.test_duration = self.continuous_pacer.total_duration
            self.total_assessment_duration = self.baseline_duration + self.test_duration
        else:
            if preset_key == self.TARGETED_KEY:
                if not self.historical_optimal:
                    self._update_hud("ERROR", "No historical data found. Run a Global Sweep.", 0.0, (255, 50, 50))
                    return
                bpm, rat = self.historical_optimal['bpm'], self.historical_optimal.get('ratio', 1.0)
                if rat == "Cont": rat = 1.0
                self.assessment_grid = [(bpm, rat), (round(bpm + 0.2, 1), rat), (round(bpm - 0.2, 1), rat)]
                self.baseline_duration, self.washout_duration, self.test_duration = 120.0, 60.0, 120.0
            else:
                self.baseline_duration, self.washout_duration, self.test_duration = config["base"], config["wash"], config["test"]
                self.assessment_grid = config["grid"]
            
            num = len(self.assessment_grid)
            self.total_assessment_duration = self.baseline_duration + (num * self.test_duration) + ((num - 1) * self.washout_duration)
        
        self.leaderboard.clear()
        self._clear_leaderboard_ui()
        dpg.configure_item("rb_start_assess_btn", show=False)
        dpg.configure_item("rb_stop_assess_btn", show=True)
        dpg.configure_item("rb_preset_combo", enabled=False)
        dpg.configure_item("rb_manual_start_btn", enabled=False)
        
        self.state = self.STATE_BASELINE
        self.assessment_start_time = self.block_start_time = self._epoch_start_time = time.time()
        self._epoch_rr.clear()
        self._epoch_ts.clear()

    def _stop_assessment(self, sender=None, app_data=None):
        self.state = self.STATE_IDLE
        dpg.configure_item("rb_start_assess_btn", show=True)
        dpg.configure_item("rb_stop_assess_btn", show=False)
        dpg.configure_item("rb_preset_combo", enabled=True)
        dpg.configure_item("rb_manual_start_btn", enabled=self._hr_connected)
        self._update_hud("SYSTEM IDLE", "Assessment Cancelled.", 0.0, (255, 50, 50))
        dpg.set_value("rb_total_time_text", "00:00 / 00:00")

    def _process_baseline(self):
        rmssd = self.math.calculate_rmssd(self._epoch_rr)
        lf_abs, lf_nu = self.math.calculate_spectral_power(self._epoch_rr, self._epoch_ts)
        self.baseline_metrics = {'rmssd': rmssd, 'lf_power': lf_nu, 'pt_amp': 1.0}
        
        self.grid_index = 0
        self.state = self.STATE_TESTING_CONT if self.active_protocol_type == "continuous" else self.STATE_TESTING
        self.block_start_time = self._epoch_start_time = time.time()
        self._epoch_rr.clear()
        self._epoch_ts.clear()

    def _start_testing_block(self):
        self.state = self.STATE_TESTING
        self.block_start_time = self._epoch_start_time = time.time()
        self._epoch_rr.clear()
        self._epoch_ts.clear()

    def _process_testing_block(self, bpm, ratio):
        cycle = 60.0 / bpm
        breath_cycles = [(t, t + cycle) for t in np.arange(0, self.test_duration, cycle)]
            
        rmssd = self.math.calculate_rmssd(self._epoch_rr)
        lf_abs, lf_nu = self.math.calculate_spectral_power(self._epoch_rr, self._epoch_ts)
        pt = self.math.calculate_pt_amplitude(self._epoch_rr, self._epoch_ts, breath_cycles)
        phase = self.math.calculate_phase_synchrony(self._epoch_rr, self._epoch_ts, cycle/(1+ratio), cycle - (cycle/(1+ratio)))
        
        score = self.math.score_epoch(rmssd, pt, lf_nu, phase, self.baseline_metrics)
        self.leaderboard.append({'bpm': bpm, 'ratio': ratio, 'score': score, 'rmssd': rmssd, 'lf': lf_nu, 'pt': pt, 'phase': phase})
        self._update_leaderboard_ui()
        
        if self.grid_index == len(self.assessment_grid) - 1: self._finish_stepped_assessment()
        else:
            self.state = self.STATE_WASHOUT
            self.block_start_time = time.time()

    def _finish_continuous_assessment(self):
        rr, ts = np.array(self._epoch_rr), np.array(self._epoch_ts)
        if len(rr) > 60:
            csm = ContinuousSlidingMath()
            time_grid, resampled_rr, lf_power = csm.compute_continuous_lf_power(ts, rr)
            bpm_arr, _, ref_wave = self.continuous_pacer.evaluate(time_grid)
            pt_amp, plv = csm.calculate_rolling_metrics(resampled_rr, ref_wave)
            best_bpm, best_score = csm.extract_resonance_frequency(time_grid, lf_power, pt_amp, plv, bpm_arr)
            
            self.leaderboard.append({'bpm': round(best_bpm, 1), 'ratio': "Cont", 'score': best_score, 'rmssd': 0.0, 'lf': 0.0, 'pt': 0.0, 'phase': 0.0})
            self._update_leaderboard_ui()
        self._finalize_and_save()

    def _finish_stepped_assessment(self):
        self._finalize_and_save()

    def _finalize_and_save(self):
        self.state = self.STATE_COMPLETE
        dpg.configure_item("rb_start_assess_btn", show=True)
        dpg.configure_item("rb_stop_assess_btn", show=False)
        dpg.configure_item("rb_preset_combo", enabled=True)
        dpg.configure_item("rb_manual_start_btn", enabled=self._hr_connected)
        
        if self.leaderboard:
            best = sorted(self.leaderboard, key=lambda x: x['score'], reverse=True)[0]
            self._save_history(best, sorted(self.leaderboard, key=lambda x: x['score'], reverse=True))
            self._update_hud("ASSESSMENT COMPLETE", f"OPTIMAL FOUND: {best['bpm']} BPM", 1.0, (0, 255, 0))
            dpg.set_value("rb_total_time_text", "COMPLETE")
            dpg.set_value("rb_history_display", f"Historical Optimal: {best['bpm']} BPM - Score: {best['score']:.1f}")
        else:
            self._update_hud("ASSESSMENT COMPLETE", "No data gathered.", 1.0, (150, 150, 150))

    def _fmt_time(self, seconds: float) -> str: return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"

    def _update_hud(self, title, instruction, progress, color):
        dpg.set_value("rb_state_text", title)
        dpg.configure_item("rb_state_text", color=color)
        dpg.set_value("rb_instruction_text", instruction)
        dpg.set_value("rb_progress_bar", progress)

    def _clear_leaderboard_ui(self):
        if children := dpg.get_item_children("rb_leaderboard_table", 1):
            for child in children: dpg.delete_item(child)

    def _update_leaderboard_ui(self):
        self._clear_leaderboard_ui()
        for rank, data in enumerate(sorted(self.leaderboard, key=lambda x: x['score'], reverse=True), 1):
            with dpg.table_row(parent="rb_leaderboard_table"):
                dpg.add_text(f"#{rank}")
                dpg.add_text(f"{data['bpm']:.1f}")
                dpg.add_text(str(data['ratio']))
                dpg.add_text(f"{data['score']:.1f}", color=(255, 215, 0) if rank == 1 else (255, 255, 255))
                dpg.add_text(f"{data.get('phase', 0.0)*100:.1f}%")
                dpg.add_text(f"{data.get('pt', 0.0):.1f}")
                dpg.add_text(f"{data.get('lf', 0.0):.1f}%")

    def _update_manual_pacer(self, sender, app_data):
        self.m_in, self.m_hi, self.m_ex, self.m_he = (dpg.get_value(t) for t in ["rb_m_in", "rb_m_hi", "rb_m_ex", "rb_m_he"])

    def _toggle_manual(self, sender, app_data):
        if not self._hr_connected: return
        if self.state in [self.STATE_IDLE, self.STATE_COMPLETE]:
            self.state = "MANUAL_ACTIVE"
            self.block_start_time = time.time()
            dpg.configure_item("rb_manual_start_btn", label="Stop Manual")
            dpg.configure_item("rb_start_assess_btn", enabled=False)
        elif self.state == "MANUAL_ACTIVE":
            self.state = self.STATE_IDLE
            dpg.configure_item("rb_manual_start_btn", label="Start Manual")
            dpg.configure_item("rb_start_assess_btn", enabled=True)
            self._update_hud("SYSTEM IDLE", "Manual Session Ended.", 0.0, (255, 200, 0))