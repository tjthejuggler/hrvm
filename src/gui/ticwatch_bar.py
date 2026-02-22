"""TicWatch top bar UI builders for the HRV Biofeedback GUI.

Each TicWatch device gets its own independent bar row:
  - TicWatchLeftBar  — purple/violet, port 5555
  - TicWatchRightBar — cyan/teal,     port 5556

Ports are hardcoded — no user input needed.  The user only needs to run
the correct `adb reverse` command in a terminal before clicking Start.
"""

import dearpygui.dearpygui as dpg
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.ble.ticwatch_manager import SingleTicWatchManager

logger = logging.getLogger(__name__)

_COLOR_LEFT  = (180, 100, 255)   # purple/violet
_COLOR_RIGHT = (0,   210, 210)   # cyan/teal


class TicWatchLeftBar:
    """Top bar row for TicWatch Left (purple, port 5555)."""

    def __init__(self, manager: "SingleTicWatchManager"):
        self.manager = manager
        self._prev_status = ""

    def build(self):
        with dpg.group(horizontal=True):
            dpg.add_text("TicWatch Left", color=_COLOR_LEFT)
            dpg.add_text(f"(port {self.manager.port})", color=(120, 70, 180))
            dpg.add_spacer(width=10)
            dpg.add_text("●", tag="tw_left_dot", color=(255, 0, 0))
            dpg.add_text("Stopped", tag="tw_left_status_text", color=(255, 0, 0))
            dpg.add_spacer(width=10)
            dpg.add_button(label="Start", tag="tw_left_btn",
                           callback=self._handle_toggle, width=80)

    def _handle_toggle(self):
        if self.manager.running:
            self.manager.stop()
            dpg.configure_item("tw_left_btn", label="Start")
            self._apply_status("Stopped", False)
        else:
            self.manager.start()
            dpg.configure_item("tw_left_btn", label="Stop")
            self._apply_status("Waiting for watch…", None)

    def poll_status(self):
        status = self.manager.status()
        if status == self._prev_status:
            return
        self._prev_status = status
        streaming = "Streaming" in status
        self._apply_status(status, streaming if self.manager.running else False)
        if dpg.does_item_exist("header_ticwatch_left"):
            dpg.configure_item("header_ticwatch_left", show=streaming)

    def _apply_status(self, text: str, streaming):
        if streaming is True:
            color = (0, 255, 0)
        elif streaming is False:
            color = (255, 0, 0)
        else:
            color = (255, 255, 0)
        if dpg.does_item_exist("tw_left_status_text"):
            dpg.set_value("tw_left_status_text", text)
            dpg.configure_item("tw_left_status_text", color=color)
        if dpg.does_item_exist("tw_left_dot"):
            dpg.configure_item("tw_left_dot", color=color)


class TicWatchRightBar:
    """Top bar row for TicWatch Right (cyan, port 5556)."""

    def __init__(self, manager: "SingleTicWatchManager"):
        self.manager = manager
        self._prev_status = ""

    def build(self):
        with dpg.group(horizontal=True):
            dpg.add_text("TicWatch Right", color=_COLOR_RIGHT)
            dpg.add_text(f"(port {self.manager.port})", color=(0, 140, 140))
            dpg.add_spacer(width=10)
            dpg.add_text("●", tag="tw_right_dot", color=(255, 0, 0))
            dpg.add_text("Stopped", tag="tw_right_status_text", color=(255, 0, 0))
            dpg.add_spacer(width=10)
            dpg.add_button(label="Start", tag="tw_right_btn",
                           callback=self._handle_toggle, width=80)

    def _handle_toggle(self):
        if self.manager.running:
            self.manager.stop()
            dpg.configure_item("tw_right_btn", label="Start")
            self._apply_status("Stopped", False)
        else:
            self.manager.start()
            dpg.configure_item("tw_right_btn", label="Stop")
            self._apply_status("Waiting for watch…", None)

    def poll_status(self):
        status = self.manager.status()
        if status == self._prev_status:
            return
        self._prev_status = status
        streaming = "Streaming" in status
        self._apply_status(status, streaming if self.manager.running else False)
        if dpg.does_item_exist("header_ticwatch_right"):
            dpg.configure_item("header_ticwatch_right", show=streaming)

    def _apply_status(self, text: str, streaming):
        if streaming is True:
            color = (0, 255, 0)
        elif streaming is False:
            color = (255, 0, 0)
        else:
            color = (255, 255, 0)
        if dpg.does_item_exist("tw_right_status_text"):
            dpg.set_value("tw_right_status_text", text)
            dpg.configure_item("tw_right_status_text", color=color)
        if dpg.does_item_exist("tw_right_dot"):
            dpg.configure_item("tw_right_dot", color=color)
