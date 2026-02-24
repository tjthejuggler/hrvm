"""
Juggling Counter app for the HRV Biofeedback GUI.

Uses accelerometer data from one or both TicWatches to count juggling catches.
Each time the ACC magnitude exceeds a configurable threshold (with a cooldown),
a catch is registered. Works with left watch only, right watch only, or both.

Settings (configurable in the UI):
  - threshold:        ACC magnitude required to register a catch (m/s² units)
  - cooldown_seconds: minimum time between catches per hand
"""
import math
import time
import logging
import dearpygui.dearpygui as dpg
from collections import deque
from typing import List, Optional

from src.ble.ticwatch_manager import TicWatchSample

logger = logging.getLogger(__name__)

# Smoothing window size (number of samples for moving average)
_HISTORY_SIZE = 3


class HandCounter:
    """Counts juggling catches for a single hand (one TicWatch).

    Applies a moving-average smoothing to the ACC magnitude, then uses
    peak detection with a cooldown to count catches.
    """

    def __init__(self, threshold: float = 15.0, cooldown_seconds: float = 0.3):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.catch_count: int = 0
        self._last_catch_time: float = 0.0
        self._history: deque = deque(maxlen=_HISTORY_SIZE)

    def reset(self) -> None:
        self.catch_count = 0
        self._last_catch_time = 0.0
        self._history.clear()

    def process(self, x: float, y: float, z: float,
                timestamp: Optional[float] = None) -> bool:
        """Feed one ACC sample. Returns True if a new catch was detected."""
        if timestamp is None:
            timestamp = time.time()

        magnitude = math.sqrt(x * x + y * y + z * z)
        self._history.append(magnitude)
        smoothed = sum(self._history) / len(self._history)

        if smoothed > self.threshold:
            if (timestamp - self._last_catch_time) > self.cooldown_seconds:
                self.catch_count += 1
                self._last_catch_time = timestamp
                return True
        return False


class JugglingWidget:
    """DearPyGui widget for the Juggling Counter app.

    Placed inside the Apps collapsible header as a tree node.
    Displays:
      - Settings: threshold and cooldown sliders
      - Live catch count (left, right, total)
      - Start / Stop / Reset controls
    """

    TAG_PREFIX = "jug_"

    def __init__(self):
        self._built = False
        self._running = False

        # Per-hand counters (settings shared)
        self._threshold: float = 15.0
        self._cooldown: float = 0.3
        self._left = HandCounter(self._threshold, self._cooldown)
        self._right = HandCounter(self._threshold, self._cooldown)

        # Track which hands have received data this session
        self._left_active = False
        self._right_active = False

    # ------------------------------------------------------------------
    # Tag helper
    # ------------------------------------------------------------------

    def _t(self, suffix: str) -> str:
        return f"{self.TAG_PREFIX}{suffix}"

    # ------------------------------------------------------------------
    # Build / Destroy
    # ------------------------------------------------------------------

    def build(self, parent: str) -> None:
        if self._built:
            return

        with dpg.tree_node(label="Juggling Counter", parent=parent,
                           tag=self._t("node"), default_open=True):

            dpg.add_text("Juggling Counter", color=(100, 220, 140))
            dpg.add_separator()

            # --- Settings ---
            dpg.add_text("Settings", color=(180, 180, 180))
            with dpg.group(horizontal=True):
                dpg.add_text("Threshold (m/s²):", color=(200, 200, 200))
                dpg.add_slider_float(
                    tag=self._t("threshold_slider"),
                    default_value=self._threshold,
                    min_value=5.0, max_value=50.0,
                    width=200,
                    callback=self._on_settings_changed,
                    format="%.1f",
                )
                dpg.add_text("", tag=self._t("threshold_val"),
                             color=(255, 220, 100))

            with dpg.group(horizontal=True):
                dpg.add_text("Cooldown (s):      ", color=(200, 200, 200))
                dpg.add_slider_float(
                    tag=self._t("cooldown_slider"),
                    default_value=self._cooldown,
                    min_value=0.05, max_value=1.0,
                    width=200,
                    callback=self._on_settings_changed,
                    format="%.2f",
                )
                dpg.add_text("", tag=self._t("cooldown_val"),
                             color=(255, 220, 100))

            dpg.add_spacer(height=6)
            dpg.add_separator()

            # --- Controls ---
            with dpg.group(horizontal=True):
                dpg.add_button(label="Start", tag=self._t("start_btn"),
                               callback=self._on_start, width=80)
                dpg.add_button(label="Stop", tag=self._t("stop_btn"),
                               callback=self._on_stop, width=80,
                               enabled=False)
                dpg.add_button(label="Reset", tag=self._t("reset_btn"),
                               callback=self._on_reset, width=80)
                dpg.add_spacer(width=20)
                dpg.add_text("Stopped", tag=self._t("status"),
                             color=(150, 150, 150))

            dpg.add_spacer(height=8)

            # --- Catch counts ---
            with dpg.group(horizontal=True):
                # Left hand
                with dpg.group():
                    dpg.add_text("LEFT HAND", color=(100, 180, 255))
                    dpg.add_text("0", tag=self._t("left_count"),
                                 color=(100, 180, 255))
                    dpg.bind_item_font(self._t("left_count"), "large_font")

                dpg.add_spacer(width=60)

                # Right hand
                with dpg.group():
                    dpg.add_text("RIGHT HAND", color=(255, 140, 80))
                    dpg.add_text("0", tag=self._t("right_count"),
                                 color=(255, 140, 80))
                    dpg.bind_item_font(self._t("right_count"), "large_font")

                dpg.add_spacer(width=60)

                # Total
                with dpg.group():
                    dpg.add_text("TOTAL", color=(100, 255, 180))
                    dpg.add_text("0", tag=self._t("total_count"),
                                 color=(100, 255, 180))
                    dpg.bind_item_font(self._t("total_count"), "large_font")

            dpg.add_spacer(height=4)
            dpg.add_text("Connect at least one TicWatch to begin.",
                         tag=self._t("hint"), color=(120, 120, 120))

            dpg.add_separator()

        self._built = True
        self._refresh_settings_labels()

    def destroy(self) -> None:
        if self._built and dpg.does_item_exist(self._t("node")):
            dpg.delete_item(self._t("node"))
        self._built = False

    @property
    def is_built(self) -> bool:
        return self._built

    # ------------------------------------------------------------------
    # Per-frame tick (called from UIManager main loop)
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Update UI each frame. No heavy work here."""
        # Nothing time-sensitive needed; counts are updated in feed_* methods.

    # ------------------------------------------------------------------
    # Data feed — called from UIManager._poll_ticwatch()
    # ------------------------------------------------------------------

    def feed_left(self, samples: List[TicWatchSample]) -> None:
        """Feed ACC samples from the left TicWatch."""
        if not self._running or not self._built:
            return
        caught = False
        for s in samples:
            if s.sensor != "acc":
                continue
            self._left_active = True
            if self._left.process(s.x, s.y, s.z, s.pc_timestamp):
                caught = True
        if caught:
            self._update_display()

    def feed_right(self, samples: List[TicWatchSample]) -> None:
        """Feed ACC samples from the right TicWatch."""
        if not self._running or not self._built:
            return
        caught = False
        for s in samples:
            if s.sensor != "acc":
                continue
            self._right_active = True
            if self._right.process(s.x, s.y, s.z, s.pc_timestamp):
                caught = True
        if caught:
            self._update_display()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_display(self) -> None:
        if not self._built:
            return
        dpg.set_value(self._t("left_count"), str(self._left.catch_count))
        dpg.set_value(self._t("right_count"), str(self._right.catch_count))
        total = self._left.catch_count + self._right.catch_count
        dpg.set_value(self._t("total_count"), str(total))

    def _refresh_settings_labels(self) -> None:
        if not self._built:
            return
        dpg.set_value(self._t("threshold_val"), f"{self._threshold:.1f}")
        dpg.set_value(self._t("cooldown_val"), f"{self._cooldown:.2f}s")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_settings_changed(self, sender=None, app_data=None) -> None:
        self._threshold = dpg.get_value(self._t("threshold_slider"))
        self._cooldown = dpg.get_value(self._t("cooldown_slider"))
        self._left.threshold = self._threshold
        self._left.cooldown_seconds = self._cooldown
        self._right.threshold = self._threshold
        self._right.cooldown_seconds = self._cooldown
        self._refresh_settings_labels()

    def _on_start(self, sender=None, app_data=None) -> None:
        self._running = True
        dpg.configure_item(self._t("start_btn"), enabled=False)
        dpg.configure_item(self._t("stop_btn"), enabled=True)
        dpg.set_value(self._t("status"), "Counting…")
        dpg.configure_item(self._t("status"), color=(0, 255, 0))
        dpg.set_value(self._t("hint"), "")
        logger.info(
            f"Juggling counter started — threshold={self._threshold:.1f}, "
            f"cooldown={self._cooldown:.2f}s"
        )

    def _on_stop(self, sender=None, app_data=None) -> None:
        self._running = False
        dpg.configure_item(self._t("start_btn"), enabled=True)
        dpg.configure_item(self._t("stop_btn"), enabled=False)
        total = self._left.catch_count + self._right.catch_count
        dpg.set_value(self._t("status"), f"Stopped — {total} catches")
        dpg.configure_item(self._t("status"), color=(150, 150, 150))
        logger.info(
            f"Juggling counter stopped — L={self._left.catch_count}, "
            f"R={self._right.catch_count}, total={total}"
        )

    def _on_reset(self, sender=None, app_data=None) -> None:
        self._left.reset()
        self._right.reset()
        self._left_active = False
        self._right_active = False
        self._update_display()
        if not self._running:
            dpg.set_value(self._t("status"), "Stopped")
            dpg.configure_item(self._t("status"), color=(150, 150, 150))
        logger.info("Juggling counter reset")
