"""Genki Wave BLE connection manager.

Manages the BLE connection to a Genki Wave ring device using the genki-wave
library. Runs BLE operations in a background thread with its own asyncio
event loop (required for bleak on Linux/D-Bus).

Data is delivered via a thread-safe queue that the GUI can poll.

Architecture mirrors the pattern from the genki_ltx reference project:
  - ProtocolThread (sync, thread-safe queue) with bleak async BLE
  - CommunicateCancel for clean shutdown
  - QueueWithPop.pop_all() for batch draining
"""

import asyncio
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

# Default BLE address for the Genki Wave (can be overridden)
DEFAULT_WAVE_ADDRESS = "EF:AA:3B:81:E7:D7"

# Try to import genki_wave; gracefully degrade if not installed
try:
    from genki_wave.data import DataPackage
    from genki_wave.protocols import ProtocolThread, CommunicateCancel
    from genki_wave.constants import API_CHAR_UUID
    from genki_wave.data.writing import (
        get_device_info_request,
        get_start_api_package,
        get_default_api_config_package,
    )
    from genki_wave.asyncio_runner import prepare_protocol_as_bleak_callback
    from bleak import BleakClient, BleakScanner

    GENKI_AVAILABLE = True
except ImportError:
    GENKI_AVAILABLE = False
    logger.warning("genki-wave library not installed. Genki Wave support disabled.")


@dataclass
class WaveSample:
    """A single IMU sample from the Genki Wave."""
    timestamp: float
    gyro: Tuple[float, float, float]
    acc: Tuple[float, float, float]
    mag: Tuple[float, float, float]


class GenkiBLEThread(threading.Thread):
    """Background thread with its own asyncio event loop for BLE operations.

    bleak on Linux uses D-Bus which requires a standard asyncio event loop.
    Qt/DearPyGui event loops don't provide proper D-Bus integration, so we
    run all BLE work in this dedicated thread.
    """

    def __init__(self):
        super().__init__(daemon=True, name="GenkiBLE")
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def wait_ready(self):
        self._ready.wait()

    def submit(self, coro) -> asyncio.Future:
        """Schedule a coroutine on the BLE thread's event loop."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class GenkiWaveManager:
    """Manages the Genki Wave BLE connection and data streaming.

    Usage:
        manager = GenkiWaveManager()
        manager.connect("EF:AA:3B:81:E7:D7")
        ...
        samples = manager.poll()  # returns list of WaveSample
        ...
        manager.disconnect()
        manager.shutdown()
    """

    def __init__(self):
        self._ble_thread: Optional[GenkiBLEThread] = None
        self._protocol: Optional[object] = None  # ProtocolThread when available
        self._comm: Optional[object] = None  # CommunicateCancel when available
        self._future: Optional[asyncio.Future] = None
        self._connected = False
        self._connecting = False
        self._status_message = "Disconnected"
        self._status_lock = threading.Lock()

        # Data buffers (thread-safe via deque + lock)
        self.buffer_size = 2000
        self._samples: List[WaveSample] = []
        self._samples_lock = threading.Lock()

    @property
    def available(self) -> bool:
        """Whether the genki-wave library is installed."""
        return GENKI_AVAILABLE

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def connecting(self) -> bool:
        return self._connecting

    @property
    def status_message(self) -> str:
        with self._status_lock:
            return self._status_message

    def _set_status(self, connected: bool, message: str):
        with self._status_lock:
            self._connected = connected
            self._status_message = message
            if not connected:
                self._connecting = False

    def _ensure_ble_thread(self):
        """Start the BLE thread if not already running."""
        if self._ble_thread is None or not self._ble_thread.is_alive():
            self._ble_thread = GenkiBLEThread()
            self._ble_thread.start()
            self._ble_thread.wait_ready()

    def connect(self, address: str = DEFAULT_WAVE_ADDRESS):
        """Start connecting to the Genki Wave at the given BLE address."""
        if not GENKI_AVAILABLE:
            self._set_status(False, "genki-wave library not installed")
            return

        if self._connected or self._connecting:
            return

        self._connecting = True
        self._set_status(False, "Connecting...")
        self._ensure_ble_thread()

        # Create protocol and comm (thread-safe objects)
        self._protocol = ProtocolThread()
        self._comm = CommunicateCancel()

        # Start the BLE task on the BLE thread's event loop
        self._future = self._ble_thread.submit(
            self._wave_ble_task(address, self._protocol, self._comm)
        )
        self._future.add_done_callback(self._on_task_done)

    def disconnect(self):
        """Request disconnection from the Genki Wave."""
        if self._comm is not None:
            self._comm.cancel = True
        self._connecting = False

    def shutdown(self):
        """Clean shutdown of the BLE thread."""
        self.disconnect()
        if self._ble_thread and self._ble_thread.loop:
            self._ble_thread.loop.call_soon_threadsafe(self._ble_thread.loop.stop)

    def poll(self) -> List[WaveSample]:
        """Drain the protocol queue and return new WaveSample objects.

        Call this from the GUI thread at ~50 Hz (every 20ms).
        Returns an empty list if no new data.
        """
        if not GENKI_AVAILABLE or self._protocol is None:
            return []

        all_data = self._protocol.queue.pop_all()
        if not all_data:
            return []

        samples = []
        now = time.time()
        for package in all_data:
            if not isinstance(package, DataPackage):
                continue
            sample = WaveSample(
                timestamp=now,
                gyro=(package.gyro.x, package.gyro.y, package.gyro.z),
                acc=(package.acc.x, package.acc.y, package.acc.z),
                mag=(package.mag.x, package.mag.y, package.mag.z),
            )
            samples.append(sample)

        return samples

    async def _wave_ble_task(self, address: str, protocol, comm):
        """Runs on the BLE thread — connects to Wave and streams data."""
        self._set_status(False, "Scanning...")
        logger.info(f"Genki Wave: scanning for {address}")

        bleak_cb = prepare_protocol_as_bleak_callback(protocol)

        def on_disconnect(client):
            logger.warning("Genki Wave disconnected unexpectedly!")
            comm.cancel = True

        try:
            device = await BleakScanner.find_device_by_address(address, timeout=10.0)
            if device:
                logger.info(f"Genki Wave: found {device.name} ({device.address})")
                target = device
            else:
                logger.warning(f"Genki Wave: scanner didn't find {address}, trying direct")
                target = address

            self._set_status(False, "Connecting...")
            self._connecting = True

            async with BleakClient(
                target, disconnected_callback=on_disconnect, timeout=20.0
            ) as client:
                logger.info("Genki Wave: connected, setting up notifications")
                await client.start_notify(API_CHAR_UUID, bleak_cb)
                await client.write_gatt_char(
                    API_CHAR_UUID, get_device_info_request(), False
                )
                await client.write_gatt_char(
                    API_CHAR_UUID, get_start_api_package(), False
                )
                await client.write_gatt_char(
                    API_CHAR_UUID, get_default_api_config_package(), False
                )

                self._set_status(True, "Connected — streaming")
                self._connecting = False
                logger.info("Genki Wave: streaming data")
                comm.is_connected = True

                while not comm.cancel:
                    await asyncio.sleep(0.1)

                await client.stop_notify(API_CHAR_UUID)

        except Exception as exc:
            self._set_status(False, f"Error: {exc}")
            logger.error(f"Genki Wave error: {exc}", exc_info=True)
        finally:
            self._set_status(False, "Disconnected")

    def _on_task_done(self, future):
        """Called on BLE thread when the wave task finishes."""
        try:
            exc = future.exception()
            if exc:
                logger.error(f"Genki Wave task exception: {exc}", exc_info=exc)
        except Exception:
            pass
        self._set_status(False, "Disconnected")
