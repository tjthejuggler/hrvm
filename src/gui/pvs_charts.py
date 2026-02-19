"""Polar Verity Sense chart widgets for the HRV Biofeedback GUI.

Provides collapsible DearPyGui chart widgets for displaying real-time
sensor data from the Polar Verity Sense:
  - Accelerometer (X, Y, Z) in mg
  - Gyroscope (X, Y, Z) in dps
  - PPI (Pulse-to-Pulse Interval) in ms
  - PPI Heart Rate in BPM

Follows the same CollapsibleChart pattern used by the Polar H10 charts
in src/gui/charts.py and the Genki Wave charts in src/gui/genki_charts.py.
"""

import dearpygui.dearpygui as dpg
from collections import deque
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from src.ble.pvs_manager import PVSSample


class PVSAccChart:
    """Accelerometer X/Y/Z time-series chart for the Polar Verity Sense."""

    def __init__(self, buffer_size: int = 2000):
        self.label = "PVS Acc (mg)"
        self.tag_prefix = "pvs_acc"
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
                with dpg.plot_axis(dpg.mvYAxis, label="mg",
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_line_series([], [], label="X",
                                        tag=f"{self.tag_prefix}_x_series")
                    dpg.add_line_series([], [], label="Y",
                                        tag=f"{self.tag_prefix}_y_series")
                    dpg.add_line_series([], [], label="Z",
                                        tag=f"{self.tag_prefix}_z_series")

    def add_samples(self, samples: 'List[PVSSample]'):
        """Add new ACC samples to the buffer."""
        for s in samples:
            if s.acc is None:
                continue
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


class PVSGyroChart:
    """Gyroscope X/Y/Z time-series chart for the Polar Verity Sense."""

    def __init__(self, buffer_size: int = 2000):
        self.label = "PVS Gyro (dps)"
        self.tag_prefix = "pvs_gyro"
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
                with dpg.plot_axis(dpg.mvYAxis, label="dps",
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_line_series([], [], label="X",
                                        tag=f"{self.tag_prefix}_x_series")
                    dpg.add_line_series([], [], label="Y",
                                        tag=f"{self.tag_prefix}_y_series")
                    dpg.add_line_series([], [], label="Z",
                                        tag=f"{self.tag_prefix}_z_series")

    def add_samples(self, samples: 'List[PVSSample]'):
        """Add new GYR samples to the buffer."""
        for s in samples:
            if s.gyro is None:
                continue
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
        latest = t[-1]
        dpg.set_axis_limits(f"{self.tag_prefix}_x_axis",
                            max(0, latest - 20), latest + 1)
        dpg.fit_axis_data(f"{self.tag_prefix}_y_axis")


class PVSPPIChart:
    """PPI (Pulse-to-Pulse Interval) time-series chart for the Polar Verity Sense."""

    def __init__(self, buffer_size: int = 500):
        self.label = "PVS PPI (ms)"
        self.tag_prefix = "pvs_ppi"
        self.buffer_size = buffer_size
        self.ts: deque = deque(maxlen=buffer_size)
        self.ppi_values: deque = deque(maxlen=buffer_size)
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
                with dpg.plot_axis(dpg.mvYAxis, label="PPI (ms)",
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_stem_series([], [], label="PPI",
                                        tag=f"{self.tag_prefix}_series")
                    dpg.set_axis_limits(f"{self.tag_prefix}_y_axis", 400, 1200)

    def add_samples(self, samples: 'List[PVSSample]'):
        """Add new PPI samples to the buffer."""
        for s in samples:
            if s.ppi_ms is None:
                continue
            if not self._start_time:
                self._start_time = s.timestamp
            t = s.timestamp - self._start_time
            self.ts.append(t)
            self.ppi_values.append(s.ppi_ms)

    def update_plot(self):
        """Push buffered data to the DearPyGui plot."""
        if not self.ts:
            return
        t = list(self.ts)
        dpg.set_value(f"{self.tag_prefix}_series", [t, list(self.ppi_values)])
        latest = t[-1]
        dpg.set_axis_limits(f"{self.tag_prefix}_x_axis",
                            max(0, latest - 60), latest + 5)
        dpg.fit_axis_data(f"{self.tag_prefix}_y_axis")


class PVSHeartRateChart:
    """Heart Rate from PPI data, displayed as BPM over time."""

    def __init__(self, buffer_size: int = 500):
        self.label = "PVS Heart Rate (BPM)"
        self.tag_prefix = "pvs_hr"
        self.buffer_size = buffer_size
        self.ts: deque = deque(maxlen=buffer_size)
        self.hr_values: deque = deque(maxlen=buffer_size)
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
                with dpg.plot_axis(dpg.mvYAxis, label="BPM",
                                   tag=f"{self.tag_prefix}_y_axis"):
                    dpg.add_line_series([], [], label="HR",
                                        tag=f"{self.tag_prefix}_series")
                    dpg.set_axis_limits(f"{self.tag_prefix}_y_axis", 40, 180)

    def add_samples(self, samples: 'List[PVSSample]'):
        """Add new HR samples from PPI data to the buffer."""
        for s in samples:
            if s.ppi_hr is None or s.ppi_hr == 0:
                continue
            if not self._start_time:
                self._start_time = s.timestamp
            t = s.timestamp - self._start_time
            self.ts.append(t)
            self.hr_values.append(s.ppi_hr)

    def update_plot(self):
        """Push buffered data to the DearPyGui plot."""
        if not self.ts:
            return
        t = list(self.ts)
        dpg.set_value(f"{self.tag_prefix}_series", [t, list(self.hr_values)])
        latest = t[-1]
        dpg.set_axis_limits(f"{self.tag_prefix}_x_axis",
                            max(0, latest - 60), latest + 5)
        dpg.fit_axis_data(f"{self.tag_prefix}_y_axis")
