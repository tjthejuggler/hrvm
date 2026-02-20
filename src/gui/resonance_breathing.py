"""
Resonance Breathing App & Automated RF Assessment System.

Provides an automated "Virtual Clinician" that runs Global Sweeps (Grid Search) 
to determine the user's optimal Resonance Frequency via weighted physiological scoring 
(RMSSD, LF Power via Lomb-Scargle, and Peak-to-Trough Amplitude).
"""
import time
import logging
import numpy as np
from scipy.signal import lombscargle
import dearpygui.dearpygui as dpg
from typing import List, Dict, Optional, Tuple

from src.gui.pacer import PacerEngine

logger = logging.getLogger(__name__)

_PACER_W = 600
_PACER_H = 340

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
        """Calculates Peak-to-Trough (PT) amplitude (Max HR - Min HR) per breath cycle."""
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
        """Computes absolute Low-Frequency (LF) power using Lomb-Scargle periodogram."""
        if len(rr_intervals) < 10:
            return 0.0
            
        # LF band: 0.04 to 0.15 Hz
        frequencies = np.linspace(0.04, 0.15, 1000)
        angular_freqs = 2 * np.pi * frequencies
        
        rr_array = np.array(rr_intervals)
        rr_centered = rr_array - np.mean(rr_array)
        ts_array = np.array(timestamps)
        
        power = lombscargle(ts_array, rr_centered, angular_freqs, normalize=True)
        lf_power = np.trapz(power, frequencies)
        return float(lf_power)
        
    @staticmethod
    def score_epoch(rmssd: float, pt_amp: float, lf_power: float, baseline: Dict) -> float:
        """Calculates the final weighted score using the Multi-Metric matrix."""
        b_rmssd = baseline.get('rmssd', 1.0) or 1.0
        b_pt = baseline.get('pt_amp', 1.0) or 1.0
        b_lf = baseline.get('lf_power', 1.0) or 1.0

        # Normalize and cap at physiological maximums to prevent outliers from skewing
        norm_rmssd = min(rmssd / b_rmssd, 5.0) 
        norm_pt = min(pt_amp / b_pt, 5.0)
        norm_lf = min(lf_power / b_lf, 10.0)

        # 40% PT Amplitude, 30% LF Power, 30% RMSSD 
        score = (norm_pt * 0.40) + (norm_lf * 0.30) + (norm_rmssd * 0.30)
        return round(score * 100, 2)

# -------------------------------------------------------------------------
# MAIN WIDGET CONTROLLER
# -------------------------------------------------------------------------
class ResonanceBreathingWidget:
    
    # State Machine Constants
    STATE_IDLE = "IDLE"
    STATE_BASELINE = "BASELINE"
    STATE_TESTING = "TESTING"
    STATE_WASHOUT = "WASHOUT"
    STATE_COMPLETE = "COMPLETE"

    def __init__(self, db=None):
        self.db = db
        self._built = False
        self._hr_connected = False  # Hard lock: Requires HR source
        
        self.pacer = PacerEngine()
        self.math = PhysiologicalMath()
        
        # State Machine Variables
        self.state = self.STATE_IDLE
        self.block_start_time = 0.0
        self.grid_index = 0
        
        # Protocol Settings (Deep Calibration)
        self.baseline_duration = 120.0
        self.washout_duration = 60.0
        self.test_duration = 180.0
        
        # Grid: tuples of (BPM, Ratio)
        self.assessment_grid = [
            (6.5, 1.0), (6.5, 1.5),
            (6.0, 1.0), (6.0, 1.5),
            (5.5, 1.0), (5.5, 1.5),
            (5.0, 1.0), (5.0, 1.5),
            (4.5, 1.0), (4.5, 1.5)
        ]
        
        # Data Buffers
        self.baseline_metrics = {}
        self.leaderboard = []
        self._epoch_rr = []
        self._epoch_ts = []
        self._epoch_start_time = 0.0

        # Display variables
        self._latest_coherence = 0.0

    def build(self, parent: str) -> None:
        if self._built: return

        with dpg.tree_node(label="Resonance Frequency Assessment", parent=parent, default_open=True, tag="rb_node"):
            
            dpg.add_text("Resonance Frequency Assessment", color=(100, 220, 180))
            dpg.add_text("⚠️ HR Device Required (Connect Polar H10 or PVS)", tag="rb_hr_warning_text", color=(255, 50, 50))
            dpg.add_separator()
            
            with dpg.tab_bar():
                # --- TAB 1: ASSESSMENT PROTOCOLS ---
                with dpg.tab(label="Virtual Clinician (Assessment)"):
                    dpg.add_spacer(height=10)
                    dpg.add_text("Automated Resonance Frequency Detection", color=(100, 220, 180))
                    dpg.add_text("Evaluates Phase Synchrony, Baroreflex RSA, and Vagal Tone to find your perfect rhythm.", color=(150, 150, 150))
                    dpg.add_separator()
                    
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="START DEEP CALIBRATION", tag="rb_start_assess_btn", callback=self._start_assessment, width=200, height=40, enabled=False)
                        dpg.add_button(label="CANCEL", tag="rb_stop_assess_btn", callback=self._stop_assessment, width=100, height=40, show=False)
                    
                    dpg.add_spacer(height=10)
                    # Status HUD
                    with dpg.group(tag="rb_status_group", show=True):
                        dpg.add_text("SYSTEM IDLE", tag="rb_state_text", color=(255, 200, 0))
                        dpg.bind_item_font("rb_state_text", "large_font")
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

            # --- COMMON PACER DRAWLIST ---
            dpg.add_separator()
            dpg.add_spacer(height=10)
            
            # The drawlist is hidden initially so it doesn't run unprompted
            with dpg.drawlist(width=_PACER_W, height=_PACER_H, tag="rb_drawlist", show=False):
                self.pacer.setup_draw_layer("rb_drawlist")

        self._built = True
        self.set_hr_status(self._hr_connected) # Enforce lock on load

    # -------------------------------------------------------------------------
    # HARDWARE CONNECTION LOCK
    # -------------------------------------------------------------------------
    def set_hr_status(self, is_connected: bool):
        """Called by UI Manager when the H10/PVS connects or disconnects."""
        self._hr_connected = is_connected
        if not self._built: return
        
        if not is_connected:
            dpg.set_value("rb_hr_warning_text", "⚠️ HR Device Required (Connect Polar H10 or Verity Sense)")
            dpg.configure_item("rb_start_assess_btn", enabled=False)
            dpg.configure_item("rb_manual_start_btn", enabled=False)
            
            # Abort active assessment to prevent corrupted math
            if self.state not in [self.STATE_IDLE, self.STATE_COMPLETE, "MANUAL_ACTIVE"]:
                self._stop_assessment()
                self._update_hud("ABORTED", "HR Device Disconnected.", 0.0, (255, 0, 0))
            elif self.state == "MANUAL_ACTIVE":
                self._toggle_manual() # Stop manual
        else:
            dpg.set_value("rb_hr_warning_text", "")
            # Only unlock buttons if we aren't currently running an assessment
            if self.state in [self.STATE_IDLE, self.STATE_COMPLETE]:
                dpg.configure_item("rb_start_assess_btn", enabled=True)
                dpg.configure_item("rb_manual_start_btn", enabled=True)

    # -------------------------------------------------------------------------
    # DATA INGESTION PIPELINE
    # -------------------------------------------------------------------------
    def feed_rr(self, rr_ms: float):
        """Called externally by UI Manager for every incoming RR interval."""
        if not self._built: return
        
        # Only record data if we are actively in an assessment block
        if self.state in [self.STATE_BASELINE, self.STATE_TESTING]:
            ts = time.time() - self._epoch_start_time
            self._epoch_rr.append(rr_ms)
            self._epoch_ts.append(ts)

    def update_resonance_score(self, score: float) -> None:
        """Fallback for coherence display in manual mode."""
        self._latest_coherence = score
        if self._built and dpg.does_item_exist("rb_manual_coherence"):
            dpg.set_value("rb_manual_coherence", f"{score:.1f}")

    # -------------------------------------------------------------------------
    # ENGINE TICK (STATE MACHINE)
    # -------------------------------------------------------------------------
    def tick(self) -> None:
        if not self._built: return
        
        # Only show and animate the Pacer when explicitly pacing
        if self.state == self.STATE_TESTING or self.state == "MANUAL_ACTIVE":
            dpg.configure_item("rb_drawlist", show=True)
            self.pacer.update(_PACER_W, _PACER_H)
        else:
            dpg.configure_item("rb_drawlist", show=False)
        
        # Assessment State Machine Logic
        if self.state != self.STATE_IDLE and self.state != self.STATE_COMPLETE and self.state != "MANUAL_ACTIVE":
            elapsed = time.time() - self.block_start_time
            
            if self.state == self.STATE_BASELINE:
                self._update_hud(f"STAGE 1: BASELINE ({int(self.baseline_duration - elapsed)}s)", "Breathe normally. Do not pace your breath.", elapsed / self.baseline_duration, (0, 150, 255))
                if elapsed >= self.baseline_duration:
                    self._process_baseline()
            
            elif self.state == self.STATE_TESTING:
                bpm, ratio = self.assessment_grid[self.grid_index]
                self._update_hud(f"TESTING: {bpm} BPM | Ratio 1:{ratio} ({int(self.test_duration - elapsed)}s)", "Follow the breathing circle precisely.", elapsed / self.test_duration, (0, 255, 100))
                if elapsed >= self.test_duration:
                    self._process_testing_block(bpm, ratio)
            
            elif self.state == self.STATE_WASHOUT:
                self._update_hud(f"WASHOUT / REST ({int(self.washout_duration - elapsed)}s)", "Breathe normally. Clearing autonomic load.", elapsed / self.washout_duration, (255, 150, 0))
                if elapsed >= self.washout_duration:
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
        
        self.leaderboard.clear()
        self._clear_leaderboard_ui()
        dpg.configure_item("rb_start_assess_btn", show=False)
        dpg.configure_item("rb_stop_assess_btn", show=True)
        dpg.configure_item("rb_manual_start_btn", enabled=False)
        
        # Hide Pacer for baseline
        self.pacer.reset()
        
        # Transition to Baseline
        self.state = self.STATE_BASELINE
        self.block_start_time = time.time()
        self._epoch_start_time = time.time()
        self._epoch_rr.clear()
        self._epoch_ts.clear()

    def _stop_assessment(self, sender=None, app_data=None):
        self.state = self.STATE_IDLE
        self.pacer.reset()
        dpg.configure_item("rb_start_assess_btn", show=True)
        dpg.configure_item("rb_stop_assess_btn", show=False)
        dpg.configure_item("rb_manual_start_btn", enabled=self._hr_connected)
        self._update_hud("SYSTEM IDLE", "Assessment Cancelled.", 0.0, (255, 50, 50))

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
        bpm, ratio = self.assessment_grid[self.grid_index]
        
        # Configure Pacer exactly
        cycle_sec = 60.0 / bpm
        inhale_sec = cycle_sec / (1.0 + ratio)
        exhale_sec = cycle_sec - inhale_sec
        self.pacer.set_timing(inhale_sec, 0.0, exhale_sec, 0.0)
        self.pacer.reset()
        
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
        
        # Hide Pacer and enter washout
        self.pacer.reset()
        self.state = self.STATE_WASHOUT
        self.block_start_time = time.time()

    def _finish_assessment(self):
        self.state = self.STATE_COMPLETE
        self.pacer.reset()
        dpg.configure_item("rb_start_assess_btn", show=True)
        dpg.configure_item("rb_stop_assess_btn", show=False)
        dpg.configure_item("rb_manual_start_btn", enabled=self._hr_connected)
        
        if self.leaderboard:
            best = max(self.leaderboard, key=lambda x: x['score'])
            msg = f"OPTIMAL FOUND: {best['bpm']} BPM (Ratio 1:{best['ratio']})"
            self._update_hud("ASSESSMENT COMPLETE", msg, 1.0, (0, 255, 0))
        else:
            self._update_hud("ASSESSMENT COMPLETE", "No data gathered.", 1.0, (150, 150, 150))

    # -------------------------------------------------------------------------
    # UI HELPER METHODS
    # -------------------------------------------------------------------------
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
        i = dpg.get_value("rb_m_in")
        hi = dpg.get_value("rb_m_hi")
        e = dpg.get_value("rb_m_ex")
        he = dpg.get_value("rb_m_he")
        self.pacer.set_timing(i, hi, e, he)

    def _toggle_manual(self, sender, app_data):
        if not self._hr_connected: return
        
        if self.state == self.STATE_IDLE or self.state == self.STATE_COMPLETE:
            self.state = "MANUAL_ACTIVE"
            dpg.configure_item("rb_manual_start_btn", label="Stop Manual")
            dpg.configure_item("rb_start_assess_btn", enabled=False)
            self.pacer.reset()
        elif self.state == "MANUAL_ACTIVE":
            self.state = self.STATE_IDLE
            dpg.configure_item("rb_manual_start_btn", label="Start Manual")
            dpg.configure_item("rb_start_assess_btn", enabled=True)
            self.pacer.reset()