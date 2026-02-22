"""
Polar H10 IMU chart widgets — ACC (all axes + individual) and ECG.

Separated from charts.py to keep file sizes manageable.
All charts inherit CollapsibleChart.is_visible() and skip rendering
when their tree node is collapsed.
"""
import dearpygui.dearpygui as dpg
from collections import deque
from typing import List, Tuple

from src.gui.charts import CollapsibleChart


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
