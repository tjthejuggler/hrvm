"""
Counting Game module for the HRV Biofeedback GUI.

The user tries to count their own heartbeats during a random interval (20-80s).
After the interval, they submit their guess. The actual BPM is calculated from
RR intervals collected during the game period. Results are persisted to a JSON
file and displayed on a scatter chart (guessed BPM vs actual BPM per round).
"""
import json
import os
import time
import random
import logging
import dearpygui.dearpygui as dpg
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# --- Persistent data store (JSON file) ---

DATA_FILE = "counting_game_data.json"


def load_game_history(filepath: str = DATA_FILE) -> List[Dict[str, Any]]:
    """Load counting game history from JSON file."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load counting game data: {e}")
        return []


def save_game_entry(entry: Dict[str, Any], filepath: str = DATA_FILE) -> None:
    """Append a single game entry to the JSON file."""
    history = load_game_history(filepath)
    history.append(entry)
    try:
        with open(filepath, "w") as f:
            json.dump(history, f, indent=2)
    except IOError as e:
        logger.error(f"Failed to save counting game data: {e}")


# --- Counting Game Controller ---

class CountingGameController:
    """Manages the state machine for a single counting-game round.

    States:
        idle     – waiting for user to press Start
        counting – timer running, user is counting heartbeats
        input    – timer expired, waiting for user to submit guess
    """

    def __init__(self):
        self.state = "idle"  # idle | counting | input
        self._start_time: float = 0.0
        self._duration: float = 0.0  # random 20-80 s
        self._rr_intervals: List[float] = []  # collected during round (ms)

    # -- public API called by UIManager --

    def start_round(self) -> float:
        """Begin a new counting round. Returns the chosen duration (hidden)."""
        self._duration = random.uniform(20.0, 80.0)
        self._start_time = time.time()
        self._rr_intervals = []
        self.state = "counting"
        return self._duration

    def tick(self) -> bool:
        """Called every frame. Returns True when the round timer expires."""
        if self.state != "counting":
            return False
        elapsed = time.time() - self._start_time
        if elapsed >= self._duration:
            self.state = "input"
            return True
        return False

    def add_rr(self, rr_ms: float) -> None:
        """Feed an RR interval received while counting is active."""
        if self.state == "counting":
            self._rr_intervals.append(rr_ms)

    def submit_guess(self, guessed_count: int) -> Optional[Dict[str, Any]]:
        """User submits their heartbeat count. Returns the result dict or None."""
        if self.state != "input":
            return None

        duration = time.time() - self._start_time  # actual wall-clock
        # Use the stored duration (the planned one) for BPM calc
        actual_duration = self._duration

        # Actual heartbeats = number of RR intervals collected
        actual_beats = len(self._rr_intervals)
        actual_bpm = (actual_beats / actual_duration) * 60.0 if actual_duration > 0 else 0.0

        # Guessed BPM extrapolated from user's count
        guessed_bpm = (guessed_count / actual_duration) * 60.0 if actual_duration > 0 else 0.0

        entry = {
            "timestamp": time.time(),
            "duration_s": round(actual_duration, 2),
            "guessed_count": guessed_count,
            "actual_beats": actual_beats,
            "guessed_bpm": round(guessed_bpm, 1),
            "actual_bpm": round(actual_bpm, 1),
        }

        save_game_entry(entry)
        self.state = "idle"
        return entry


# --- DearPyGui Widget (controls + chart) ---

class CountingGameWidget:
    """Builds and manages the counting-game UI section.

    Placed at the top of the charts area when session_mode == 'counting'.
    Contains:
      - Start / Count… / Stop button
      - Guess input + Submit button
      - Scatter chart of guessed BPM vs actual BPM per round
    """

    TAG_GROUP = "counting_game_group"
    TAG_BTN = "counting_game_btn"
    TAG_INPUT = "counting_game_input"
    TAG_SUBMIT = "counting_game_submit"
    TAG_STATUS = "counting_game_status"
    TAG_PLOT = "counting_game_plot"

    def __init__(self):
        self.controller = CountingGameController()
        self._built = False
        # Chart data (loaded from file on build)
        self._round_indices: List[int] = []
        self._guessed_bpms: List[float] = []
        self._actual_bpms: List[float] = []

    # -- build / teardown --

    def build(self, parent: str) -> None:
        """Create the widget tree inside *parent*."""
        if self._built:
            return

        with dpg.group(parent=parent, tag=self.TAG_GROUP):
            dpg.add_text("Heartbeat Counting Game", color=(100, 180, 255))
            dpg.add_separator()

            with dpg.group(horizontal=True):
                dpg.add_button(label="Start", tag=self.TAG_BTN,
                               callback=self._on_btn_click, width=100)
                dpg.add_input_int(label="", tag=self.TAG_INPUT,
                                  default_value=0, width=80, enabled=False)
                dpg.add_button(label="Submit", tag=self.TAG_SUBMIT,
                               callback=self._on_submit, width=80, enabled=False)
                dpg.add_spacer(width=10)
                dpg.add_text("Ready", tag=self.TAG_STATUS,
                             color=(150, 150, 150))

            dpg.add_spacer(height=5)

            # Scatter chart: round # on X, BPM on Y, two series
            with dpg.plot(label="Counting Game Results", height=200,
                          width=-1, tag=self.TAG_PLOT):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Round #",
                                  tag="cg_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="BPM",
                                   tag="cg_y_axis"):
                    dpg.add_scatter_series([], [], label="Guessed BPM",
                                           tag="cg_guessed_series")
                    dpg.add_scatter_series([], [], label="Actual BPM",
                                           tag="cg_actual_series")

            dpg.add_separator()

        self._built = True
        self._load_history_into_chart()

    def destroy(self) -> None:
        """Remove the widget from the UI."""
        if self._built and dpg.does_item_exist(self.TAG_GROUP):
            dpg.delete_item(self.TAG_GROUP)
        self._built = False

    @property
    def is_built(self) -> bool:
        return self._built

    # -- per-frame update (called from UIManager main loop) --

    def tick(self) -> None:
        """Must be called every frame while counting mode is active."""
        if not self._built:
            return
        expired = self.controller.tick()
        if expired:
            # Timer just expired — switch button to "Stop" and enable input
            dpg.configure_item(self.TAG_BTN, label="Stop", enabled=False)
            dpg.configure_item(self.TAG_INPUT, enabled=True)
            dpg.configure_item(self.TAG_SUBMIT, enabled=True)
            dpg.set_value(self.TAG_STATUS, "Time's up! Enter your count.")
            dpg.configure_item(self.TAG_STATUS, color=(255, 255, 0))

    def feed_rr(self, rr_ms: float) -> None:
        """Forward an RR interval to the controller while counting."""
        self.controller.add_rr(rr_ms)

    # -- callbacks --

    def _on_btn_click(self, sender=None, app_data=None) -> None:
        if self.controller.state == "idle":
            self.controller.start_round()
            dpg.configure_item(self.TAG_BTN, label="Count...")
            dpg.configure_item(self.TAG_INPUT, enabled=False)
            dpg.set_value(self.TAG_INPUT, 0)
            dpg.configure_item(self.TAG_SUBMIT, enabled=False)
            dpg.set_value(self.TAG_STATUS, "Count your heartbeats...")
            dpg.configure_item(self.TAG_STATUS, color=(0, 255, 0))

    def _on_submit(self, sender=None, app_data=None) -> None:
        guessed = dpg.get_value(self.TAG_INPUT)
        result = self.controller.submit_guess(int(guessed))
        if result is None:
            return

        # Update status
        dpg.set_value(
            self.TAG_STATUS,
            f"Guessed {result['guessed_bpm']} BPM | "
            f"Actual {result['actual_bpm']} BPM  "
            f"({result['duration_s']:.0f}s)"
        )
        dpg.configure_item(self.TAG_STATUS, color=(100, 180, 255))

        # Reset controls
        dpg.configure_item(self.TAG_BTN, label="Start", enabled=True)
        dpg.configure_item(self.TAG_INPUT, enabled=False)
        dpg.configure_item(self.TAG_SUBMIT, enabled=False)

        # Update chart
        self._append_to_chart(result)

    # -- chart helpers --

    def _load_history_into_chart(self) -> None:
        """Load persisted history and populate chart series."""
        history = load_game_history()
        self._round_indices = []
        self._guessed_bpms = []
        self._actual_bpms = []
        for i, entry in enumerate(history, start=1):
            self._round_indices.append(i)
            self._guessed_bpms.append(entry.get("guessed_bpm", 0))
            self._actual_bpms.append(entry.get("actual_bpm", 0))
        self._refresh_chart()

    def _append_to_chart(self, entry: Dict[str, Any]) -> None:
        """Add a single new result to the chart."""
        idx = len(self._round_indices) + 1
        self._round_indices.append(idx)
        self._guessed_bpms.append(entry["guessed_bpm"])
        self._actual_bpms.append(entry["actual_bpm"])
        self._refresh_chart()

    def _refresh_chart(self) -> None:
        """Push current data to DearPyGui series."""
        if not self._built:
            return
        dpg.set_value("cg_guessed_series",
                      [list(self._round_indices), list(self._guessed_bpms)])
        dpg.set_value("cg_actual_series",
                      [list(self._round_indices), list(self._actual_bpms)])
        if self._round_indices:
            dpg.fit_axis_data("cg_x_axis")
            dpg.fit_axis_data("cg_y_axis")
