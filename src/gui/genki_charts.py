"""Genki Wave chart widgets for the HRV Biofeedback GUI.

Provides collapsible DearPyGui chart widgets for displaying real-time
IMU data from the Genki Wave ring:
  - Gyroscope (X, Y, Z)
  - Accelerometer (X, Y, Z)
  - Gyroscope Sum (|X| + |Y| + |Z|)

Follows the same CollapsibleChart pattern used by the Polar H10 charts
in src/gui/charts.py.
"""

import dearpygui.dearpygui as dpg
from collections import deque
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from src.ble.genki_manager import WaveSample


class GenkiGyroChart:
    """Gyroscope X/Y/Z time-series chart for the Genki Wave."""

    def __init__(self, buffer_size: int = 2000):
        self.label = "Wave Gyro (deg/s)"
        self.tag_prefix = "genki_gyro"
        self.buffer_size = buffer_size
        self.ts: deque = deque(maxlen=buffer_size)
        self.x: deque = deque(maxlen=buffer_size)
        self.y: deque = deque(maxlen=buffer_size)
        self.z: deque = deque(maxlen=buffer_size)
        self._start_time: float = 0.0

    def build(self, parent: str):
        """Build the chart UI inside the given parent container."""
        with dpg.tree_node(
            label=self.label, parent=parent,
            tag=f"{self.tag_prefix}_node", default_open=True
        ):
            with dpg.plot(label=self.label, height=200, width=-1,
                          tag=f"{self.tag_prefix}_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)",
                                  tag=f"{self.tag_prefix}_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="deg/s",
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_line_series([], [], label="X",
                                        tag=f"{self.tag_prefix}_x_series")
                    dpg.add_line_series([], [], label="Y",
                                        tag=f"{self.tag_prefix}_y_series")
                    dpg.add_line_series([], [], label="Z",
                                        tag=f"{self.tag_prefix}_z_series")

    def add_samples(self, samples: 'List[WaveSample]'):
        """Add new samples to the buffer."""
        for s in samples:
            if not self._start_time:
                self._start_time = s.timestamp
            t = s.timestamp - self._start_time
            self.ts.append(t)
            self.x.append(s.gyro[0])
            self.y.append(s.gyro[1])
            self.z.append(s.gyro[2])

    def update_plot(self):
        """Push buffered data to the DearPyGui plot."""
        if not self.ts:
            return
        t = list(self.ts)
        dpg.set_value(f"{self.tag_prefix}_x_series", [t, list(self.x)])
        dpg.set_value(f"{self.tag_prefix}_y_series", [t, list(self.y)])
        dpg.set_value(f"{self.tag_prefix}_z_series", [t, list(self.z)])
        # Auto-scroll to show last 20 seconds
        latest = t[-1]
        dpg.set_axis_limits(f"{self.tag_prefix}_x_axis",
                            max(0, latest - 20), latest + 1)
        dpg.fit_axis_data(f"{self.tag_prefix}_y_axis")


class GenkiAccChart:
    """Accelerometer X/Y/Z time-series chart for the Genki Wave."""

    def __init__(self, buffer_size: int = 2000):
        self.label = "Wave Acc (g)"
        self.tag_prefix = "genki_acc"
        self.buffer_size = buffer_size
        self.ts: deque = deque(maxlen=buffer_size)
        self.x: deque = deque(maxlen=buffer_size)
        self.y: deque = deque(maxlen=buffer_size)
        self.z: deque = deque(maxlen=buffer_size)
        self._start_time: float = 0.0

    def build(self, parent: str):
        """Build the chart UI inside the given parent container."""
        with dpg.tree_node(
            label=self.label, parent=parent,
            tag=f"{self.tag_prefix}_node", default_open=True
        ):
            with dpg.plot(label=self.label, height=200, width=-1,
                          tag=f"{self.tag_prefix}_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)",
                                  tag=f"{self.tag_prefix}_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="g",
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_line_series([], [], label="X",
                                        tag=f"{self.tag_prefix}_x_series")
                    dpg.add_line_series([], [], label="Y",
                                        tag=f"{self.tag_prefix}_y_series")
                    dpg.add_line_series([], [], label="Z",
                                        tag=f"{self.tag_prefix}_z_series")

    def add_samples(self, samples: 'List[WaveSample]'):
        """Add new samples to the buffer."""
        for s in samples:
            if not self._start_time:
                self._start_time = s.timestamp
            t = s.timestamp - self._start_time
            self.ts.append(t)
            self.x.append(s.acc[0])
            self.y.append(s.acc[1])
            self.z.append(s.acc[2])

    def update_plot(self):
        """Push buffered data to the DearPyGui plot."""
        if not self.ts:
            return
        t = list(self.ts)
        dpg.set_value(f"{self.tag_prefix}_x_series", [t, list(self.x)])
        dpg.set_value(f"{self.tag_prefix}_y_series", [t, list(self.y)])
        dpg.set_value(f"{self.tag_prefix}_z_series", [t, list(self.z)])
        latest = t[-1]
        dpg.set_axis_limits(f"{self.tag_prefix}_x_axis",
                            max(0, latest - 20), latest + 1)
        dpg.fit_axis_data(f"{self.tag_prefix}_y_axis")


class GenkiGyroSumChart:
    """Gyroscope magnitude sum (|X|+|Y|+|Z|) time-series chart."""

    def __init__(self, buffer_size: int = 2000):
        self.label = "Wave Gyro Sum (deg/s)"
        self.tag_prefix = "genki_gyro_sum"
        self.buffer_size = buffer_size
        self.ts: deque = deque(maxlen=buffer_size)
        self.values: deque = deque(maxlen=buffer_size)
        self._start_time: float = 0.0

    def build(self, parent: str):
        """Build the chart UI inside the given parent container."""
        with dpg.tree_node(
            label=self.label, parent=parent,
            tag=f"{self.tag_prefix}_node", default_open=True
        ):
            with dpg.plot(label=self.label, height=200, width=-1,
                          tag=f"{self.tag_prefix}_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)",
                                  tag=f"{self.tag_prefix}_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="deg/s",
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_line_series([], [], label="Sum",
                                        tag=f"{self.tag_prefix}_series")

    def add_samples(self, samples: 'List[WaveSample]'):
        """Add new samples to the buffer."""
        for s in samples:
            if not self._start_time:
                self._start_time = s.timestamp
            t = s.timestamp - self._start_time
            self.ts.append(t)
            gyro_sum = abs(s.gyro[0]) + abs(s.gyro[1]) + abs(s.gyro[2])
            self.values.append(gyro_sum)

    def update_plot(self):
        """Push buffered data to the DearPyGui plot."""
        if not self.ts:
            return
        t = list(self.ts)
        dpg.set_value(f"{self.tag_prefix}_series", [t, list(self.values)])
        latest = t[-1]
        dpg.set_axis_limits(f"{self.tag_prefix}_x_axis",
                            max(0, latest - 20), latest + 1)
        dpg.fit_axis_data(f"{self.tag_prefix}_y_axis")
