"""Genki Wave top bar UI builder for the HRV Biofeedback GUI.

Builds the 'Genki Wave' connection bar with:
  - Device label
  - Status indicator
  - BLE address input
  - Connect/Disconnect button
  - Breath Profile dropdown + Calibrate button

Keeps the bar logic separate from the main UIManager for modularity.
The calibration callback is injected by UIManager after construction.
"""

import dearpygui.dearpygui as dpg
import logging
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.ble.genki_manager import GenkiWaveManager

logger = logging.getLogger(__name__)

# Default BLE address
DEFAULT_WAVE_ADDRESS = "EF:AA:3B:81:E7:D7"


class GenkiWaveBar:
    """Builds and manages the Genki Wave connection bar in the UI."""

    def __init__(self, manager: 'GenkiWaveManager'):
        self.manager = manager
        self._prev_status = ""
        # Injected by UIManager after construction
        self.on_calibrate: Optional[Callable] = None
        self.on_profile_changed: Optional[Callable] = None

    def build(self):
        """Build the Genki Wave top bar. Call inside a dpg.window context."""
        from src.processing.acc_respiration import PROFILES
        with dpg.group(horizontal=True):
            dpg.add_text("Genki Wave", color=(0, 255, 128))
            dpg.add_spacer(width=20)
            dpg.add_text("Status: ")
            dpg.add_text("Disconnected", tag="genki_status_text", color=(255, 0, 0))
            dpg.add_spacer(width=20)
            dpg.add_input_text(
                tag="genki_address_input",
                default_value=DEFAULT_WAVE_ADDRESS,
                width=200,
                hint="Wave BLE Address",
            )
            dpg.add_spacer(width=10)
            dpg.add_button(
                label="Connect",
                tag="genki_connect_btn",
                callback=self._handle_connect,
                width=100,
            )
            dpg.add_spacer(width=20)
            dpg.add_text("Breath Profile:", color=(150, 220, 255))
            dpg.add_combo(
                items=PROFILES,
                tag="genki_breath_profile_combo",
                default_value=PROFILES[0],
                width=150,
                callback=self._handle_profile_changed,
            )
            dpg.add_button(
                label="Calibrate Breath",
                tag="genki_cal_btn",
                callback=self._handle_calibrate,
                width=130,
            )
            dpg.add_text("", tag="genki_breath_rate_text", color=(100, 255, 180))

    def _handle_calibrate(self):
        """Delegate to UIManager's Genki calibration opener."""
        if self.on_calibrate:
            self.on_calibrate()

    def _handle_profile_changed(self, sender, app_data):
        """Delegate profile change to UIManager."""
        if self.on_profile_changed:
            self.on_profile_changed(app_data)

    def _handle_connect(self):
        """Handle the Connect/Disconnect button click."""
        if self.manager.connected or self.manager.connecting:
            self.manager.disconnect()
            dpg.set_value("genki_status_text", "Disconnecting...")
            dpg.configure_item("genki_status_text", color=(255, 255, 0))
        else:
            address = dpg.get_value("genki_address_input").strip()
            if not address:
                dpg.set_value("genki_status_text", "Address required")
                dpg.configure_item("genki_status_text", color=(255, 100, 0))
                return
            self.manager.connect(address)
            dpg.set_value("genki_status_text", "Connecting...")
            dpg.configure_item("genki_status_text", color=(255, 255, 0))

    def poll_status(self):
        """Poll the manager status and update UI. Call each frame."""
        status = self.manager.status_message
        if status == self._prev_status:
            return
        self._prev_status = status

        dpg.set_value("genki_status_text", status)

        if self.manager.connected:
            dpg.configure_item("genki_status_text", color=(0, 255, 0))
            dpg.configure_item("genki_connect_btn", label="Disconnect")
            # Show Genki Wave graphs subsection
            if dpg.does_item_exist("header_genki_wave"):
                dpg.configure_item("header_genki_wave", show=True)
        elif self.manager.connecting:
            dpg.configure_item("genki_status_text", color=(255, 255, 0))
            dpg.configure_item("genki_connect_btn", label="Cancel")
        else:
            dpg.configure_item("genki_status_text", color=(255, 0, 0))
            dpg.configure_item("genki_connect_btn", label="Connect")
            # Hide Genki Wave graphs subsection
            if dpg.does_item_exist("header_genki_wave"):
                dpg.configure_item("header_genki_wave", show=False)
