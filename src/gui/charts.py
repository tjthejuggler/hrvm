"""
Collapsible chart widgets for the HRV Biofeedback GUI.
Each chart is a collapsible tree node containing a DearPyGui plot.

Collapsed-state optimization: update_plot() checks whether the tree node
is open before doing any DPG set_value / axis-limit calls, so collapsed
charts consume no CPU for rendering.
"""
import dearpygui.dearpygui as dpg
import numpy as np
from collections import deque
from typing import List, Tuple, Optional


class CollapsibleChart:
    """Base class for a collapsible chart section."""

    def __init__(self, label: str, tag_prefix: str, default_open: bool = True):
        self.label = label
        self.tag_prefix = tag_prefix
        self.default_open = default_open
        self.node_tag = f"{tag_prefix}_node"

    def build(self, parent):
        """Override in subclass to build chart inside the tree node."""
        raise NotImplementedError

    def is_visible(self) -> bool:
        """Return True if the tree node exists and is currently open.

        dpg.get_value() on a tree_node returns the open/closed boolean.
        Falls back to True if the item doesn't exist yet (safe default).
        """
        if not dpg.does_item_exist(self.node_tag):
            return True  # not built yet — don't suppress data
        return bool(dpg.get_value(self.node_tag))


class BiofeedbackChart(CollapsibleChart):
    """Heart Rate & Pacer overlay chart."""

    def __init__(self):
        super().__init__("Heart Rate & Pacer", "biofeedback", default_open=True)
        self.hr_x = deque(maxlen=240)
        self.hr_y = deque(maxlen=240)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="Heart Rate & Pacer", height=250, width=-1, tag="biofeedback_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="bf_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="BPM", tag="bf_y_axis"):
                    dpg.add_line_series([], [], label="Interpolated HR", tag="hr_series")
                    dpg.add_line_series([], [], label="Pacer", tag="pacer_series")
                    dpg.set_axis_limits("bf_y_axis", 50, 90)  # initial sensible default


class HeartbeatChart(CollapsibleChart):
    """Shows individual heartbeats with timing and RR intervals."""

    def __init__(self):
        super().__init__("Heartbeats (RR Intervals)", "heartbeat", default_open=True)
        self.max_beats = 300
        self.beat_times: deque = deque(maxlen=self.max_beats)
        self.beat_rr: deque = deque(maxlen=self.max_beats)
        self.beat_hr: deque = deque(maxlen=self.max_beats)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="Heartbeats", height=250, width=-1, tag="heartbeat_plot"):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="hb_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="RR Interval (ms)", tag="hb_y_axis"):
                    dpg.add_stem_series([], [], label="RR (ms)", tag="hb_rr_series")
                    dpg.set_axis_limits("hb_y_axis", 400, 1200)

    def add_beats(self, timestamp: float, rr_intervals: List[float], start_time: float):
        current_time = timestamp - start_time
        for rr in rr_intervals:
            if 300 < rr < 2000:
                self.beat_times.append(current_time)
                self.beat_rr.append(rr)
                self.beat_hr.append(60000.0 / rr if rr > 0 else 0)

    def update_plot(self, current_time_offset: float):
        if not self.is_visible() or len(self.beat_times) == 0:
            return
        times = list(self.beat_times)
        rr_vals = list(self.beat_rr)
        dpg.set_value("hb_rr_series", [times, rr_vals])
        dpg.set_axis_limits("hb_x_axis", max(0, current_time_offset - 60), current_time_offset + 5)
        dpg.fit_axis_data("hb_y_axis")


class TachogramChart(CollapsibleChart):
    """RR Interval Tachogram — scrolling ~60s window."""

    def __init__(self):
        super().__init__("RR Tachogram", "tachogram", default_open=False)
        self.max_beats = 120
        self.rr_times: deque = deque(maxlen=self.max_beats)
        self.rr_history: deque = deque(maxlen=self.max_beats)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="RR Interval Tachogram", height=200, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="rr_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="RR (ms)", tag="rr_y_axis"):
                    dpg.add_line_series([], [], label="RR Intervals", tag="rr_series")

    def add_rr(self, rr_intervals: List[float], current_time: Optional[float] = None):
        t = current_time if current_time is not None else 0.0
        for rr in rr_intervals:
            if 300 < rr < 2000:
                self.rr_times.append(t)
                self.rr_history.append(rr)

    def update_plot(self, current_time: Optional[float] = None):
        if not self.is_visible() or len(self.rr_history) == 0:
            return
        t = list(self.rr_times)
        dpg.set_value("rr_series", [t, list(self.rr_history)])
        if current_time is not None:
            dpg.set_axis_limits("rr_x_axis", max(0, current_time - 60), current_time + 5)
        else:
            dpg.fit_axis_data("rr_x_axis")
        dpg.fit_axis_data("rr_y_axis")


class PoincareChart(CollapsibleChart):
    """Poincaré Plot (RR_n vs RR_n+1) — rolling ~60-beat window."""

    def __init__(self):
        super().__init__("Poincaré Plot", "poincare", default_open=False)
        self.px: deque = deque(maxlen=120)
        self.py: deque = deque(maxlen=120)
        self._last_rr = None

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="Poincaré Plot", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="RR_n (ms)", tag="poincare_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="RR_n+1 (ms)", tag="poincare_y_axis"):
                    dpg.add_scatter_series([], [], label="RR Pairs", tag="poincare_series")

    def add_rr(self, rr_intervals: List[float]):
        for rr in rr_intervals:
            if 300 < rr < 2000:
                if self._last_rr is not None:
                    self.px.append(self._last_rr)
                    self.py.append(rr)
                self._last_rr = rr

    def update_plot(self):
        if not self.is_visible() or len(self.px) == 0:
            return
        dpg.set_value("poincare_series", [list(self.px), list(self.py)])
        dpg.fit_axis_data("poincare_x_axis")
        dpg.fit_axis_data("poincare_y_axis")


class RMSSDHistoryChart(CollapsibleChart):
    """RMSSD history chart — scrolling ~60s window."""

    def __init__(self):
        super().__init__("RMSSD History", "rmssd_hist", default_open=False)
        self.max_history = 600
        self.time_history: deque = deque(maxlen=self.max_history)
        self.rmssd_history: deque = deque(maxlen=self.max_history)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="RMSSD History", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="rmssd_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="RMSSD (ms)", tag="rmssd_y_axis"):
                    dpg.add_line_series([], [], label="RMSSD", tag="rmssd_series")

    def add_data(self, current_time: float, rmssd: float):
        self.time_history.append(current_time)
        self.rmssd_history.append(rmssd)

    def update_plot(self):
        if not self.is_visible() or len(self.time_history) == 0:
            return
        t = list(self.time_history)
        min_len = min(len(t), len(self.rmssd_history))
        dpg.set_value("rmssd_series", [t[-min_len:], list(self.rmssd_history)[-min_len:]])
        latest = t[-1]
        dpg.set_axis_limits("rmssd_x_axis", max(0, latest - 60), latest + 5)
        dpg.fit_axis_data("rmssd_y_axis")


class SDNNHistoryChart(CollapsibleChart):
    """SDNN history chart — scrolling ~60s window."""

    def __init__(self):
        super().__init__("SDNN History", "sdnn_hist", default_open=False)
        self.max_history = 600
        self.time_history: deque = deque(maxlen=self.max_history)
        self.sdnn_history: deque = deque(maxlen=self.max_history)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="SDNN History", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="sdnn_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="SDNN (ms)", tag="sdnn_y_axis"):
                    dpg.add_line_series([], [], label="SDNN", tag="sdnn_series")

    def add_data(self, current_time: float, sdnn: float):
        self.time_history.append(current_time)
        self.sdnn_history.append(sdnn)

    def update_plot(self):
        if not self.is_visible() or len(self.time_history) == 0:
            return
        t = list(self.time_history)
        min_len = min(len(t), len(self.sdnn_history))
        dpg.set_value("sdnn_series", [t[-min_len:], list(self.sdnn_history)[-min_len:]])
        latest = t[-1]
        dpg.set_axis_limits("sdnn_x_axis", max(0, latest - 60), latest + 5)
        dpg.fit_axis_data("sdnn_y_axis")


class CoherenceHistoryChart(CollapsibleChart):
    """Coherence score history chart — scrolling ~60s window."""

    def __init__(self):
        super().__init__("Coherence History", "coherence_hist", default_open=False)
        self.max_history = 600
        self.time_history: deque = deque(maxlen=self.max_history)
        self.coherence_history: deque = deque(maxlen=self.max_history)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="Coherence History", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="coherence_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="Score", tag="coherence_y_axis"):
                    dpg.add_line_series([], [], label="Coherence", tag="coherence_series")

    def add_data(self, current_time: float, coherence: float):
        self.time_history.append(current_time)
        self.coherence_history.append(coherence)

    def update_plot(self):
        if not self.is_visible() or len(self.time_history) == 0:
            return
        t = list(self.time_history)
        min_len = min(len(t), len(self.coherence_history))
        dpg.set_value("coherence_series", [t[-min_len:], list(self.coherence_history)[-min_len:]])
        latest = t[-1]
        dpg.set_axis_limits("coherence_x_axis", max(0, latest - 60), latest + 5)
        dpg.fit_axis_data("coherence_y_axis")


# ---------------------------------------------------------------------------
# HRV Section Charts
# Device-agnostic HRV charts shown in the dedicated HRV section.
# ---------------------------------------------------------------------------

class HRVTachogramChart(CollapsibleChart):
    """HRV Tachogram — RR intervals over time, scrolling ~60s window."""

    def __init__(self):
        super().__init__("RR Tachogram", "hrv_tachogram", default_open=True)
        self.max_beats = 120
        self.rr_times: deque = deque(maxlen=self.max_beats)
        self.rr_history: deque = deque(maxlen=self.max_beats)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="RR Interval Tachogram", height=200, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="hrv_rr_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="RR (ms)", tag="hrv_rr_y_axis"):
                    dpg.add_line_series([], [], label="RR Intervals", tag="hrv_rr_series")

    def add_rr(self, rr_intervals: List[float], current_time: float):
        for rr in rr_intervals:
            if 300 < rr < 2000:
                self.rr_times.append(current_time)
                self.rr_history.append(rr)

    def update_plot(self, current_time: float):
        if not self.is_visible() or not self.rr_history:
            return
        dpg.set_value("hrv_rr_series", [list(self.rr_times), list(self.rr_history)])
        dpg.set_axis_limits("hrv_rr_x_axis", max(0, current_time - 60), current_time + 5)
        dpg.fit_axis_data("hrv_rr_y_axis")


class HRVPoincareChart(CollapsibleChart):
    """HRV Poincaré Plot — rolling ~60-beat window."""

    def __init__(self):
        super().__init__("Poincaré Plot", "hrv_poincare", default_open=False)
        self.px: deque = deque(maxlen=120)
        self.py: deque = deque(maxlen=120)
        self._last_rr: Optional[float] = None

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="Poincaré Plot", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="RR_n (ms)", tag="hrv_poincare_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="RR_n+1 (ms)",
                                   tag="hrv_poincare_y_axis"):
                    dpg.add_scatter_series([], [], label="RR Pairs",
                                           tag="hrv_poincare_series")

    def add_rr(self, rr_intervals: List[float]):
        for rr in rr_intervals:
            if 300 < rr < 2000:
                if self._last_rr is not None:
                    self.px.append(self._last_rr)
                    self.py.append(rr)
                self._last_rr = rr

    def update_plot(self):
        if not self.is_visible() or not self.px:
            return
        dpg.set_value("hrv_poincare_series", [list(self.px), list(self.py)])
        dpg.fit_axis_data("hrv_poincare_x_axis")
        dpg.fit_axis_data("hrv_poincare_y_axis")


class HRVRMSSDChart(CollapsibleChart):
    """HRV RMSSD history — scrolling ~60s window."""

    def __init__(self):
        super().__init__("RMSSD History", "hrv_rmssd", default_open=True)
        self.time_history: deque = deque(maxlen=600)
        self.rmssd_history: deque = deque(maxlen=600)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="RMSSD History", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="hrv_rmssd_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="RMSSD (ms)", tag="hrv_rmssd_y_axis"):
                    dpg.add_line_series([], [], label="RMSSD", tag="hrv_rmssd_series")

    def add_data(self, current_time: float, rmssd: float):
        self.time_history.append(current_time)
        self.rmssd_history.append(rmssd)

    def update_plot(self):
        if not self.is_visible() or not self.time_history:
            return
        t = list(self.time_history)
        min_len = min(len(t), len(self.rmssd_history))
        dpg.set_value("hrv_rmssd_series", [t[-min_len:], list(self.rmssd_history)[-min_len:]])
        latest = t[-1]
        dpg.set_axis_limits("hrv_rmssd_x_axis", max(0, latest - 60), latest + 5)
        dpg.fit_axis_data("hrv_rmssd_y_axis")


class HRVSDNNChart(CollapsibleChart):
    """HRV SDNN history — scrolling ~60s window."""

    def __init__(self):
        super().__init__("SDNN History", "hrv_sdnn", default_open=False)
        self.time_history: deque = deque(maxlen=600)
        self.sdnn_history: deque = deque(maxlen=600)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="SDNN History", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="hrv_sdnn_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="SDNN (ms)", tag="hrv_sdnn_y_axis"):
                    dpg.add_line_series([], [], label="SDNN", tag="hrv_sdnn_series")

    def add_data(self, current_time: float, sdnn: float):
        self.time_history.append(current_time)
        self.sdnn_history.append(sdnn)

    def update_plot(self):
        if not self.is_visible() or not self.time_history:
            return
        t = list(self.time_history)
        min_len = min(len(t), len(self.sdnn_history))
        dpg.set_value("hrv_sdnn_series", [t[-min_len:], list(self.sdnn_history)[-min_len:]])
        latest = t[-1]
        dpg.set_axis_limits("hrv_sdnn_x_axis", max(0, latest - 60), latest + 5)
        dpg.fit_axis_data("hrv_sdnn_y_axis")


class HRVCoherenceChart(CollapsibleChart):
    """HRV Coherence score history — scrolling ~60s window."""

    def __init__(self):
        super().__init__("Coherence History", "hrv_coherence", default_open=False)
        self.time_history: deque = deque(maxlen=600)
        self.coherence_history: deque = deque(maxlen=600)

    def build(self, parent):
        with dpg.tree_node(
            label=self.label, parent=parent, tag=self.node_tag,
            default_open=self.default_open
        ):
            with dpg.plot(label="Coherence History", height=250, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="hrv_coherence_x_axis")
                with dpg.plot_axis(dpg.mvYAxis, label="Score", tag="hrv_coherence_y_axis"):
                    dpg.add_line_series([], [], label="Coherence", tag="hrv_coherence_series")

    def add_data(self, current_time: float, coherence: float):
        self.time_history.append(current_time)
        self.coherence_history.append(coherence)

    def update_plot(self):
        if not self.is_visible() or not self.time_history:
            return
        t = list(self.time_history)
        min_len = min(len(t), len(self.coherence_history))
        dpg.set_value("hrv_coherence_series",
                      [t[-min_len:], list(self.coherence_history)[-min_len:]])
        latest = t[-1]
        dpg.set_axis_limits("hrv_coherence_x_axis", max(0, latest - 60), latest + 5)
        dpg.fit_axis_data("hrv_coherence_y_axis")


# ACC and ECG charts moved to h10_imu_charts.py
# Import them from there for backwards compatibility if needed.
