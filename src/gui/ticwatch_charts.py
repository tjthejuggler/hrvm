"""TicWatch IMU chart widgets for the HRV Biofeedback GUI.

Provides DearPyGui chart widgets for displaying real-time IMU data from
TicWatch Left and TicWatch Right:
  - Accelerometer (X, Y, Z)
  - Gyroscope (X, Y, Z)
  - Magnetometer (X, Y, Z)

Each chart class is parameterised by a tag_prefix so Left and Right watches
can coexist without DPG tag collisions.

Follows the same pattern used by genki_charts.py and pvs_charts.py.
"""

import dearpygui.dearpygui as dpg
from collections import deque
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from src.ble.ticwatch_manager import TicWatchSample


class _IMUXYZChart:
    """Base class for a 3-axis (X/Y/Z) time-series chart."""

    def __init__(self, label: str, tag_prefix: str, y_label: str,
                 buffer_size: int = 2000):
        self.label = label
        self.tag_prefix = tag_prefix
        self.y_label = y_label
        self.buffer_size = buffer_size
        self.ts: deque = deque(maxlen=buffer_size)
        self.x:  deque = deque(maxlen=buffer_size)
        self.y:  deque = deque(maxlen=buffer_size)
        self.z:  deque = deque(maxlen=buffer_size)
        self._start_time: float = 0.0

    def build(self, parent: str):
        with dpg.tree_node(label=self.label, parent=parent,
                           tag=f"{self.tag_prefix}_node", default_open=True):
            with dpg.plot(label=self.label, height=200, width=-1,
                          tag=f"{self.tag_prefix}_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)",
                                  tag=f"{self.tag_prefix}_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label=self.y_label,
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_line_series([], [], label="X",
                                        tag=f"{self.tag_prefix}_x_series")
                    dpg.add_line_series([], [], label="Y",
                                        tag=f"{self.tag_prefix}_y_series")
                    dpg.add_line_series([], [], label="Z",
                                        tag=f"{self.tag_prefix}_z_series")

    def _add(self, sample: "TicWatchSample"):
        if not self._start_time:
            self._start_time = sample.timestamp
        t = sample.timestamp - self._start_time
        self.ts.append(t)
        self.x.append(sample.x)
        self.y.append(sample.y)
        self.z.append(sample.z)

    def update_plot(self):
        if not self.ts:
            return
        t = list(self.ts)
        dpg.set_value(f"{self.tag_prefix}_x_series", [t, list(self.x)])
        dpg.set_value(f"{self.tag_prefix}_y_series", [t, list(self.y)])
        dpg.set_value(f"{self.tag_prefix}_z_series", [t, list(self.z)])
        latest = t[-1]
        dpg.set_axis_limits(f"{self.tag_prefix}_x_axis",
                            max(0.0, latest - 20.0), latest + 1.0)
        dpg.fit_axis_data(f"{self.tag_prefix}_y_axis")


# ---------------------------------------------------------------------------
# TicWatch Left charts
# ---------------------------------------------------------------------------

class TicWatchLeftAccChart(_IMUXYZChart):
    """Accelerometer chart for TicWatch Left."""

    def __init__(self, buffer_size: int = 2000):
        super().__init__(
            label="TW Left Acc (m/s²)",
            tag_prefix="tw_left_acc",
            y_label="m/s²",
            buffer_size=buffer_size,
        )

    def add_samples(self, samples: "List[TicWatchSample]"):
        for s in samples:
            if s.sensor == "acc":
                self._add(s)


class TicWatchLeftGyroChart(_IMUXYZChart):
    """Gyroscope chart for TicWatch Left."""

    def __init__(self, buffer_size: int = 2000):
        super().__init__(
            label="TW Left Gyro (rad/s)",
            tag_prefix="tw_left_gyro",
            y_label="rad/s",
            buffer_size=buffer_size,
        )

    def add_samples(self, samples: "List[TicWatchSample]"):
        for s in samples:
            if s.sensor == "gyro":
                self._add(s)


class TicWatchLeftMagChart(_IMUXYZChart):
    """Magnetometer chart for TicWatch Left."""

    def __init__(self, buffer_size: int = 2000):
        super().__init__(
            label="TW Left Mag (µT)",
            tag_prefix="tw_left_mag",
            y_label="µT",
            buffer_size=buffer_size,
        )

    def add_samples(self, samples: "List[TicWatchSample]"):
        for s in samples:
            if s.sensor == "mag":
                self._add(s)


# ---------------------------------------------------------------------------
# TicWatch Right charts
# ---------------------------------------------------------------------------

class TicWatchRightAccChart(_IMUXYZChart):
    """Accelerometer chart for TicWatch Right."""

    def __init__(self, buffer_size: int = 2000):
        super().__init__(
            label="TW Right Acc (m/s²)",
            tag_prefix="tw_right_acc",
            y_label="m/s²",
            buffer_size=buffer_size,
        )

    def add_samples(self, samples: "List[TicWatchSample]"):
        for s in samples:
            if s.sensor == "acc":
                self._add(s)


class TicWatchRightGyroChart(_IMUXYZChart):
    """Gyroscope chart for TicWatch Right."""

    def __init__(self, buffer_size: int = 2000):
        super().__init__(
            label="TW Right Gyro (rad/s)",
            tag_prefix="tw_right_gyro",
            y_label="rad/s",
            buffer_size=buffer_size,
        )

    def add_samples(self, samples: "List[TicWatchSample]"):
        for s in samples:
            if s.sensor == "gyro":
                self._add(s)


class TicWatchRightMagChart(_IMUXYZChart):
    """Magnetometer chart for TicWatch Right."""

    def __init__(self, buffer_size: int = 2000):
        super().__init__(
            label="TW Right Mag (µT)",
            tag_prefix="tw_right_mag",
            y_label="µT",
            buffer_size=buffer_size,
        )

    def add_samples(self, samples: "List[TicWatchSample]"):
        for s in samples:
            if s.sensor == "mag":
                self._add(s)
