"""
Rapid Change Game module for the HRV Biofeedback GUI.

The user configures a start HR and end HR, selects a mode (one_way or return),
then races to change their heart rate as fast as possible. Results are persisted
to a JSON file and displayed on a bar chart showing completion times for games
played with the exact same configuration.

Settings include:
  - mode: one_way | return
  - start_hr / end_hr (or peak HR in return mode)
  - breathing_only: if checked, only games with breathing_only=True are shown in the chart

Session-end guards:
  - All threshold crossings require 5 consecutive readings beyond the target
    before the condition is considered met, to avoid false triggers from noisy data.
"""
import json
import os
import time
import logging
import dearpygui.dearpygui as dpg
from typing import List, Dict, Any, Optional, Callable
from src.gui.audio_feedback import play_end_beep, play_peak_beep

logger = logging.getLogger(__name__)

# --- Persistent data store (JSON file) ---

RC_DATA_FILE = "rapid_change_data.json"

# Number of consecutive readings required to confirm a threshold crossing
CONFIRM_READINGS = 5


def load_rc_history(filepath: str = RC_DATA_FILE) -> List[Dict[str, Any]]:
    """Load rapid change game history from JSON file."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load rapid change data: {e}")
        return []


def save_rc_entry(entry: Dict[str, Any], filepath: str = RC_DATA_FILE) -> None:
    """Append a single game entry to the JSON file."""
    history = load_rc_history(filepath)
    history.append(entry)
    try:
        with open(filepath, "w") as f:
            json.dump(history, f, indent=2)
    except IOError as e:
        logger.error(f"Failed to save rapid change data: {e}")


def config_key(mode: str, start_hr: int, end_hr: int, breathing_only: bool) -> str:
    """Create a unique string key for a game configuration."""
    return f"{mode}|{start_hr}|{end_hr}|{int(breathing_only)}"


def get_unique_configs(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract unique game configurations from history (only configs with finished games)."""
    seen = set()
    configs = []
    for entry in history:
        bo = entry.get("breathing_only", False)
        key = config_key(entry["mode"], entry["start_hr"], entry["end_hr"], bo)
        if key not in seen:
            seen.add(key)
            configs.append({
                "mode": entry["mode"],
                "start_hr": entry["start_hr"],
                "end_hr": entry["end_hr"],
                "breathing_only": bo,
            })
    return configs


def get_times_for_config(history: List[Dict[str, Any]], mode: str,
                         start_hr: int, end_hr: int,
                         breathing_only: bool) -> List[float]:
    """Get all completion times for a specific configuration."""
    times = []
    for entry in history:
        entry_bo = entry.get("breathing_only", False)
        if (entry["mode"] == mode and entry["start_hr"] == start_hr
                and entry["end_hr"] == end_hr and entry_bo == breathing_only):
            times.append(entry["elapsed_s"])
    return times


# --- Rapid Change Game Controller ---

class RapidChangeController:
    """Manages the state machine for a rapid change game round.

    States:
        idle      – waiting for user to configure and press Start
        waiting   – start pressed, waiting for HR to be in valid range
        racing    – timer running, user is trying to reach target HR
        returning – (return mode only) user reached peak, now returning to start
        finished  – round complete, showing results

    All threshold crossings require CONFIRM_READINGS consecutive readings
    beyond the target before the condition is confirmed, preventing false
    triggers from noisy sensor data.
    """

    def __init__(self):
        self.state = "idle"  # idle | waiting | racing | returning | finished
        self.mode = "one_way"  # one_way | return
        self.start_hr: int = 60
        self.end_hr: int = 100
        self.breathing_only: bool = False
        self._start_time: float = 0.0
        self._peak_reached_time: float = 0.0
        self._current_hr: float = 0.0
        self._direction: str = "up"  # up | down (derived from start/end)
        self._peak_reached: bool = False

        # Consecutive-reading counters for each threshold guard
        self._end_confirm_count: int = 0    # one_way: readings past end_hr
        self._peak_confirm_count: int = 0   # return racing: readings past peak
        self._return_confirm_count: int = 0 # return returning: readings past start

    def configure(self, mode: str, start_hr: int, end_hr: int,
                  breathing_only: bool = False) -> None:
        """Set game configuration."""
        self.mode = mode
        self.start_hr = start_hr
        self.end_hr = end_hr
        self.breathing_only = breathing_only
        if mode == "return":
            # In return mode, end_hr is the peak; direction is always up then down
            self._direction = "up"
        else:
            self._direction = "up" if end_hr > start_hr else "down"

    def validate_config(self) -> tuple:
        """Validate the current configuration.
        Returns (valid: bool, reason: str)."""
        if self.start_hr == self.end_hr:
            return False, "Start HR and End HR must be different"
        if self.mode == "return" and self.end_hr <= self.start_hr:
            return False, "Peak HR must be higher than Start HR in return mode"
        return True, ""

    def can_start(self) -> tuple:
        """Check if the game can start given current HR.
        Returns (can_start: bool, reason: str)."""
        # First validate config
        valid, reason = self.validate_config()
        if not valid:
            return False, reason

        if self._current_hr <= 0:
            return False, "No heart rate detected"

        if self.mode == "one_way":
            if self._direction == "up":
                # Going up: current HR must be at or below start_hr
                if self._current_hr > self.start_hr:
                    return False, f"HR ({self._current_hr:.0f}) must be ≤ {self.start_hr} to start"
                return True, "Ready"
            else:
                # Going down: current HR must be at or above start_hr
                if self._current_hr < self.start_hr:
                    return False, f"HR ({self._current_hr:.0f}) must be ≥ {self.start_hr} to start"
                return True, "Ready"
        else:
            # Return mode: start at start_hr, go up to peak, come back
            # Current HR must be at or below start_hr
            if self._current_hr > self.start_hr:
                return False, f"HR ({self._current_hr:.0f}) must be ≤ {self.start_hr} to start"
            return True, "Ready"

    def start_round(self) -> bool:
        """Begin a new round. Returns True if started successfully."""
        can, reason = self.can_start()
        if not can:
            return False
        self._start_time = time.time()
        self._peak_reached = False
        self._peak_reached_time = 0.0
        # Reset all confirmation counters
        self._end_confirm_count = 0
        self._peak_confirm_count = 0
        self._return_confirm_count = 0
        self.state = "racing"
        return True

    def update_hr(self, hr: float) -> None:
        """Feed current heart rate value."""
        self._current_hr = hr

    def tick(self) -> Optional[Dict[str, Any]]:
        """Called every frame. Returns result dict when game completes, else None."""
        if self.state == "racing":
            if self.mode == "one_way":
                return self._tick_one_way()
            else:
                return self._tick_return_racing()
        elif self.state == "returning":
            return self._tick_return_returning()
        return None

    def _tick_one_way(self) -> Optional[Dict[str, Any]]:
        """Check if target HR reached in one_way mode.

        Requires CONFIRM_READINGS consecutive readings beyond end_hr.
        """
        beyond = (
            (self._direction == "up" and self._current_hr >= self.end_hr) or
            (self._direction == "down" and self._current_hr <= self.end_hr)
        )
        if beyond:
            self._end_confirm_count += 1
            if self._end_confirm_count >= CONFIRM_READINGS:
                return self._finish()
        else:
            self._end_confirm_count = 0
        return None

    def _tick_return_racing(self) -> Optional[Dict[str, Any]]:
        """Check if peak HR reached in return mode.

        Requires CONFIRM_READINGS consecutive readings at or above end_hr.
        """
        if self._current_hr >= self.end_hr:
            self._peak_confirm_count += 1
            if self._peak_confirm_count >= CONFIRM_READINGS:
                self._peak_reached = True
                self._peak_reached_time = time.time()
                self._return_confirm_count = 0
                self.state = "returning"
                play_peak_beep()
        else:
            self._peak_confirm_count = 0
        return None

    def _tick_return_returning(self) -> Optional[Dict[str, Any]]:
        """Check if returned to start HR in return mode.

        Requires CONFIRM_READINGS consecutive readings at or below start_hr.
        """
        if self._current_hr <= self.start_hr:
            self._return_confirm_count += 1
            if self._return_confirm_count >= CONFIRM_READINGS:
                return self._finish()
        else:
            self._return_confirm_count = 0
        return None

    def _finish(self) -> Dict[str, Any]:
        """Complete the round and return results."""
        elapsed = time.time() - self._start_time
        entry = {
            "timestamp": time.time(),
            "mode": self.mode,
            "start_hr": self.start_hr,
            "end_hr": self.end_hr,
            "breathing_only": self.breathing_only,
            "elapsed_s": round(elapsed, 2),
            "direction": self._direction,
        }
        if self.mode == "return" and self._peak_reached_time > 0:
            entry["time_to_peak_s"] = round(self._peak_reached_time - self._start_time, 2)
            entry["time_from_peak_s"] = round(time.time() - self._peak_reached_time, 2)

        save_rc_entry(entry)
        self.state = "finished"
        play_end_beep()
        return entry

    def cancel(self) -> None:
        """Cancel the current round without saving."""
        self.state = "idle"

    def reset(self) -> None:
        """Reset to idle after viewing results."""
        self.state = "idle"

    def get_elapsed(self) -> float:
        """Get elapsed time since round started."""
        if self.state in ("racing", "returning"):
            return time.time() - self._start_time
        return 0.0

    @property
    def current_hr(self) -> float:
        return self._current_hr


# --- DearPyGui Widget ---

class RapidChangeWidget:
    """Builds and manages the Rapid Change game UI section.

    Placed inside the Apps collapsible header, as a collapsible tree node.
    Contains:
      - Configuration controls (mode, start HR, end HR, breathing_only)
      - Start / Cancel button
      - Live status display
      - Bar chart of completion times for current config
      - Clickable list of past game configurations with hover tooltips
    """

    TAG_PREFIX = "rc_"

    def __init__(self):
        self.controller = RapidChangeController()
        self._built = False
        self._history: List[Dict[str, Any]] = []
        # Chart data for current config
        self._bar_indices: List[float] = []
        self._bar_times: List[float] = []
        # Current config for filtering
        self._current_mode = "one_way"
        self._current_start_hr = 60
        self._current_end_hr = 100
        self._current_breathing_only = False
        # Track unique tag IDs for past config buttons
        self._config_button_tags: List[str] = []

    def _tag(self, suffix: str) -> str:
        return f"{self.TAG_PREFIX}{suffix}"

    def build(self, parent: str) -> None:
        """Create the widget tree inside *parent*."""
        if self._built:
            return

        with dpg.tree_node(label="Rapid Change", parent=parent,
                           tag=self._tag("node"), default_open=True):
            dpg.add_text("Rapid Change Game", color=(255, 140, 50))
            dpg.add_separator()

            # --- Configuration Section ---
            with dpg.group(horizontal=True):
                dpg.add_text("Mode:")
                dpg.add_radio_button(
                    items=["one_way", "return"],
                    tag=self._tag("mode_radio"),
                    default_value="one_way",
                    horizontal=True,
                    callback=self._on_config_changed
                )

            with dpg.group(horizontal=True):
                dpg.add_text("Start HR:")
                dpg.add_input_int(
                    tag=self._tag("start_hr"),
                    default_value=60, min_value=40, max_value=200,
                    min_clamped=True, max_clamped=True,
                    width=120, callback=self._on_config_changed
                )
                dpg.add_spacer(width=20)
                dpg.add_text("End HR:", tag=self._tag("end_hr_label"))
                dpg.add_input_int(
                    tag=self._tag("end_hr"),
                    default_value=100, min_value=40, max_value=200,
                    min_clamped=True, max_clamped=True,
                    width=120, callback=self._on_config_changed
                )

            dpg.add_spacer(height=4)

            # --- Breathing Only setting ---
            dpg.add_checkbox(
                label="Breathing Only",
                tag=self._tag("breathing_only"),
                default_value=False,
                callback=self._on_config_changed
            )

            dpg.add_spacer(height=5)

            # --- Controls ---
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Start", tag=self._tag("start_btn"),
                    callback=self._on_start, width=100
                )
                dpg.add_button(
                    label="Cancel", tag=self._tag("cancel_btn"),
                    callback=self._on_cancel, width=80, show=False
                )
                dpg.add_spacer(width=20)
                dpg.add_text("Idle", tag=self._tag("status"),
                             color=(150, 150, 150))

            dpg.add_spacer(height=3)
            dpg.add_text("", tag=self._tag("timer_display"),
                         color=(255, 255, 0))

            dpg.add_spacer(height=5)
            dpg.add_separator()

            # --- Results Bar Chart ---
            dpg.add_text("Completion Times (current config)", color=(180, 180, 180))
            with dpg.plot(label="##rc_results_plot", height=180, width=-1,
                          tag=self._tag("plot"), no_title=True):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Game #",
                                  tag=self._tag("x_axis"))
                with dpg.plot_axis(dpg.mvYAxis, label="Time (s)",
                                   tag=self._tag("y_axis")):
                    dpg.add_bar_series([], [], label="Elapsed Time",
                                       tag=self._tag("bar_series"), weight=0.6)

            dpg.add_spacer(height=5)
            dpg.add_separator()

            # --- Past Configurations ---
            dpg.add_text("Past Configurations", color=(180, 180, 180))
            with dpg.group(tag=self._tag("past_configs_group")):
                pass  # Will be populated dynamically

            dpg.add_separator()

        self._built = True
        self._load_history()
        self._update_end_hr_label()
        self._refresh_chart()
        self._refresh_past_configs()

    def destroy(self) -> None:
        """Remove the widget from the UI."""
        if self._built and dpg.does_item_exist(self._tag("node")):
            dpg.delete_item(self._tag("node"))
        self._built = False

    @property
    def is_built(self) -> bool:
        return self._built

    # -- per-frame update --

    def tick(self) -> None:
        """Must be called every frame."""
        if not self._built:
            return

        result = self.controller.tick()

        # Update timer display while racing/returning
        if self.controller.state in ("racing", "returning"):
            elapsed = self.controller.get_elapsed()
            phase = "Racing" if self.controller.state == "racing" else "Returning"
            if self.controller.mode == "return" and self.controller.state == "returning":
                phase = "Returning to start"
            dpg.set_value(self._tag("timer_display"),
                          f"{phase}: {elapsed:.1f}s  |  HR: {self.controller.current_hr:.0f}")

        # Update can-start status when idle
        if self.controller.state == "idle":
            can, reason = self.controller.can_start()
            if can:
                dpg.configure_item(self._tag("start_btn"), enabled=True)
                dpg.set_value(self._tag("status"), f"Ready (HR: {self.controller.current_hr:.0f})")
                dpg.configure_item(self._tag("status"), color=(0, 255, 0))
            else:
                dpg.configure_item(self._tag("start_btn"), enabled=True)  # Keep enabled, check on click
                dpg.set_value(self._tag("status"), reason)
                dpg.configure_item(self._tag("status"), color=(255, 165, 0))

        if result is not None:
            self._on_game_finished(result)

    def feed_hr(self, hr: float) -> None:
        """Feed current heart rate to the controller."""
        self.controller.update_hr(hr)

    # -- callbacks --

    def _on_config_changed(self, sender=None, app_data=None) -> None:
        """Called when any config control changes."""
        self._current_mode = dpg.get_value(self._tag("mode_radio"))
        self._current_start_hr = dpg.get_value(self._tag("start_hr"))
        self._current_end_hr = dpg.get_value(self._tag("end_hr"))
        self._current_breathing_only = dpg.get_value(self._tag("breathing_only"))

        self.controller.configure(
            self._current_mode, self._current_start_hr, self._current_end_hr,
            self._current_breathing_only
        )
        self._update_end_hr_label()
        self._refresh_chart()

    def _update_end_hr_label(self) -> None:
        """Update the end HR label based on mode."""
        if self._current_mode == "return":
            dpg.set_value(self._tag("end_hr_label"), "Peak HR:")
        else:
            dpg.set_value(self._tag("end_hr_label"), "End HR:")

    def _on_start(self, sender=None, app_data=None) -> None:
        """Start button clicked."""
        # Read current config
        self._on_config_changed()

        can, reason = self.controller.can_start()
        if not can:
            dpg.set_value(self._tag("status"), reason)
            dpg.configure_item(self._tag("status"), color=(255, 0, 0))
            return

        started = self.controller.start_round()
        if started:
            dpg.configure_item(self._tag("start_btn"), show=False)
            dpg.configure_item(self._tag("cancel_btn"), show=True)
            # Disable config controls during game
            dpg.configure_item(self._tag("mode_radio"), enabled=False)
            dpg.configure_item(self._tag("start_hr"), enabled=False)
            dpg.configure_item(self._tag("end_hr"), enabled=False)
            dpg.configure_item(self._tag("breathing_only"), enabled=False)

            if self.controller.mode == "return":
                dpg.set_value(self._tag("status"),
                              f"Race to {self.controller.end_hr} BPM then back to {self.controller.start_hr}!")
            else:
                dpg.set_value(self._tag("status"),
                              f"Race to {self.controller.end_hr} BPM!")
            dpg.configure_item(self._tag("status"), color=(0, 255, 0))

    def _on_cancel(self, sender=None, app_data=None) -> None:
        """Cancel button clicked."""
        self.controller.cancel()
        self._reset_controls()
        dpg.set_value(self._tag("status"), "Cancelled")
        dpg.configure_item(self._tag("status"), color=(255, 100, 100))
        dpg.set_value(self._tag("timer_display"), "")

    def _on_game_finished(self, result: Dict[str, Any]) -> None:
        """Called when a game round completes."""
        elapsed = result["elapsed_s"]
        msg = f"Finished in {elapsed:.2f}s!"
        if result["mode"] == "return" and "time_to_peak_s" in result:
            msg += f"  (↑{result['time_to_peak_s']:.1f}s  ↓{result['time_from_peak_s']:.1f}s)"

        dpg.set_value(self._tag("status"), msg)
        dpg.configure_item(self._tag("status"), color=(100, 255, 100))
        dpg.set_value(self._tag("timer_display"), "")

        self._reset_controls()
        self.controller.reset()

        # Reload history and refresh
        self._load_history()
        self._refresh_chart()
        self._refresh_past_configs()

    def _reset_controls(self) -> None:
        """Reset UI controls to idle state."""
        dpg.configure_item(self._tag("start_btn"), show=True)
        dpg.configure_item(self._tag("cancel_btn"), show=False)
        dpg.configure_item(self._tag("mode_radio"), enabled=True)
        dpg.configure_item(self._tag("start_hr"), enabled=True)
        dpg.configure_item(self._tag("end_hr"), enabled=True)
        dpg.configure_item(self._tag("breathing_only"), enabled=True)

    # -- past config click --

    def _on_past_config_click(self, sender, app_data, user_data) -> None:
        """User clicked a past configuration — load it."""
        cfg = user_data
        dpg.set_value(self._tag("mode_radio"), cfg["mode"])
        dpg.set_value(self._tag("start_hr"), cfg["start_hr"])
        dpg.set_value(self._tag("end_hr"), cfg["end_hr"])
        dpg.set_value(self._tag("breathing_only"), cfg.get("breathing_only", False))
        self._on_config_changed()

    # -- data helpers --

    def _load_history(self) -> None:
        """Load persisted history."""
        self._history = load_rc_history()

    def _refresh_chart(self) -> None:
        """Update bar chart for current config."""
        if not self._built:
            return

        times = get_times_for_config(
            self._history, self._current_mode,
            self._current_start_hr, self._current_end_hr,
            self._current_breathing_only
        )

        self._bar_indices = [float(i + 1) for i in range(len(times))]
        self._bar_times = times

        dpg.set_value(self._tag("bar_series"),
                      [self._bar_indices, self._bar_times])

        if self._bar_indices:
            dpg.fit_axis_data(self._tag("x_axis"))
            dpg.fit_axis_data(self._tag("y_axis"))

    def _refresh_past_configs(self) -> None:
        """Rebuild the clickable list of past configurations."""
        if not self._built:
            return

        group_tag = self._tag("past_configs_group")

        # Delete old buttons
        for tag in self._config_button_tags:
            if dpg.does_item_exist(tag):
                dpg.delete_item(tag)
        self._config_button_tags.clear()

        configs = get_unique_configs(self._history)

        if not configs:
            no_tag = self._tag("no_past_configs")
            if not dpg.does_item_exist(no_tag):
                dpg.add_text("No past games yet.", parent=group_tag,
                             tag=no_tag, color=(120, 120, 120))
            self._config_button_tags.append(no_tag)
            return

        for i, cfg in enumerate(configs):
            btn_tag = self._tag(f"past_cfg_{i}")
            mode_label = cfg["mode"].replace("_", " ").title()
            if cfg["mode"] == "return":
                label = f"{mode_label}: {cfg['start_hr']} → {cfg['end_hr']} → {cfg['start_hr']}"
            else:
                label = f"{mode_label}: {cfg['start_hr']} → {cfg['end_hr']}"

            if cfg.get("breathing_only", False):
                label += "  [Breathing Only]"

            # Count games for this config
            count = len(get_times_for_config(
                self._history, cfg["mode"], cfg["start_hr"], cfg["end_hr"],
                cfg.get("breathing_only", False)
            ))
            label += f"  ({count} games)"

            dpg.add_button(
                label=label, parent=group_tag, tag=btn_tag,
                callback=self._on_past_config_click,
                user_data=cfg, width=-1
            )

            # Add tooltip on hover with full config details
            with dpg.tooltip(parent=btn_tag):
                dpg.add_text(f"Mode: {cfg['mode']}")
                dpg.add_text(f"Start HR: {cfg['start_hr']} BPM")
                if cfg["mode"] == "return":
                    dpg.add_text(f"Peak HR: {cfg['end_hr']} BPM")
                    dpg.add_text(f"Return to: {cfg['start_hr']} BPM")
                else:
                    dpg.add_text(f"End HR: {cfg['end_hr']} BPM")
                    direction = "Increase" if cfg["end_hr"] > cfg["start_hr"] else "Decrease"
                    dpg.add_text(f"Direction: {direction}")
                dpg.add_text(f"Breathing Only: {'Yes' if cfg.get('breathing_only', False) else 'No'}")
                dpg.add_text(f"Games played: {count}")
                # Show best time
                times = get_times_for_config(
                    self._history, cfg["mode"], cfg["start_hr"], cfg["end_hr"],
                    cfg.get("breathing_only", False)
                )
                if times:
                    dpg.add_text(f"Best time: {min(times):.2f}s")
                    dpg.add_text(f"Avg time: {sum(times)/len(times):.2f}s")

            self._config_button_tags.append(btn_tag)
