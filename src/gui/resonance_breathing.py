"""
Resonance Breathing App for the HRV Biofeedback GUI.

Provides a self-contained breathing pacer with:
  - Manual timing inputs (inhale / hold-full / exhale / hold-empty)
  - Start / Stop button that drives the animated breathing circle
  - Session history chart showing past sessions and their resonance scores
"""
import time
import logging
import dearpygui.dearpygui as dpg
from typing import Optional, List, Dict, Any

from src.gui.pacer import PacerEngine

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────
_PACER_W = 600
_PACER_H = 340


class ResonanceBreathingWidget:
    """Builds and manages the Resonance Breathing app UI section.

    Placed inside the APPS collapsing header in ui_manager.
    Contains:
      - Breathing timing inputs (inhale / hold / exhale / hold)
      - Start / Stop button
      - Animated breathing circle (PacerEngine)
      - Session history bar chart (session # vs resonance score)
    """

    TAG_NODE          = "rb_node"
    TAG_START_BTN     = "rb_start_btn"
    TAG_INHALE        = "rb_inhale"
    TAG_HOLD_FULL     = "rb_hold_full"
    TAG_EXHALE        = "rb_exhale"
    TAG_HOLD_EMPTY    = "rb_hold_empty"
    TAG_BPM_LABEL     = "rb_bpm_label"
    TAG_STATUS        = "rb_status"
    TAG_COHERENCE     = "rb_coherence"
    TAG_DRAWLIST      = "rb_drawlist"
    TAG_PLOT          = "rb_plot"
    TAG_X_AXIS        = "rb_x_axis"
    TAG_Y_AXIS        = "rb_y_axis"
    TAG_SCORE_SERIES  = "rb_score_series"

    def __init__(self, db=None):
        """
        Parameters
        ----------
        db : DatabaseManager | None
            Optional reference to the shared DatabaseManager so the widget
            can persist and load session history.
        """
        self.db = db
        self._built = False
        self._active = False          # True while a session is running
        self._session_start: float = 0.0

        # Pacer engine (shared logic with the old top-bar circle)
        self.pacer = PacerEngine()
        self.pacer.set_timing(4.0, 0.0, 4.0, 0.0)  # default 4-4 resonance

        # Chart data
        self._session_indices: List[int] = []
        self._resonance_scores: List[float] = []

    # ── build / teardown ─────────────────────────────────────────────────────

    def build(self, parent: str) -> None:
        """Create the widget tree inside *parent*."""
        if self._built:
            return

        with dpg.tree_node(label="Resonance Breathing", parent=parent,
                           tag=self.TAG_NODE, default_open=True):

            dpg.add_text("Resonance Breathing", color=(100, 220, 180))
            dpg.add_separator()

            # ── Timing inputs ────────────────────────────────────────────
            with dpg.group(horizontal=True):
                dpg.add_text("Timing (seconds):", color=(180, 180, 180))

            with dpg.group(horizontal=True):
                dpg.add_input_float(
                    label="Inhale", tag=self.TAG_INHALE,
                    default_value=4.0, min_value=0.5, max_value=30.0,
                    step=0.5, width=160,
                    callback=self._on_timing_changed
                )
                dpg.add_spacer(width=10)
                dpg.add_input_float(
                    label="Hold (Full)", tag=self.TAG_HOLD_FULL,
                    default_value=0.0, min_value=0.0, max_value=30.0,
                    step=0.5, width=160,
                    callback=self._on_timing_changed
                )
                dpg.add_spacer(width=10)
                dpg.add_input_float(
                    label="Exhale", tag=self.TAG_EXHALE,
                    default_value=4.0, min_value=0.5, max_value=30.0,
                    step=0.5, width=160,
                    callback=self._on_timing_changed
                )
                dpg.add_spacer(width=10)
                dpg.add_input_float(
                    label="Hold (Empty)", tag=self.TAG_HOLD_EMPTY,
                    default_value=0.0, min_value=0.0, max_value=30.0,
                    step=0.5, width=160,
                    callback=self._on_timing_changed
                )

            dpg.add_spacer(height=4)
            dpg.add_text("Cycle: 8.0 s  |  Rate: 7.5 BPM",
                         tag=self.TAG_BPM_LABEL, color=(150, 220, 255))

            dpg.add_spacer(height=6)

            # ── Coherence (resonance score) display ──────────────────────
            with dpg.group(horizontal=True):
                dpg.add_text("COHERENCE:", color=(150, 150, 150))
                dpg.add_text("—", tag=self.TAG_COHERENCE, color=(0, 255, 0))

            dpg.add_spacer(height=6)

            # ── Start / Stop button ──────────────────────────────────────
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Start", tag=self.TAG_START_BTN,
                    callback=self._on_start_stop, width=120
                )
                dpg.add_spacer(width=16)
                dpg.add_text("Idle", tag=self.TAG_STATUS,
                             color=(150, 150, 150))

            dpg.add_spacer(height=8)

            # ── Breathing circle (PacerEngine) ───────────────────────────
            with dpg.drawlist(width=_PACER_W, height=_PACER_H,
                              tag=self.TAG_DRAWLIST):
                self.pacer.setup_draw_layer(self.TAG_DRAWLIST)

            dpg.add_spacer(height=10)
            dpg.add_separator()

            # ── Session history chart ────────────────────────────────────
            dpg.add_text("Session History", color=(180, 180, 180))
            with dpg.plot(label="Resonance Score per Session",
                          height=200, width=-1, tag=self.TAG_PLOT):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Session #",
                                  tag=self.TAG_X_AXIS)
                with dpg.plot_axis(dpg.mvYAxis, label="Resonance Score",
                                   tag=self.TAG_Y_AXIS):
                    dpg.add_bar_series([], [], label="Resonance Score",
                                       tag=self.TAG_SCORE_SERIES, weight=0.6)

            dpg.add_separator()

        self._built = True
        self._refresh_bpm_label()
        self._load_history()

    def destroy(self) -> None:
        if self._built and dpg.does_item_exist(self.TAG_NODE):
            dpg.delete_item(self.TAG_NODE)
        self._built = False

    @property
    def is_built(self) -> bool:
        return self._built

    # ── per-frame update ──────────────────────────────────────────────────────

    def tick(self) -> None:
        """Must be called every frame from the UIManager main loop."""
        if not self._built or not self._active:
            return
        self.pacer.update(_PACER_W, _PACER_H)

    # ── public API ────────────────────────────────────────────────────────────

    def stop_session(self, resonance_score: float = 0.0) -> None:
        """Stop the current session and persist it.

        Called externally (e.g. from UIManager) when a recording ends,
        or internally when the user presses Stop.
        """
        if not self._active:
            return
        self._active = False
        duration = time.time() - self._session_start
        self._persist_session(duration, resonance_score)
        if self._built:
            dpg.configure_item(self.TAG_START_BTN, label="Start")
            dpg.set_value(self.TAG_STATUS, "Idle")
            dpg.configure_item(self.TAG_STATUS, color=(150, 150, 150))

    def update_resonance_score(self, score: float) -> None:
        """Feed the latest coherence/resonance score (called each data frame)."""
        self._latest_score = score
        if self._built and dpg.does_item_exist(self.TAG_COHERENCE):
            dpg.set_value(self.TAG_COHERENCE, f"{score:.1f}")

    # ── callbacks ─────────────────────────────────────────────────────────────

    def _on_timing_changed(self, sender=None, app_data=None) -> None:
        inhale     = dpg.get_value(self.TAG_INHALE)
        hold_full  = dpg.get_value(self.TAG_HOLD_FULL)
        exhale     = dpg.get_value(self.TAG_EXHALE)
        hold_empty = dpg.get_value(self.TAG_HOLD_EMPTY)
        self.pacer.set_timing(inhale, hold_full, exhale, hold_empty)
        self._refresh_bpm_label()

    def _on_start_stop(self, sender=None, app_data=None) -> None:
        if not self._active:
            self._start_session()
        else:
            self.stop_session(getattr(self, "_latest_score", 0.0))

    # ── private helpers ───────────────────────────────────────────────────────

    def _start_session(self) -> None:
        self._active = True
        self._session_start = time.time()
        self._latest_score = 0.0
        self.pacer.reset()
        if self._built:
            dpg.configure_item(self.TAG_START_BTN, label="Stop")
            dpg.set_value(self.TAG_STATUS, "Breathing…")
            dpg.configure_item(self.TAG_STATUS, color=(0, 220, 120))

    def _refresh_bpm_label(self) -> None:
        if not self._built:
            return
        cycle = self.pacer.get_cycle_duration()
        bpm   = self.pacer.get_bpm()
        dpg.set_value(
            self.TAG_BPM_LABEL,
            f"Cycle: {cycle:.1f} s  |  Rate: {bpm:.2f} BPM"
        )

    def _persist_session(self, duration_s: float, resonance_score: float) -> None:
        """Save session to DB and update the chart."""
        if self.db is not None:
            try:
                inhale     = dpg.get_value(self.TAG_INHALE)     if self._built else self.pacer.inhale_time
                hold_full  = dpg.get_value(self.TAG_HOLD_FULL)  if self._built else self.pacer.inhale_hold_time
                exhale     = dpg.get_value(self.TAG_EXHALE)     if self._built else self.pacer.exhale_time
                hold_empty = dpg.get_value(self.TAG_HOLD_EMPTY) if self._built else self.pacer.exhale_hold_time
                self.db.save_breathing_session(
                    duration_s=duration_s,
                    resonance_score=resonance_score,
                    inhale=inhale,
                    hold_full=hold_full,
                    exhale=exhale,
                    hold_empty=hold_empty,
                )
            except Exception as e:
                logger.error(f"Failed to save breathing session: {e}")

        # Append to in-memory chart data
        idx = len(self._session_indices) + 1
        self._session_indices.append(float(idx))
        self._resonance_scores.append(resonance_score)
        self._refresh_chart()

    def _load_history(self) -> None:
        """Load persisted sessions from DB into the chart."""
        if self.db is None:
            return
        try:
            sessions = self.db.get_breathing_sessions(limit=50)
            self._session_indices = []
            self._resonance_scores = []
            for i, s in enumerate(reversed(sessions), start=1):
                self._session_indices.append(float(i))
                self._resonance_scores.append(s.get("resonance_score") or 0.0)
            self._refresh_chart()
        except Exception as e:
            logger.error(f"Failed to load breathing session history: {e}")

    def _refresh_chart(self) -> None:
        if not self._built:
            return
        dpg.set_value(
            self.TAG_SCORE_SERIES,
            [list(self._session_indices), list(self._resonance_scores)]
        )
        if self._session_indices:
            dpg.fit_axis_data(self.TAG_X_AXIS)
            dpg.fit_axis_data(self.TAG_Y_AXIS)
