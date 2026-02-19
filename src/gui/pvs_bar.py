"""Polar Verity Sense top bar UI builder for the HRV Biofeedback GUI.

Builds the 'Polar Verity Sense' connection bar with:
  - Device label
  - Status indicator
  - Stream toggle checkboxes (ACC, GYR, MAG, PPI)
  - Connect/Disconnect button

Keeps the bar logic separate from the main UIManager for modularity.
"""

import dearpygui.dearpygui as dpg
import logging
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.ble.pvs_manager import PolarVeritySenseManager

logger = logging.getLogger(__name__)


class PolarVeritySenseBar:
    """Builds and manages the Polar Verity Sense connection bar in the UI."""

    def __init__(self, manager: 'PolarVeritySenseManager'):
        self.manager = manager
        self._prev_status = ""
        # Called with (is_streaming_hr: bool) when HR streaming state changes
        self.on_hr_streaming_changed: Optional[Callable[[bool], None]] = None
        self._prev_hr_streaming = False

    def build(self):
        """Build the PVS top bar. Call inside a dpg.window context."""
        with dpg.group(horizontal=True):
            dpg.add_text("Polar Verity Sense", color=(255, 128, 0))
            dpg.add_spacer(width=20)
            dpg.add_text("Status: ")
            dpg.add_text("Disconnected", tag="pvs_status_text", color=(255, 0, 0))
            dpg.add_spacer(width=20)

            # Stream toggles
            dpg.add_checkbox(label="ACC", tag="pvs_acc_toggle",
                             default_value=self.manager.enable_acc,
                             callback=self._on_toggle_acc)
            dpg.add_checkbox(label="GYR", tag="pvs_gyro_toggle",
                             default_value=self.manager.enable_gyro,
                             callback=self._on_toggle_gyro)
            dpg.add_checkbox(label="MAG", tag="pvs_mag_toggle",
                             default_value=self.manager.enable_mag,
                             callback=self._on_toggle_mag)
            dpg.add_checkbox(label="PPI", tag="pvs_ppi_toggle",
                             default_value=self.manager.enable_ppi,
                             callback=self._on_toggle_ppi)

            dpg.add_spacer(width=10)
            dpg.add_button(
                label="Connect",
                tag="pvs_connect_btn",
                callback=self._handle_connect,
                width=100,
            )

    def _on_toggle_acc(self, sender, app_data):
        self.manager.enable_acc = app_data

    def _on_toggle_gyro(self, sender, app_data):
        self.manager.enable_gyro = app_data

    def _on_toggle_mag(self, sender, app_data):
        self.manager.enable_mag = app_data

    def _on_toggle_ppi(self, sender, app_data):
        self.manager.enable_ppi = app_data

    def _handle_connect(self):
        """Handle the Connect/Disconnect button click."""
        if self.manager.connected or self.manager.connecting:
            self.manager.disconnect()
            dpg.set_value("pvs_status_text", "Disconnecting...")
            dpg.configure_item("pvs_status_text", color=(255, 255, 0))
        else:
            self.manager.connect()
            dpg.set_value("pvs_status_text", "Connecting...")
            dpg.configure_item("pvs_status_text", color=(255, 255, 0))

    def _set_toggles_enabled(self, enabled: bool):
        """Enable or disable all stream toggle checkboxes."""
        for tag in ("pvs_acc_toggle", "pvs_gyro_toggle", "pvs_mag_toggle",
                    "pvs_ppi_toggle"):
            dpg.configure_item(tag, enabled=enabled)

    def poll_status(self):
        """Poll the manager status and update UI. Call each frame."""
        status = self.manager.status_message

        # Detect whether PVS is actively streaming HR data.
        # HR is streaming when connected and the status message contains "HR"
        # (set by the manager when the HR notification subscription succeeds).
        is_hr_streaming = self.manager.connected and "HR" in status

        if status != self._prev_status:
            self._prev_status = status
            dpg.set_value("pvs_status_text", status)

            if self.manager.connected:
                dpg.configure_item("pvs_status_text", color=(0, 255, 0))
                dpg.configure_item("pvs_connect_btn", label="Disconnect")
                self._set_toggles_enabled(False)
                if dpg.does_item_exist("header_pvs"):
                    dpg.configure_item("header_pvs", show=True)
            elif self.manager.connecting:
                dpg.configure_item("pvs_status_text", color=(255, 255, 0))
                dpg.configure_item("pvs_connect_btn", label="Cancel")
            else:
                dpg.configure_item("pvs_status_text", color=(255, 0, 0))
                dpg.configure_item("pvs_connect_btn", label="Connect")
                self._set_toggles_enabled(True)
                if dpg.does_item_exist("header_pvs"):
                    dpg.configure_item("header_pvs", show=False)

        # Fire HR streaming callback when state changes
        if is_hr_streaming != self._prev_hr_streaming:
            self._prev_hr_streaming = is_hr_streaming
            if self.on_hr_streaming_changed is not None:
                self.on_hr_streaming_changed(is_hr_streaming)
