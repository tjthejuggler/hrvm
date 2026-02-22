"""
Polar H10 IMU chart widgets — ACC (all axes + individual), ECG, and Breathing Phase.

Separated from charts.py to keep file sizes manageable.
All charts inherit CollapsibleChart.is_visible() and skip rendering
when their tree node is collapsed.
"""
import dearpygui.dearpygui as dpg
from collections import deque
from typing import List, Tuple, Optional

from src.gui.charts import CollapsibleChart

# Colour map for breathing phases
_PHASE_COLORS = {
    "INHALING": (0, 200, 255, 255),
    "EXHALING": (255, 100, 50, 255),
    "HOLDING":  (200, 200, 50, 255),
    None:       (80, 80, 80, 255),
}
# Numeric encoding for the line series (DPG needs floats)
_PHASE_VALUES = {"INHALING": 2.0, "EXHALING": 0.0, "HOLDING": 1.0}


# ---------------------------------------------------------------------------
# Polar H10 ACC Charts
# ---------------------------------------------------------------------------

class ACCChart(CollapsibleChart):
    """3-axis accelerometer chart — all axes overlaid."""

    def __init__(self):
        super().__init__("Accelerometer (IMU) — All Axes", "acc", default_open=True)
        self.max_samples = 500  # ~20 seconds at 25Hz
        self.acc_time: deque = deque(maxlen=self.max_samples)
        self.acc_x: deque = deque(maxlen=self.max_samples)
        self.acc_y: deque = deque(maxlen=self.max_samples)
        self.acc_z: deque = deque(maxlen=self.max_samples)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="Accelerometer (mG)", height=250, width=-1, tag="acc_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="acc_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="Acceleration (mG)", tag="acc_y_axis"):
                    dpg.add_line_series([], [], label="X", tag="acc_x_series")
                    dpg.add_line_series([], [], label="Y", tag="acc_y_series")
                    dpg.add_line_series([], [], label="Z", tag="acc_z_series")

    def add_samples(self, timestamp: float, samples: List[Tuple[int, int, int]],
                    sample_rate: int, start_time: float):
        base_time = timestamp - start_time
        dt = 1.0 / sample_rate if sample_rate > 0 else 0.04
        for i, (x, y, z) in enumerate(samples):
            t = base_time + i * dt
            self.acc_time.append(t)
            self.acc_x.append(x)
            self.acc_y.append(y)
            self.acc_z.append(z)

    def update_plot(self, current_time_offset: float):
        if not self.is_visible() or len(self.acc_time) == 0:
            return
        t = list(self.acc_time)
        dpg.set_value("acc_x_series", [t, list(self.acc_x)])
        dpg.set_value("acc_y_series", [t, list(self.acc_y)])
        dpg.set_value("acc_z_series", [t, list(self.acc_z)])
        dpg.set_axis_limits("acc_x_axis", max(0, current_time_offset - 20), current_time_offset + 2)
        dpg.fit_axis_data("acc_y_axis")


class ACCXChart(CollapsibleChart):
    """Accelerometer X-axis only."""

    def __init__(self):
        super().__init__("ACC — X Axis", "acc_x_only", default_open=False)
        self.max_samples = 500
        self.acc_time: deque = deque(maxlen=self.max_samples)
        self.acc_x: deque = deque(maxlen=self.max_samples)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="ACC X (mG)", height=200, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="acc_x_only_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="mG", tag="acc_x_only_y_axis"):
                    dpg.add_line_series([], [], label="X", tag="acc_x_only_series")

    def update_plot(self, current_time_offset: float):
        if not self.is_visible() or len(self.acc_time) == 0:
            return
        t = list(self.acc_time)
        dpg.set_value("acc_x_only_series", [t, list(self.acc_x)])
        dpg.set_axis_limits("acc_x_only_x_axis",
                            max(0, current_time_offset - 20), current_time_offset + 2)
        dpg.fit_axis_data("acc_x_only_y_axis")


class ACCYChart(CollapsibleChart):
    """Accelerometer Y-axis only."""

    def __init__(self):
        super().__init__("ACC — Y Axis", "acc_y_only", default_open=False)
        self.max_samples = 500
        self.acc_time: deque = deque(maxlen=self.max_samples)
        self.acc_y: deque = deque(maxlen=self.max_samples)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="ACC Y (mG)", height=200, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="acc_y_only_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="mG", tag="acc_y_only_y_axis"):
                    dpg.add_line_series([], [], label="Y", tag="acc_y_only_series")

    def update_plot(self, current_time_offset: float):
        if not self.is_visible() or len(self.acc_time) == 0:
            return
        t = list(self.acc_time)
        dpg.set_value("acc_y_only_series", [t, list(self.acc_y)])
        dpg.set_axis_limits("acc_y_only_x_axis",
                            max(0, current_time_offset - 20), current_time_offset + 2)
        dpg.fit_axis_data("acc_y_only_y_axis")


class ACCZChart(CollapsibleChart):
    """Accelerometer Z-axis only."""

    def __init__(self):
        super().__init__("ACC — Z Axis", "acc_z_only", default_open=False)
        self.max_samples = 500
        self.acc_time: deque = deque(maxlen=self.max_samples)
        self.acc_z: deque = deque(maxlen=self.max_samples)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="ACC Z (mG)", height=200, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="acc_z_only_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="mG", tag="acc_z_only_y_axis"):
                    dpg.add_line_series([], [], label="Z", tag="acc_z_only_series")

    def update_plot(self, current_time_offset: float):
        if not self.is_visible() or len(self.acc_time) == 0:
            return
        t = list(self.acc_time)
        dpg.set_value("acc_z_only_series", [t, list(self.acc_z)])
        dpg.set_axis_limits("acc_z_only_x_axis",
                            max(0, current_time_offset - 20), current_time_offset + 2)
        dpg.fit_axis_data("acc_z_only_y_axis")


# ---------------------------------------------------------------------------
# Polar H10 ECG Chart
# ---------------------------------------------------------------------------

class ECGChart(CollapsibleChart):
    """Real-time ECG waveform chart from Polar H10 PMD service (130 Hz)."""

    def __init__(self):
        super().__init__("ECG Waveform", "ecg", default_open=True)
        # ~5 seconds of ECG at 130Hz = 650 samples
        self.max_samples = 650
        self.ecg_time: deque = deque(maxlen=self.max_samples)
        self.ecg_val: deque = deque(maxlen=self.max_samples)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="ECG (µV)", height=250, width=-1, tag="ecg_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="ecg_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="Amplitude (µV)", tag="ecg_y_axis"):
                    dpg.add_line_series([], [], label="ECG", tag="ecg_series")

    def add_samples(self, timestamp: float, samples: list,
                    sample_rate: int, start_time: float):
        base_time = timestamp - start_time
        dt = 1.0 / sample_rate if sample_rate > 0 else 1.0 / 130.0
        for i, val in enumerate(samples):
            t = base_time + i * dt
            self.ecg_time.append(t)
            self.ecg_val.append(val)

    def update_plot(self, current_time_offset: float):
        if not self.is_visible() or len(self.ecg_time) == 0:
            return
        t = list(self.ecg_time)
        dpg.set_value("ecg_series", [t, list(self.ecg_val)])
        # Auto-scroll to last 5 seconds
        dpg.set_axis_limits("ecg_x_axis", max(0, current_time_offset - 5), current_time_offset + 0.5)
        dpg.fit_axis_data("ecg_y_axis")


# ---------------------------------------------------------------------------
# Breathing Phase Chart
# ---------------------------------------------------------------------------

# Rate at which the lung-fullness value rises/falls per second.
# At a typical 6 BPM (10 s cycle), inhale ≈ 5 s → needs ~0.2/s to go 0→1.
# We use 0.25/s so it reaches full range comfortably within a normal breath.
_LUNG_RATE = 0.25


class BreathingPhaseChart(CollapsibleChart):
    """Real-time breathing chart derived from ACC Z-axis respiration detection.

    Shows a "lung fullness" curve:
      - Rises during INHALING
      - Falls during EXHALING
      - Stays flat during HOLDING

    Uses the engine's *predicted_phase* (raw live prediction, no debounce lag)
    so the chart responds immediately — the same signal shown in the calibration
    popup's "System sees" feedback.

    A coloured status label above the plot shows the current phase name.
    """

    def __init__(self):
        super().__init__("Breathing (ACC)", "breath_phase", default_open=True)
        self.max_samples = 600   # ~30 s at ~20 Hz update rate
        self._time: deque = deque(maxlen=self.max_samples)
        self._lung_val: deque = deque(maxlen=self.max_samples)
        self._lung_level: float = 0.5   # start mid-range
        self._last_t: Optional[float] = None
        self._current_phase: Optional[str] = None

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.group(horizontal=True):
                dpg.add_text("Phase: ")
                dpg.add_text("---", tag="breath_phase_label",
                             color=_PHASE_COLORS[None])
            with dpg.plot(label="Lung Fullness", height=130, width=-1,
                          tag="breath_phase_plot"):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)",
                                  tag="breath_phase_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="Fullness",
                                   tag="breath_phase_y_axis"):
                    dpg.add_line_series([], [], label="Breath",
                                        tag="breath_phase_series")
                    dpg.set_axis_limits("breath_phase_y_axis", -0.05, 1.05)

    def update_phase(self, phase: Optional[str], current_time: float):
        """Called each frame with the engine's predicted_phase (live, no debounce).

        Integrates the phase into a lung-fullness value that rises on INHALING,
        falls on EXHALING, and stays flat on HOLDING/None.
        """
        self._current_phase = phase

        # Compute dt since last call
        if self._last_t is None:
            dt = 0.05  # assume ~50 ms on first call
        else:
            dt = max(0.0, current_time - self._last_t)
        self._last_t = current_time

        # Integrate lung level
        if phase == "INHALING":
            self._lung_level = min(1.0, self._lung_level + _LUNG_RATE * dt)
        elif phase == "EXHALING":
            self._lung_level = max(0.0, self._lung_level - _LUNG_RATE * dt)
        # HOLDING or None → no change

        self._time.append(current_time)
        self._lung_val.append(self._lung_level)

        # Update label
        if dpg.does_item_exist("breath_phase_label"):
            label = phase if phase else "---"
            dpg.set_value("breath_phase_label", label)
            dpg.configure_item("breath_phase_label",
                               color=_PHASE_COLORS.get(phase, _PHASE_COLORS[None]))

    def update_plot(self, current_time_offset: float):
        if not self.is_visible() or len(self._time) == 0:
            return
        t = list(self._time)
        v = list(self._lung_val)
        dpg.set_value("breath_phase_series", [t, v])
        dpg.set_axis_limits("breath_phase_x_axis",
                            max(0, current_time_offset - 30),
                            current_time_offset + 1)
