"""
Resonance Breathing App & Automated RF Assessment System.

Provides an automated "Virtual Clinician" that runs Global Sweeps (Grid Search) 
and Targeted Micro-Adjustments (Hill Climbing) to determine the user's optimal 
Resonance Frequency via weighted physiological scoring.
Includes a dynamic horizontal pacer, persistent JSON history, and customizable test durations.
"""
import time
import logging
import json
import os
import numpy as np
from scipy.signal import lombscargle
import dearpygui.dearpygui as dpg
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

_PACER_W = 850
_PACER_H = 60
_HISTORY_FILE = "rf_history.json"

# -------------------------------------------------------------------------
# MATHEMATICAL SIGNAL PROCESSING ENGINE
# -------------------------------------------------------------------------
class PhysiologicalMath:
    """Calculates HRV metrics from unevenly sampled R-R intervals."""
    
    @staticmethod
    def calculate_rmssd(rr_intervals: List[float]) -> float:
        if len(rr_intervals) < 2:
            return 0.0
        rr_array = np.array(rr_intervals)
        successive_diffs = np.diff(rr_array)
        return float(np.sqrt(np.mean(successive_diffs ** 2)))

    @staticmethod
    def calculate_pt_amplitude(rr_intervals: List[float], timestamps: List[float], breath_cycles: List[Tuple[float, float]]) -> float:
        if not rr_intervals or not breath_cycles:
            return 0.0
            
        rr_array = np.array(rr_intervals)
        ts_array = np.array(timestamps)
        hr_array = 60000.0 / rr_array  # Convert RR (ms) to HR (BPM)
        
        pt_amplitudes = []
        for start, end in breath_cycles:
            mask = (ts_array >= start) & (ts_array <= end)
            cycle_hr = hr_array[mask]
            if len(cycle_hr) > 0:
                pt_amplitudes.append(np.max(cycle_hr) - np.min(cycle_hr))
                
        return float(np.mean(pt_amplitudes)) if pt_amplitudes else 0.0

    @staticmethod
    def calculate_lf_power(rr_intervals: List[float], timestamps: List[float]) -> float:
        if len(rr_intervals) < 10:
            return 0.0
            
        frequencies = np.linspace(0.04, 0.15, 1000)
        angular_freqs = 2 * np.pi * frequencies
        
        rr_array = np.array(rr_intervals)
        rr_centered = rr_array - np.mean(rr_array)
        ts_array = np.array(timestamps)
        
        try:
            power = lombscargle(ts_array, rr_centered, angular_freqs)
        except TypeError:
            power = lombscargle(ts_array, rr_centered, angular_freqs)
            
        lf_power = np.trapz(power, frequencies)
        return float(lf_power)
        
    @staticmethod
    def score_epoch(rmssd: float, pt_amp: float, lf_power: float, baseline: Dict) -> float:
        b_rmssd = baseline.get('rmssd', 1.0) or 1.0
        b_pt = baseline.get('pt_amp', 1.0) or 1.0
        b_lf = baseline.get('lf_power', 1.0) or 1.0

        norm_rmssd = min(rmssd / b_rmssd, 5.0) 
        norm_pt = min(pt_amp / b_pt, 5.0)
        norm_lf = min(lf_power / b_lf, 10.0)

        score = (norm_pt * 0.40) + (norm_lf * 0.30) + (norm_rmssd * 0.30)
        return round(score * 100, 2)

# -------------------------------------------------------------------------
# MAIN WIDGET CONTROLLER
# -------------------------------------------------------------------------
class ResonanceBreathingWidget:
    
    STATE_IDLE = "IDLE"
    STATE_BASELINE = "BASELINE"
    STATE_TESTING = "TESTING"
    STATE_WASHOUT = "WASHOUT"
    STATE_COMPLETE = "COMPLETE"

    def __init__(self, db=None):
        self.db = db
        self._built = False
        self._hr_connected = False
        
        self.math = PhysiologicalMath()
        
        # State Machine Variables
        self.state = self.STATE_IDLE
        self.assessment_start_time = 0.0
        self.block_start_time = 0.0
        self.grid_index = 0
        self.total_assessment_duration = 0.0
        
        self.baseline_duration = 120.0
        self.washout_duration = 60.0
        self.test_duration = 180.0
        self.assessment_grid = []
        
        # Presets Data
        self.TARGETED_KEY = "Targeted Micro-Adjustment (Requires History, ~10 mins)"
        self.presets = {
            "Express Sweep (Fresh, ~8 mins)": {
                "base": 60, "wash": 30, "test": 60,
                "grid": [(6.5, 1.0), (6.0, 1.0), (5.5, 1.0), (5.0, 1.0), (4.5, 1.0)]
            },
            "Standard Sweep (Fresh, ~18 mins)": {
                "base": 120, "wash": 60, "test": 120,
                "grid": [(6.5, 1.0), (6.0, 1.0), (5.5, 1.0), (5.0, 1.0), (4.5, 1.0)]
            },
            "Deep Calibration Sweep (Fresh, ~42 mins)": {
                "base": 120, "wash": 60, "test": 180,
                "grid": [
                    (6.5, 1.0), (6.5, 1.5), (6.0, 1.0), (6.0, 1.5),
                    (5.5, 1.0), (5.5, 1.5), (5.0, 1.0), (5.0, 1.5),
                    (4.5, 1.0), (4.5, 1.5)
                ]
            },
            self.TARGETED_KEY: {} # Grid dynamically built at runtime
        }
        
        self.m_in, self.m_hi, self.m_ex, self.m_he = 4.0, 0.0, 6.0, 0.0
        
        self.baseline_metrics = {}
        self.leaderboard = []
        self._epoch_rr = []
        self._epoch_ts = []
        self._epoch_start_time = 0.0
        self._latest_coherence = 0.0
        
        # Persistence
        self.history = []
        self.historical_optimal = None
        self._load_history()

    def _load_history(self):
        if os.path.exists(_HISTORY_FILE):
            try:
                with open(_HISTORY_FILE, "r") as f:
                    self.history = json.load(f)
                    if self.history:
                        # Grab the best result from the most recent run
                        self.historical_optimal = self.history[-1].get("best", None)
            except Exception as e:
                logger.error(f"Failed to load RF history: {e}")

    def _save_history(self, best_node, leaderboard):
        entry = {
            "timestamp": time.time(),
            "best": best_node,
            "leaderboard": leaderboard
        }
        self.history.append(entry)
        try:
            with open(_HISTORY_FILE, "w") as f:
                json.dump(self.history, f, indent=4)
            self.historical_optimal = best_node
        except Exception as e:
            logger.error(f"Failed to save RF history: {e}")

    def build(self, parent: str) -> None:
        if self._built: return

        with dpg.tree_node(label="Resonance Frequency Assessment", parent=parent, default_open=True, tag="rb_node"):
            
            dpg.add_text("Resonance Frequency Assessment", color=(100, 220, 180))
            dpg.add_text("⚠️ HR Device Required (Connect Polar H10 or PVS)", tag="rb_hr_warning_text", color=(255, 50, 50))
            
            # Historical Data Display
            if self.historical_optimal:
                b = self.historical_optimal
                hist_text = f"Historical Optimal: {b['bpm']} BPM (1:{b['ratio']}) - Score: {b['score']:.1f}"
                dpg.add_text(hist_text, tag="rb_history_display", color=(255, 215, 0))
            else:
                dpg.add_text("Historical Optimal: None (Run a Global Sweep)", tag="rb_history_display", color=(150, 150, 150))
                
            dpg.add_separator()
            
            with dpg.tab_bar():
                # --- TAB 1: ASSESSMENT PROTOCOLS ---
                with dpg.tab(label="Virtual Clinician (Assessment)"):
                    dpg.add_spacer(height=10)
                    
                    with dpg.group(horizontal=True):
                        dpg.add_text("Protocol Selection:")
                        dpg.add_combo(list(self.presets.keys()), default_value="Express Sweep (Fresh, ~8 mins)", 
                                      width=350, tag="rb_preset_combo")
                    
                    dpg.add_spacer(height=10)
                    
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="START PROTOCOL", tag="rb_start_assess_btn", callback=self._start_assessment, width=200, height=40, enabled=False)
                        dpg.add_button(label="CANCEL", tag="rb_stop_assess_btn", callback=self._stop_assessment, width=100, height=40, show=False)
                    
                    dpg.add_spacer(height=10)
                    # Status HUD
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

                # --- TAB 2: MANUAL PACING ---
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
                
                # --- TAB 3: LEADERBOARD ---
                with dpg.tab(label="Assessment Leaderboard", tag="rb_leaderboard_tab"):
                    dpg.add_spacer(height=10)
                    with dpg.table(header_row=True, tag="rb_leaderboard_table", borders_innerH=True, borders_outerH=True):
                        dpg.add_table_column(label="Rank")
                        dpg.add_table_column(label="BPM")
                        dpg.add_table_column(label="I:E Ratio")
                        dpg.add_table_column(label="Final Score")
                        dpg.add_table_column(label="PT Amp (ms)")
                        dpg.add_table_column(label="LF Power")
                        dpg.add_table_column(label="RMSSD (ms)")

            # --- DYNAMIC HORIZONTAL PACER BAR ---
            dpg.add_separator()
            dpg.add_spacer(height=10)
            
            dpg.add_text("Timing: -- | --", tag="rb_pacer_timing_text", show=False, color=(200, 200, 255))
            with dpg.drawlist(width=_PACER_W, height=_PACER_H, tag="rb_pacer_drawlist", show=False):
                pass 

        self._built = True
        self.set_hr_status(self._hr_connected) 
        
        # Pre-populate leaderboard from history if it exists
        if self.history and self.history[-1].get("leaderboard"):
            self.leaderboard = self.history[-1]["leaderboard"]
            self._update_leaderboard_ui()

    # -------------------------------------------------------------------------
    # HARDWARE CONNECTION LOCK
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

    # -------------------------------------------------------------------------
    # DATA PIPELINE
    # -------------------------------------------------------------------------
    def feed_rr(self, rr_ms: float):
        if not self._built: return
        if self.state in [self.STATE_BASELINE, self.STATE_TESTING]:
            ts = time.time() - self._epoch_start_time
            self._epoch_rr.append(rr_ms)
            self._epoch_ts.append(ts)

    def update_resonance_score(self, score: float) -> None:
        self._latest_coherence = score
        if self._built and dpg.does_item_exist("rb_manual_coherence"):
            dpg.set_value("rb_manual_coherence", f"{score:.1f}")

    # -------------------------------------------------------------------------
    # UI DRAWING LOGIC (HORIZONTAL PACER)
    # -------------------------------------------------------------------------
    def _draw_horizontal_pacer(self, t_cycle: float, i_sec: float, hi_sec: float, e_sec: float, he_sec: float):
        dpg.delete_item("rb_pacer_drawlist", children_only=True)
        
        if t_cycle < i_sec:
            phase_text = "INHALE"
            volume = t_cycle / i_sec
            fill_col = (0, 200, 255, 255) 
        elif t_cycle < i_sec + hi_sec:
            phase_text = "HOLD FULL"
            volume = 1.0
            fill_col = (0, 150, 200, 255) 
        elif t_cycle < i_sec + hi_sec + e_sec:
            phase_text = "EXHALE"
            t_ex = t_cycle - i_sec - hi_sec
            volume = 1.0 - (t_ex / e_sec)
            fill_col = (0, 200, 255, 255) 
        else:
            phase_text = "HOLD EMPTY"
            volume = 0.0
            fill_col = (80, 80, 80, 255)

        dpg.draw_rectangle((0, 0), (_PACER_W, _PACER_H), color=(50, 50, 50), fill=(30, 30, 30), parent="rb_pacer_drawlist")
        dpg.draw_rectangle((0, 0), (_PACER_W * volume, _PACER_H), color=(0,0,0,0), fill=fill_col, parent="rb_pacer_drawlist")
        dpg.draw_text((_PACER_W / 2 - 30, _PACER_H / 2 - 10), phase_text, size=20, color=(255, 255, 255), parent="rb_pacer_drawlist")

    # -------------------------------------------------------------------------
    # ENGINE TICK (STATE MACHINE)
    # -------------------------------------------------------------------------
    def tick(self) -> None:
        if not self._built: return
        
        current_time = time.time()
        
        if self.state not in [self.STATE_IDLE, self.STATE_COMPLETE, "MANUAL_ACTIVE"]:
            tot_elapsed = current_time - self.assessment_start_time
            dpg.set_value("rb_total_time_text", f"{self._fmt_time(tot_elapsed)} / {self._fmt_time(self.total_assessment_duration)}")

        if self.state == self.STATE_TESTING:
            dpg.configure_item("rb_pacer_drawlist", show=True)
            dpg.configure_item("rb_pacer_timing_text", show=True)
            bpm, ratio = self.assessment_grid[self.grid_index]
            cycle = 60.0 / bpm
            i_sec = cycle / (1.0 + ratio)
            e_sec = cycle - i_sec
            dpg.set_value("rb_pacer_timing_text", f"Timing: Inhale {i_sec:.1f}s | Exhale {e_sec:.1f}s")
            
            t_cycle = (current_time - self.block_start_time) % cycle
            self._draw_horizontal_pacer(t_cycle, i_sec, 0.0, e_sec, 0.0)
            
        elif self.state == "MANUAL_ACTIVE":
            dpg.configure_item("rb_pacer_drawlist", show=True)
            dpg.configure_item("rb_pacer_timing_text", show=True)
            dpg.set_value("rb_pacer_timing_text", f"Timing: Inhale {self.m_in:.1f}s | Hold {self.m_hi:.1f}s | Exhale {self.m_ex:.1f}s | Hold {self.m_he:.1f}s")
            
            cycle = self.m_in + self.m_hi + self.m_ex + self.m_he
            if cycle > 0:
                t_cycle = (current_time - self.block_start_time) % cycle
                self._draw_horizontal_pacer(t_cycle, self.m_in, self.m_hi, self.m_ex, self.m_he)
        else:
            dpg.configure_item("rb_pacer_drawlist", show=False)
            dpg.configure_item("rb_pacer_timing_text", show=False)

        if self.state != self.STATE_IDLE and self.state != self.STATE_COMPLETE and self.state != "MANUAL_ACTIVE":
            block_elapsed = current_time - self.block_start_time
            
            if self.state == self.STATE_BASELINE:
                rem = self.baseline_duration - block_elapsed
                self._update_hud("STAGE 1: BASELINE", f"Breathe normally. Time remaining: {int(rem)}s", block_elapsed / self.baseline_duration, (0, 150, 255))
                if block_elapsed >= self.baseline_duration:
                    self._process_baseline()
            
            elif self.state == self.STATE_TESTING:
                rem = self.test_duration - block_elapsed
                bpm, ratio = self.assessment_grid[self.grid_index]
                self._update_hud(f"TESTING: {bpm} BPM | Ratio 1:{ratio}", f"Follow the breathing bar. Time remaining: {int(rem)}s", block_elapsed / self.test_duration, (0, 255, 100))
                if block_elapsed >= self.test_duration:
                    self._process_testing_block(bpm, ratio)
            
            elif self.state == self.STATE_WASHOUT:
                rem = self.washout_duration - block_elapsed
                self._update_hud("WASHOUT / REST", f"Breathe normally. Clearing load: {int(rem)}s", block_elapsed / self.washout_duration, (255, 150, 0))
                if block_elapsed >= self.washout_duration:
                    self.grid_index += 1
                    if self.grid_index >= len(self.assessment_grid):
                        self._finish_assessment()
                    else:
                        self._start_testing_block()

    # -------------------------------------------------------------------------
    # ASSESSMENT PROTOCOL LOGIC
    # -------------------------------------------------------------------------
    def _start_assessment(self, sender=None, app_data=None):
        if not self._hr_connected: return
        
        preset_key = dpg.get_value("rb_preset_combo")
        
        # --- HILL CLIMBING LOGIC (Dynamic Grid generation based on past history) ---
        if preset_key == self.TARGETED_KEY:
            if not self.historical_optimal:
                self._update_hud("ERROR", "No historical data found. Run a Global Sweep first.", 0.0, (255, 50, 50))
                return
            
            bpm = self.historical_optimal['bpm']
            rat = self.historical_optimal['ratio']
            
            # Build a tight micro-grid around the historic optimal
            self.assessment_grid = [
                (bpm, rat),
                (round(bpm + 0.2, 1), rat),
                (round(bpm - 0.2, 1), rat)
            ]
            self.baseline_duration = 120.0
            self.washout_duration = 60.0
            self.test_duration = 120.0
            
        else:
            # --- GLOBAL SWEEP LOGIC (Fresh, Static Grids) ---
            preset = self.presets[preset_key]
            self.baseline_duration = preset["base"]
            self.washout_duration = preset["wash"]
            self.test_duration = preset["test"]
            self.assessment_grid = preset["grid"]
        
        num = len(self.assessment_grid)
        self.total_assessment_duration = self.baseline_duration + (num * self.test_duration) + ((num - 1) * self.washout_duration)
        
        self.leaderboard.clear()
        self._clear_leaderboard_ui()
        dpg.configure_item("rb_start_assess_btn", show=False)
        dpg.configure_item("rb_stop_assess_btn", show=True)
        dpg.configure_item("rb_preset_combo", enabled=False)
        dpg.configure_item("rb_manual_start_btn", enabled=False)
        
        self.state = self.STATE_BASELINE
        self.assessment_start_time = time.time()
        self.block_start_time = time.time()
        self._epoch_start_time = time.time()
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
        lf = self.math.calculate_lf_power(self._epoch_rr, self._epoch_ts)
        cycles = [(i, i+60) for i in range(0, int(self.baseline_duration), 60)]
        pt = self.math.calculate_pt_amplitude(self._epoch_rr, self._epoch_ts, cycles)
        
        self.baseline_metrics = {'rmssd': rmssd, 'lf_power': lf, 'pt_amp': pt}
        logger.info(f"Baseline established: RMSSD={rmssd:.1f}, LF={lf:.1f}, PT={pt:.1f}")
        
        self.grid_index = 0
        self._start_testing_block()

    def _start_testing_block(self):
        self.state = self.STATE_TESTING
        self.block_start_time = time.time()
        self._epoch_start_time = time.time()
        self._epoch_rr.clear()
        self._epoch_ts.clear()

    def _process_testing_block(self, bpm, ratio):
        cycle_duration = 60.0 / bpm
        breath_cycles = []
        t = 0.0
        while t + cycle_duration <= self.test_duration:
            breath_cycles.append((t, t + cycle_duration))
            t += cycle_duration
            
        rmssd = self.math.calculate_rmssd(self._epoch_rr)
        lf = self.math.calculate_lf_power(self._epoch_rr, self._epoch_ts)
        pt = self.math.calculate_pt_amplitude(self._epoch_rr, self._epoch_ts, breath_cycles)
        
        score = self.math.score_epoch(rmssd, pt, lf, self.baseline_metrics)
        
        result = {
            'bpm': bpm, 'ratio': ratio, 'score': score,
            'rmssd': rmssd, 'lf': lf, 'pt': pt
        }
        self.leaderboard.append(result)
        self._update_leaderboard_ui()
        
        if self.grid_index == len(self.assessment_grid) - 1:
            self.grid_index += 1
            self._finish_assessment()
        else:
            self.state = self.STATE_WASHOUT
            self.block_start_time = time.time()

    def _finish_assessment(self):
        self.state = self.STATE_COMPLETE
        dpg.configure_item("rb_start_assess_btn", show=True)
        dpg.configure_item("rb_stop_assess_btn", show=False)
        dpg.configure_item("rb_preset_combo", enabled=True)
        dpg.configure_item("rb_manual_start_btn", enabled=self._hr_connected)
        
        if self.leaderboard:
            # Sort the leaderboard mathematically to find the undisputed winner
            sorted_board = sorted(self.leaderboard, key=lambda x: x['score'], reverse=True)
            best = sorted_board[0]
            
            # Save to JSON History System
            self._save_history(best, sorted_board)
            
            # Update UI
            msg = f"OPTIMAL FOUND: {best['bpm']} BPM (Ratio 1:{best['ratio']})"
            self._update_hud("ASSESSMENT COMPLETE", msg, 1.0, (0, 255, 0))
            dpg.set_value("rb_total_time_text", "COMPLETE")
            dpg.set_value("rb_history_display", f"Historical Optimal: {best['bpm']} BPM (1:{best['ratio']}) - Score: {best['score']:.1f}")
            dpg.configure_item("rb_history_display", color=(255, 215, 0))
        else:
            self._update_hud("ASSESSMENT COMPLETE", "No data gathered.", 1.0, (150, 150, 150))

    # -------------------------------------------------------------------------
    # UI HELPER METHODS
    # -------------------------------------------------------------------------
    def _fmt_time(self, seconds: float) -> str:
        s = int(seconds)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _update_hud(self, title, instruction, progress, color):
        dpg.set_value("rb_state_text", title)
        dpg.configure_item("rb_state_text", color=color)
        dpg.set_value("rb_instruction_text", instruction)
        dpg.set_value("rb_progress_bar", progress)

    def _clear_leaderboard_ui(self):
        children = dpg.get_item_children("rb_leaderboard_table", 1)
        if children:
            for child in children:
                dpg.delete_item(child)

    def _update_leaderboard_ui(self):
        self._clear_leaderboard_ui()
        sorted_board = sorted(self.leaderboard, key=lambda x: x['score'], reverse=True)
        
        for rank, data in enumerate(sorted_board, 1):
            with dpg.table_row(parent="rb_leaderboard_table"):
                dpg.add_text(f"#{rank}")
                dpg.add_text(f"{data['bpm']:.1f}")
                dpg.add_text(f"1:{data['ratio']}")
                
                color = (255, 215, 0) if rank == 1 else (255, 255, 255)
                dpg.add_text(f"{data['score']:.1f}", color=color)
                
                dpg.add_text(f"{data['pt']:.1f}")
                dpg.add_text(f"{data['lf']:.1f}")
                dpg.add_text(f"{data['rmssd']:.1f}")

    def _update_manual_pacer(self, sender, app_data):
        self.m_in = dpg.get_value("rb_m_in")
        self.m_hi = dpg.get_value("rb_m_hi")
        self.m_ex = dpg.get_value("rb_m_ex")
        self.m_he = dpg.get_value("rb_m_he")

    def _toggle_manual(self, sender, app_data):
        if not self._hr_connected: return
        
        if self.state == self.STATE_IDLE or self.state == self.STATE_COMPLETE:
            self.state = "MANUAL_ACTIVE"
            self.block_start_time = time.time()
            dpg.configure_item("rb_manual_start_btn", label="Stop Manual")
            dpg.configure_item("rb_start_assess_btn", enabled=False)
        elif self.state == "MANUAL_ACTIVE":
            self.state = self.STATE_IDLE
            dpg.configure_item("rb_manual_start_btn", label="Start Manual")
            dpg.configure_item("rb_start_assess_btn", enabled=True)
            self._update_hud("SYSTEM IDLE", "Manual Session Ended.", 0.0, (255, 200, 0))