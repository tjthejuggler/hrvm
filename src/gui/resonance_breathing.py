"""
Resonance Breathing App & Automated RF Assessment System.
"""
import time
import logging
import json
import os
import threading
from datetime import datetime
import numpy as np
import dearpygui.dearpygui as dpg

# Import from our new decoupled clinical math engine
from src.processing.resonance_math import PhysiologicalMath, ContinuousPacer, ContinuousSlidingMath

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Leaderboard score → base RGB color mapping
#
# The leaderboard score from score_epoch() is multiplied by 100 and can
# theoretically reach ~260 (phase=1.0, pt_amp=5×baseline, lf_nu=100%,
# rmssd=5×baseline).  In practice, scores above ~220 are extraordinary.
#
# Scale (designed so blue ≈ 200, white/yellow/pink are very rare):
#   Red    0 – 80    low
#   Orange 80 – 130  fair
#   Green  130 – 170 good
#   Blue   170 – 210 very good
#   Pink   210 – 230 excellent    (rare)
#   Yellow 230 – 245 exceptional  (very rare)
#   White  245+      extraordinary (almost never seen)
# ---------------------------------------------------------------------------
_COHERENCE_COLORS = [
    # (min_score, (R, G, B))
    (245, (255, 255, 255)),   # White  — extraordinary (≥245)
    (230, (255, 255,   0)),   # Yellow — exceptional  (230–245)
    (210, (255, 105, 180)),   # Pink   — excellent    (210–230)
    (170, (  0,   0, 255)),   # Blue   — very good    (170–210)
    (130, (  0, 200,   0)),   # Green  — good         (130–170)
    ( 80, (255, 140,   0)),   # Orange — fair         (80–130)
    (  0, (255,   0,   0)),   # Red    — low          (0–80)
]

def _coherence_to_base_color(score: float):
    """Return (R, G, B) base color for the given leaderboard score (0–260+)."""
    for threshold, color in _COHERENCE_COLORS:
        if score >= threshold:
            return color
    return (255, 0, 0)

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

        # LTX LED ball integration
        self._led_ball = None          # set via set_led_ball()
        self._use_ltx_ball = False     # toggled by checkbox
        self._ltx_flash_timer = None   # threading.Timer for flash-back
        self._ltx_prev_vol = 0.0       # previous breath bar volume (0–1)
        self._ltx_prev_phase = ""      # previous phase text
        self._latest_coherence = 0.0   # live coherence (0–100), for UI display
        self._latest_lb_score = 0.0    # leaderboard score (0–260+), drives ball color
        
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

        # ACC breathing integration
        # When True, coherence score uses the real-time ACC breath rate instead
        # of the prescribed pacer rate.
        self._use_acc_breathing: bool = False
        self._acc_breath_rate_bpm: float = None  # updated by ui_manager

        # Separate leaderboards: keyed by source ("prescribed" | "acc")
        self._leaderboards: dict = {"prescribed": [], "acc": []}

        self.history = []
        self.historical_optimal = None

        # Flat list of all individual session entries for the history tab.
        # Each entry: {session_ts, session_dt, bpm, ratio, score, phase, pt, lf, source}
        self._all_session_entries: list = []

        # History-tab filter/sort state
        self._hist_sort_col: str = "session_ts"   # column key to sort by
        self._hist_sort_asc: bool = False          # descending by default (newest first)
        self._hist_filter_source: str = "All"     # "All" | "Prescribed" | "ACC"

        self._load_history()

    def _load_history(self):
        if os.path.exists(_HISTORY_FILE):
            try:
                with open(_HISTORY_FILE, "r") as f:
                    self.history = json.load(f)
                    if self.history:
                        self.historical_optimal = self.history[-1].get("best", None)
                        # Seed ball color from the best historical score
                        if self.historical_optimal:
                            self._latest_lb_score = float(
                                self.historical_optimal.get("score", 0.0)
                            )
                        self._rebuild_all_session_entries()
            except Exception as e:
                logger.error(f"Failed to load RF history: {e}")

    def _rebuild_all_session_entries(self):
        """Flatten all history runs into self._all_session_entries for the history tab."""
        self._all_session_entries = []
        for run in self.history:
            session_ts = run.get("timestamp", 0.0)
            session_dt = datetime.fromtimestamp(session_ts).strftime("%Y-%m-%d %H:%M")
            for entry in run.get("leaderboard", []):
                flat = {
                    "session_ts": session_ts,
                    "session_dt": session_dt,
                    "bpm": entry.get("bpm", 0.0),
                    "ratio": entry.get("ratio", 1.0),
                    "score": entry.get("score", 0.0),
                    "phase": entry.get("phase", 0.0),
                    "pt": entry.get("pt", 0.0),
                    "lf": entry.get("lf", 0.0),
                    "source": entry.get("source", "prescribed"),
                }
                self._all_session_entries.append(flat)

    def _save_history(self, best_node, leaderboard):
        self.history.append({"timestamp": time.time(), "best": best_node, "leaderboard": leaderboard})
        try:
            with open(_HISTORY_FILE, "w") as f:
                json.dump(self.history, f, indent=4)
            self.historical_optimal = best_node
            self._rebuild_all_session_entries()
        except Exception as e:
            logger.error(f"Failed to save RF history: {e}")

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
                    # ACC breathing toggle
                    dpg.add_spacer(height=6)
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(
                            label="Use ACC Breathing (real-time breath rate for coherence score)",
                            tag="rb_use_acc_checkbox",
                            default_value=False,
                            callback=self._on_acc_toggle,
                        )
                    dpg.add_text(
                        "ACC breath rate: --- br/min",
                        tag="rb_acc_rate_text",
                        color=(100, 255, 180),
                    )
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
                    dpg.add_spacer(height=8)
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(
                            label="Use LTX Ball (breath bar → brightness, coherence → color)",
                            tag="rb_use_ltx_checkbox",
                            default_value=False,
                            callback=self._on_ltx_toggle,
                        )
                        dpg.add_spacer(width=14)
                        # Visual ball model — always visible, mirrors real ball state
                        with dpg.drawlist(width=36, height=36, tag="rb_ltx_ball_drawlist"):
                            dpg.draw_circle(
                                center=(18, 18), radius=15,
                                color=(200, 200, 200, 180), thickness=2,
                            )
                            dpg.draw_circle(
                                center=(18, 18), radius=13,
                                fill=(30, 30, 30, 255),
                                color=(0, 0, 0, 0),
                                tag="rb_ltx_ball_fill",
                            )

                # --- Leaderboard tab: two sub-tabs for prescribed vs ACC ---
                with dpg.tab(label="Assessment Leaderboard", tag="rb_leaderboard_tab"):
                    dpg.add_spacer(height=6)
                    with dpg.tab_bar(tag="rb_lb_tabbar"):
                        with dpg.tab(label="Prescribed Rate", tag="rb_lb_tab_prescribed"):
                            dpg.add_spacer(height=6)
                            with dpg.table(header_row=True, tag="rb_leaderboard_table",
                                           borders_innerH=True, borders_outerH=True):
                                dpg.add_table_column(label="Rank")
                                dpg.add_table_column(label="BPM")
                                dpg.add_table_column(label="Protocol/Ratio")
                                dpg.add_table_column(label="Score")
                                dpg.add_table_column(label="Phase/PLV")
                                dpg.add_table_column(label="PT Amp")
                                dpg.add_table_column(label="LFnu")
                        with dpg.tab(label="ACC Breathing", tag="rb_lb_tab_acc"):
                            dpg.add_spacer(height=6)
                            with dpg.table(header_row=True, tag="rb_leaderboard_acc_table",
                                           borders_innerH=True, borders_outerH=True):
                                dpg.add_table_column(label="Rank")
                                dpg.add_table_column(label="BPM")
                                dpg.add_table_column(label="Protocol/Ratio")
                                dpg.add_table_column(label="Score")
                                dpg.add_table_column(label="Phase/PLV")
                                dpg.add_table_column(label="PT Amp")
                                dpg.add_table_column(label="LFnu")

                # --- Session History tab ---
                with dpg.tab(label="Session History", tag="rb_history_tab"):
                    self._build_history_tab()

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
            self._leaderboards["prescribed"] = list(self.leaderboard)
            self._update_leaderboard_ui()
        # Populate history tab from loaded data
        self._refresh_history_ui()

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

    def set_acc_breath_rate(self, bpm: float):
        """Called by ui_manager each time the ACC engine produces a new breath rate."""
        self._acc_breath_rate_bpm = bpm
        if self._built and dpg.does_item_exist("rb_acc_rate_text"):
            dpg.set_value("rb_acc_rate_text", f"ACC breath rate: {bpm:.1f} br/min")

    def _on_acc_toggle(self, sender, app_data):
        self._use_acc_breathing = bool(app_data)

    def feed_rr(self, rr_ms: float):
        """Called by ui_manager for every heartbeat to fill our data arrays."""
        if not self._built: return
        if self.state in [self.STATE_BASELINE, self.STATE_TESTING, self.STATE_TESTING_CONT]:
            ts = time.time() - self._epoch_start_time
            self._epoch_rr.append(rr_ms)
            self._epoch_ts.append(ts)

    def set_led_ball(self, led_ball) -> None:
        """Inject the shared LEDBallController instance from UIManager."""
        self._led_ball = led_ball

    def _on_ltx_toggle(self, sender, app_data):
        self._use_ltx_ball = bool(app_data)
        if not self._use_ltx_ball and self._led_ball is not None:
            # Turn the real ball off when feature is disabled;
            # the UI circle continues to show the current state.
            self._cancel_flash_timer()
            self._led_ball.send_color(0, 0, 0)

    def update_resonance_score(self, score: float) -> None:
        """MISSING METHOD RESTORED: Satisfies ui_manager.py payload update"""
        self._latest_coherence = score
        if self._built and dpg.does_item_exist("rb_manual_coherence"):
            dpg.set_value("rb_manual_coherence", f"{score:.1f}")

    # -------------------------------------------------------------------------
    # LTX LED Ball helpers
    # -------------------------------------------------------------------------
    def _cancel_flash_timer(self):
        if self._ltx_flash_timer is not None:
            self._ltx_flash_timer.cancel()
            self._ltx_flash_timer = None

    def _send_ball_color(self, r: int, g: int, b: int):
        """Send color to the real ball (clamped) AND update the UI circle."""
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        self._set_ui_ball_color(r, g, b)
        if self._use_ltx_ball and self._led_ball is not None:
            self._led_ball.send_color(r, g, b)

    def _set_ui_ball_color(self, r: int, g: int, b: int):
        """Update only the on-screen ball circle (safe to call from any thread)."""
        if self._built and dpg.does_item_exist("rb_ltx_ball_fill"):
            dpg.configure_item("rb_ltx_ball_fill", fill=(r, g, b, 255))

    def _update_ltx_ball(self, vol: float, phase_txt: str):
        """Drive the UI ball model (and optionally the real ball) from the breath bar.

        vol       — breath bar fill fraction 0.0 (empty) → 1.0 (full)
        phase_txt — "INHALE", "EXHALE", "HOLD FULL", "HOLD EMPTY"

        The UI circle always updates regardless of whether the real ball is
        enabled or connected.
        """
        # Use the leaderboard score (0–260+) for color; falls back to 0 until
        # the first assessment block completes.
        br, bg, bb = _coherence_to_base_color(self._latest_lb_score)

        # Detect phase transitions for flash effects
        prev_vol = self._ltx_prev_vol

        # Peak-inhale flash: bar just reached full (vol ≥ 0.98) and was rising
        at_peak = vol >= 0.98 and prev_vol < 0.98 and phase_txt in ("INHALE", "HOLD FULL")
        # Trough-exhale flash: bar just reached empty (vol ≤ 0.02) and was falling
        at_trough = vol <= 0.02 and prev_vol > 0.02 and phase_txt in ("EXHALE", "HOLD EMPTY")

        self._ltx_prev_vol = vol
        self._ltx_prev_phase = phase_txt

        if at_peak:
            # Flash to black for 100 ms, then restore full brightness
            self._cancel_flash_timer()
            self._send_ball_color(0, 0, 0)
            def _restore_peak():
                self._send_ball_color(br, bg, bb)
            self._ltx_flash_timer = threading.Timer(0.1, _restore_peak)
            self._ltx_flash_timer.daemon = True
            self._ltx_flash_timer.start()
            return

        if at_trough:
            # Flash to full brightness for 100 ms, then restore dim level
            self._cancel_flash_timer()
            self._send_ball_color(br, bg, bb)
            def _restore_trough():
                dim_r = max(10, int(br * 0.05))
                dim_g = max(10, int(bg * 0.05))
                dim_b = max(10, int(bb * 0.05))
                self._send_ball_color(dim_r, dim_g, dim_b)
            self._ltx_flash_timer = threading.Timer(0.1, _restore_trough)
            self._ltx_flash_timer.daemon = True
            self._ltx_flash_timer.start()
            return

        # Normal: scale base color by vol (min 5% so circle never goes fully dark)
        brightness = 0.05 + 0.95 * vol
        self._send_ball_color(
            int(br * brightness),
            int(bg * brightness),
            int(bb * brightness),
        )

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

        # Drive LTX ball from breath bar state
        self._update_ltx_ball(vol, txt)

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
        self._leaderboards["prescribed"].clear()
        self._leaderboards["acc"].clear()
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
        # When ACC breathing is active, use the real-time breath rate for phase
        # synchrony calculation; the prescribed bpm/ratio still drives the pacer.
        if self._use_acc_breathing and self._acc_breath_rate_bpm is not None:
            effective_bpm = self._acc_breath_rate_bpm
            # Assume 1:1 ratio for ACC-derived rate (no prescribed ratio)
            effective_cycle = 60.0 / effective_bpm
            i_sec = effective_cycle / 2.0
            e_sec = effective_cycle / 2.0
        else:
            effective_bpm = bpm
            effective_cycle = 60.0 / bpm
            i_sec = effective_cycle / (1.0 + ratio)
            e_sec = effective_cycle - i_sec

        breath_cycles = [(t, t + effective_cycle)
                         for t in np.arange(0, self.test_duration, effective_cycle)]

        rmssd = self.math.calculate_rmssd(self._epoch_rr)
        lf_abs, lf_nu = self.math.calculate_spectral_power(self._epoch_rr, self._epoch_ts)
        pt = self.math.calculate_pt_amplitude(self._epoch_rr, self._epoch_ts, breath_cycles)
        phase = self.math.calculate_phase_synchrony(
            self._epoch_rr, self._epoch_ts, i_sec, e_sec
        )

        score = self.math.score_epoch(rmssd, pt, lf_nu, phase, self.baseline_metrics)
        # Keep the best leaderboard score seen so far — drives ball color
        if score > self._latest_lb_score:
            self._latest_lb_score = score
        entry = {
            'bpm': bpm,
            'ratio': ratio,
            'score': score,
            'rmssd': rmssd,
            'lf': lf_nu,
            'pt': pt,
            'phase': phase,
            'source': 'acc' if self._use_acc_breathing else 'prescribed',
        }
        self.leaderboard.append(entry)
        # Route to the appropriate sub-leaderboard
        source_key = 'acc' if self._use_acc_breathing else 'prescribed'
        self._leaderboards[source_key].append(entry)
        self._update_leaderboard_ui()

        if self.grid_index == len(self.assessment_grid) - 1:
            self._finish_stepped_assessment()
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
            best_bpm, best_score = csm.extract_resonance_frequency(
                time_grid, lf_power, pt_amp, plv, bpm_arr
            )
            if best_score > self._latest_lb_score:
                self._latest_lb_score = best_score
            entry = {
                'bpm': round(best_bpm, 1), 'ratio': "Cont",
                'score': best_score, 'rmssd': 0.0, 'lf': 0.0, 'pt': 0.0, 'phase': 0.0,
                'source': 'acc' if self._use_acc_breathing else 'prescribed',
            }
            self.leaderboard.append(entry)
            source_key = 'acc' if self._use_acc_breathing else 'prescribed'
            self._leaderboards[source_key].append(entry)
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
            source_label = " (ACC)" if best.get('source') == 'acc' else " (Prescribed)"
            self._update_hud(
                "ASSESSMENT COMPLETE",
                f"OPTIMAL FOUND: {best['bpm']} BPM{source_label}",
                1.0, (0, 255, 0),
            )
            dpg.set_value("rb_total_time_text", "COMPLETE")
            dpg.set_value(
                "rb_history_display",
                f"Historical Optimal: {best['bpm']} BPM{source_label} - Score: {best['score']:.1f}",
            )
        else:
            self._update_hud("ASSESSMENT COMPLETE", "No data gathered.", 1.0, (150, 150, 150))

    def _fmt_time(self, seconds: float) -> str: return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"

    def _update_hud(self, title, instruction, progress, color):
        dpg.set_value("rb_state_text", title)
        dpg.configure_item("rb_state_text", color=color)
        dpg.set_value("rb_instruction_text", instruction)
        dpg.set_value("rb_progress_bar", progress)

    # -------------------------------------------------------------------------
    # SESSION HISTORY TAB
    # -------------------------------------------------------------------------
    def _build_history_tab(self):
        """Build the filter/sort controls and the history table inside the history tab."""
        dpg.add_spacer(height=6)

        # --- Filter / Sort controls ---
        with dpg.group(horizontal=True):
            dpg.add_text("Filter Source:")
            dpg.add_combo(
                ["All", "Prescribed", "ACC"],
                default_value="All",
                width=120,
                tag="rb_hist_filter_source",
                callback=self._on_hist_filter_change,
            )
            dpg.add_spacer(width=20)
            dpg.add_text("Sort by:")
            dpg.add_combo(
                ["Date/Time", "BPM", "Ratio", "Score", "Phase/PLV", "PT Amp", "LFnu"],
                default_value="Date/Time",
                width=130,
                tag="rb_hist_sort_col",
                callback=self._on_hist_sort_change,
            )
            dpg.add_combo(
                ["Descending", "Ascending"],
                default_value="Descending",
                width=120,
                tag="rb_hist_sort_dir",
                callback=self._on_hist_sort_change,
            )
            dpg.add_spacer(width=20)
            dpg.add_button(
                label="Clear History",
                tag="rb_hist_clear_btn",
                callback=self._on_hist_clear,
                width=120,
            )

        dpg.add_spacer(height=6)
        dpg.add_text("", tag="rb_hist_count_text", color=(150, 150, 150))
        dpg.add_spacer(height=4)

        with dpg.table(
            header_row=True,
            tag="rb_history_table",
            borders_innerH=True,
            borders_outerH=True,
            borders_innerV=True,
            scrollY=True,
            freeze_rows=1,
            height=400,
        ):
            dpg.add_table_column(label="Date / Time", width_fixed=True, init_width_or_weight=145)
            dpg.add_table_column(label="BPM", width_fixed=True, init_width_or_weight=60)
            dpg.add_table_column(label="Ratio", width_fixed=True, init_width_or_weight=60)
            dpg.add_table_column(label="Score", width_fixed=True, init_width_or_weight=70)
            dpg.add_table_column(label="Phase/PLV", width_fixed=True, init_width_or_weight=85)
            dpg.add_table_column(label="PT Amp", width_fixed=True, init_width_or_weight=75)
            dpg.add_table_column(label="LFnu", width_fixed=True, init_width_or_weight=65)
            dpg.add_table_column(label="Source", width_fixed=True, init_width_or_weight=90)

    # Column-key mapping from combo label → dict key
    _HIST_COL_MAP = {
        "Date/Time": "session_ts",
        "BPM": "bpm",
        "Ratio": "ratio",
        "Score": "score",
        "Phase/PLV": "phase",
        "PT Amp": "pt",
        "LFnu": "lf",
    }

    def _on_hist_filter_change(self, sender, app_data):
        self._hist_filter_source = app_data
        self._refresh_history_ui()

    def _on_hist_sort_change(self, sender, app_data):
        col_label = dpg.get_value("rb_hist_sort_col")
        self._hist_sort_col = self._HIST_COL_MAP.get(col_label, "session_ts")
        self._hist_sort_asc = dpg.get_value("rb_hist_sort_dir") == "Ascending"
        self._refresh_history_ui()

    def _on_hist_clear(self, sender, app_data):
        """Wipe rf_history.json and reset all in-memory history."""
        self.history.clear()
        self._all_session_entries.clear()
        self.historical_optimal = None
        try:
            with open(_HISTORY_FILE, "w") as f:
                json.dump([], f)
        except Exception as e:
            logger.error(f"Failed to clear RF history: {e}")
        if self._built and dpg.does_item_exist("rb_history_display"):
            dpg.set_value("rb_history_display", "Historical Optimal: None (Run a Global Sweep)")
            dpg.configure_item("rb_history_display", color=(150, 150, 150))
        self._refresh_history_ui()

    def _refresh_history_ui(self):
        """Repopulate the history table from self._all_session_entries."""
        if not self._built or not dpg.does_item_exist("rb_history_table"):
            return

        # Clear existing rows
        if children := dpg.get_item_children("rb_history_table", 1):
            for child in children:
                dpg.delete_item(child)

        # Filter
        source_filter = self._hist_filter_source
        if source_filter == "All":
            entries = list(self._all_session_entries)
        elif source_filter == "Prescribed":
            entries = [e for e in self._all_session_entries if e["source"] == "prescribed"]
        else:  # ACC
            entries = [e for e in self._all_session_entries if e["source"] == "acc"]

        # Sort — ratio can be "Cont" (string) so coerce to float for numeric sort
        sort_key = self._hist_sort_col
        def _sort_val(x):
            v = x.get(sort_key, 0)
            if sort_key == "ratio":
                return -1.0 if v == "Cont" else float(v)
            return v
        entries.sort(key=_sort_val, reverse=not self._hist_sort_asc)

        # Update count label
        if dpg.does_item_exist("rb_hist_count_text"):
            dpg.set_value("rb_hist_count_text", f"{len(entries)} session entries")

        # Populate rows
        for entry in entries:
            score = entry["score"]
            score_color = list(_coherence_to_base_color(score)) + [255]
            ratio_val = entry["ratio"]
            if ratio_val == "Cont":
                ratio_str = "Cont"
            else:
                # Show as integer if whole number (e.g. 1.0 → "1:1"), else 1 decimal
                r = float(ratio_val)
                ratio_str = f"1:{int(r)}" if r == int(r) else f"1:{r:.1f}"
            source_str = "ACC" if entry["source"] == "acc" else "Prescribed"
            with dpg.table_row(parent="rb_history_table"):
                dpg.add_text(entry["session_dt"])
                dpg.add_text(f"{entry['bpm']:.1f}")
                dpg.add_text(ratio_str)
                dpg.add_text(f"{score:.1f}", color=score_color)
                dpg.add_text(f"{entry['phase'] * 100:.1f}%")
                dpg.add_text(f"{entry['pt']:.1f}")
                dpg.add_text(f"{entry['lf']:.1f}%")
                dpg.add_text(source_str, color=(100, 200, 255) if entry["source"] == "acc" else (200, 200, 200))

    def _clear_leaderboard_ui(self):
        for tag in ("rb_leaderboard_table", "rb_leaderboard_acc_table"):
            if dpg.does_item_exist(tag):
                if children := dpg.get_item_children(tag, 1):
                    for child in children:
                        dpg.delete_item(child)

    def _populate_table(self, table_tag: str, entries: list):
        """Fill a leaderboard table with sorted entries."""
        if not dpg.does_item_exist(table_tag):
            return
        for rank, data in enumerate(
            sorted(entries, key=lambda x: x['score'], reverse=True), 1
        ):
            with dpg.table_row(parent=table_tag):
                dpg.add_text(f"#{rank}")
                dpg.add_text(f"{data['bpm']:.1f}")
                dpg.add_text(str(data['ratio']))
                dpg.add_text(
                    f"{data['score']:.1f}",
                    color=(255, 215, 0) if rank == 1 else (255, 255, 255),
                )
                dpg.add_text(f"{data.get('phase', 0.0)*100:.1f}%")
                dpg.add_text(f"{data.get('pt', 0.0):.1f}")
                dpg.add_text(f"{data.get('lf', 0.0):.1f}%")

    def _update_leaderboard_ui(self):
        self._clear_leaderboard_ui()
        # Prescribed leaderboard
        self._populate_table("rb_leaderboard_table", self._leaderboards["prescribed"])
        # ACC leaderboard
        self._populate_table("rb_leaderboard_acc_table", self._leaderboards["acc"])
        # Refresh history tab whenever a new entry is added
        self._refresh_history_ui()

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