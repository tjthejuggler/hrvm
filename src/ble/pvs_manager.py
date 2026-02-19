"""Polar Verity Sense BLE connection manager.

Manages the BLE connection to a Polar Verity Sense device using bleak.
Runs BLE operations in a background thread with its own asyncio event loop
(same pattern as GenkiWaveManager).

Streams available:
  - ACC  (accelerometer, 3-axis)
  - GYR  (gyroscope, 3-axis)
  - MAG  (magnetometer, 3-axis)
  - PPI  (pulse-to-pulse interval / HRV)
  - PPG  (raw optical heart rate)

Data is delivered via a thread-safe deque that the GUI can poll.
"""

import asyncio
import logging
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from bleak import BleakClient, BleakScanner

from src.ble.dbus_agent import register_agent
from src.ble.pvs_parser import (
    PMD_TYPE_ACC, PMD_TYPE_PPI, PMD_TYPE_GYR, PMD_TYPE_MAG, PMD_TYPE_PPG,
    parse_pmd_data, parse_settings_response,
    build_get_settings_command, build_start_command, build_stop_command,
    PVSDataPacket, PVSAccSample, PVSGyroSample, PVSMagSample,
    PVSPPISample, PVSPPGSample,
)

logger = logging.getLogger(__name__)

# Polar Verity Sense PMD Service UUIDs
PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

# Device name prefixes to scan for
PVS_NAME_PREFIXES = ("Polar Sense", "Polar Verity Sense")


@dataclass
class PVSSample:
    """A single timestamped sample from the Polar Verity Sense.

    Depending on which streams are active, some fields may be None.
    """
    timestamp: float
    acc: Optional[tuple] = None   # (x, y, z) in mg
    gyro: Optional[tuple] = None  # (x, y, z) in dps
    mag: Optional[tuple] = None   # (x, y, z) in Gauss/10
    ppi_ms: Optional[int] = None  # Pulse-to-pulse interval in ms
    ppi_hr: Optional[int] = None  # Heart rate from PPI
    ppg_channels: Optional[List[int]] = None  # Raw PPG channel values


class PVSBLEThread(threading.Thread):
    """Background thread with its own asyncio event loop for BLE operations.

    bleak on Linux uses D-Bus which requires a standard asyncio event loop.
    """

    def __init__(self):
        super().__init__(daemon=True, name="PVS_BLE")
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


class PolarVeritySenseManager:
    """Manages the Polar Verity Sense BLE connection and data streaming.

    Usage:
        manager = PolarVeritySenseManager()
        manager.connect()  # scans and connects
        ...
        samples = manager.poll()  # returns list of PVSSample
        ...
        manager.disconnect()
        manager.shutdown()
    """

    # Default stream settings for Polar Verity Sense
    # PVS uses different sample rates than H10: 26, 52, 104, 208, 416 Hz
    DEFAULT_ACC_RATE = 52
    DEFAULT_ACC_RESOLUTION = 16
    DEFAULT_ACC_RANGE = 8
    DEFAULT_GYR_RATE = 52
    DEFAULT_GYR_RESOLUTION = 16
    DEFAULT_GYR_RANGE = 2000
    DEFAULT_MAG_RATE = 10
    DEFAULT_MAG_RESOLUTION = 16

    # Fallback sample rates to try if the default fails
    ACC_RATE_FALLBACKS = [52, 26, 50, 25, 104, 208]
    GYR_RATE_FALLBACKS = [52, 26, 50, 25, 104, 208]

    def __init__(self):
        self._ble_thread: Optional[PVSBLEThread] = None
        self._future: Optional[asyncio.Future] = None
        self._connected = False
        self._connecting = False
        self._status_message = "Disconnected"
        self._status_lock = threading.Lock()
        self._cancel = False

        # Data buffer (thread-safe via deque)
        self._samples: deque = deque(maxlen=5000)
        self._samples_lock = threading.Lock()

        # Stream enable flags (set before connecting)
        self.enable_acc = True
        self.enable_gyro = True
        self.enable_mag = False   # Off by default (less common)
        self.enable_ppi = True
        self.enable_ppg = False   # Off by default (high bandwidth)

        # Settings received from device
        self._device_settings: Dict[int, dict] = {}

        # Control point response handling
        self._cp_response_event: Optional[asyncio.Event] = None
        self._cp_response_data: Optional[bytearray] = None

        # D-Bus agent state
        self._agent_registered = False

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
            self._ble_thread = PVSBLEThread()
            self._ble_thread.start()
            self._ble_thread.wait_ready()

    def connect(self, address: Optional[str] = None):
        """Start connecting to the Polar Verity Sense.

        If address is None, scans for the device by name.
        If address is provided, connects directly.
        """
        if self._connected or self._connecting:
            return

        self._connecting = True
        self._cancel = False
        self._set_status(False, "Connecting...")
        self._ensure_ble_thread()

        self._future = self._ble_thread.submit(
            self._pvs_ble_task(address)
        )
        self._future.add_done_callback(self._on_task_done)

    def disconnect(self):
        """Request disconnection from the Polar Verity Sense."""
        self._cancel = True
        self._connecting = False

    def shutdown(self):
        """Clean shutdown of the BLE thread."""
        self.disconnect()
        if self._ble_thread and self._ble_thread.loop:
            self._ble_thread.loop.call_soon_threadsafe(self._ble_thread.loop.stop)

    def poll(self) -> List[PVSSample]:
        """Drain the sample buffer and return new PVSSample objects.

        Call this from the GUI thread at ~50 Hz (every 20ms).
        Returns an empty list if no new data.
        """
        with self._samples_lock:
            if not self._samples:
                return []
            samples = list(self._samples)
            self._samples.clear()
            return samples

    def _enqueue_samples(self, packet: PVSDataPacket):
        """Convert parsed packet to PVSSample objects and enqueue them."""
        now = time.time()

        with self._samples_lock:
            if packet.acc_samples:
                for s in packet.acc_samples:
                    self._samples.append(PVSSample(
                        timestamp=now,
                        acc=(s.x, s.y, s.z),
                    ))

            if packet.gyro_samples:
                for s in packet.gyro_samples:
                    self._samples.append(PVSSample(
                        timestamp=now,
                        gyro=(s.x, s.y, s.z),
                    ))

            if packet.mag_samples:
                for s in packet.mag_samples:
                    self._samples.append(PVSSample(
                        timestamp=now,
                        mag=(s.x, s.y, s.z),
                    ))

            if packet.ppi_samples:
                for s in packet.ppi_samples:
                    self._samples.append(PVSSample(
                        timestamp=now,
                        ppi_ms=s.ppi_ms,
                        ppi_hr=s.hr,
                    ))

            if packet.ppg_samples:
                for s in packet.ppg_samples:
                    self._samples.append(PVSSample(
                        timestamp=now,
                        ppg_channels=s.channels,
                    ))

    def _on_pmd_data(self, sender, data: bytearray):
        """Callback for PMD Data notifications."""
        packet = parse_pmd_data(data)
        if packet:
            self._enqueue_samples(packet)

    def _on_cp_response(self, sender, data: bytearray):
        """Callback for PMD Control Point notifications (responses).

        Response format: [0xF0, op_code, measurement_type, status, ...]
        """
        logger.debug(f"PVS CP notification: {data.hex()}")
        self._cp_response_data = data
        if self._cp_response_event:
            self._cp_response_event.set()

    async def _send_cp_command(self, client: BleakClient, command: bytearray,
                                timeout: float = 5.0) -> Optional[bytearray]:
        """Send a command to the PMD Control Point and wait for response."""
        self._cp_response_event = asyncio.Event()
        self._cp_response_data = None

        await client.write_gatt_char(PMD_CONTROL_UUID, command, response=True)

        try:
            await asyncio.wait_for(self._cp_response_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("PVS: Control Point response timeout")
            return None

        return self._cp_response_data

    async def _query_settings(self, client: BleakClient, meas_type: int) -> dict:
        """Query device settings for a measurement type."""
        cmd = build_get_settings_command(meas_type)
        response = await self._send_cp_command(client, cmd)
        if response:
            settings = parse_settings_response(response)
            self._device_settings[meas_type] = settings
            logger.info(f"PVS settings for type 0x{meas_type:02x}: {settings}")
            return settings
        return {}

    async def _start_stream(self, client: BleakClient, meas_type: int,
                            sample_rate: int = 0, resolution: int = 0,
                            range_val: int = 0, channels: int = 0) -> bool:
        """Start a measurement stream on the device.

        Uses hardcoded known-good settings (like the H10 manager does)
        rather than parsing the settings response, which has a complex
        format that varies between firmware versions.
        """
        cmd = build_start_command(meas_type, sample_rate, resolution,
                                  range_val, channels)
        logger.info(f"PVS: Sending start command for type 0x{meas_type:02x}: {cmd.hex()}")
        response = await self._send_cp_command(client, cmd)
        # Response format: [0xF0, op_code(0x02), meas_type, status(0x00=ok)]
        if response and len(response) >= 4 and response[3] == 0x00:
            logger.info(f"PVS: Started stream type 0x{meas_type:02x}")
            return True
        else:
            status = response[3] if response and len(response) >= 4 else -1
            logger.error(f"PVS: Failed to start stream type 0x{meas_type:02x}, "
                        f"status=0x{status:02x}, "
                        f"response: {response.hex() if response else 'None'}")
            return False

    async def _stop_stream(self, client: BleakClient, meas_type: int):
        """Stop a measurement stream on the device."""
        cmd = build_stop_command(meas_type)
        try:
            await client.write_gatt_char(PMD_CONTROL_UUID, cmd, response=True)
            logger.info(f"PVS: Stopped stream type 0x{meas_type:02x}")
        except Exception as e:
            logger.warning(f"PVS: Error stopping stream 0x{meas_type:02x}: {e}")

    async def _pvs_ble_task(self, address: Optional[str]):
        """Runs on the BLE thread — connects to PVS and streams data."""
        self._set_status(False, "Scanning...")
        logger.info("PVS: Scanning for Polar Verity Sense...")

        def on_disconnect(client):
            logger.warning("PVS: Disconnected unexpectedly!")
            self._cancel = True

        try:
            # Find device
            if address:
                logger.info(f"PVS: Looking for device at {address}")
                device = await BleakScanner.find_device_by_address(address, timeout=10.0)
                if not device:
                    logger.warning(f"PVS: Device not found at {address}, trying scan by name")
                    device = await BleakScanner.find_device_by_filter(
                        lambda d, ad: d.name and any(
                            d.name.startswith(p) for p in PVS_NAME_PREFIXES
                        ),
                        timeout=10.0,
                    )
            else:
                device = await BleakScanner.find_device_by_filter(
                    lambda d, ad: d.name and any(
                        d.name.startswith(p) for p in PVS_NAME_PREFIXES
                    ),
                    timeout=10.0,
                )

            if not device:
                self._set_status(False, "Device not found")
                logger.error("PVS: Polar Verity Sense not found during scan")
                return

            logger.info(f"PVS: Found {device.name} ({device.address})")
            self._set_status(False, "Connecting...")
            self._connecting = True

            async with BleakClient(
                device, disconnected_callback=on_disconnect, timeout=20.0
            ) as client:
                logger.info(f"PVS: Connected to {device.name}")

                # Negotiate MTU for large PMD data frames
                try:
                    mtu_before = client.mtu_size
                    await client._backend._acquire_mtu()
                    mtu_after = client.mtu_size
                    logger.info(f"PVS: MTU {mtu_before} -> {mtu_after}")
                except Exception as e:
                    logger.warning(f"PVS: MTU negotiation failed (non-fatal): {e}")

                # Register D-Bus agent for auto-accept pairing (idempotent)
                if not self._agent_registered:
                    self._agent_registered = await register_agent()

                # Pair device (required for PMD service access on Polar devices)
                try:
                    await client.pair()
                    logger.info("PVS: Device paired successfully")
                except Exception as e:
                    logger.warning(f"PVS: Pairing result (may already be paired): {e}")

                # Log discovered services for debugging
                logger.info("PVS: Listing discovered services...")
                for service in client.services:
                    logger.info(f"  Service: {service.uuid}")
                    for char in service.characteristics:
                        logger.info(f"    Char: {char.uuid} ({char.properties})")

                # Subscribe to Control Point notifications (for responses)
                logger.info("PVS: Subscribing to PMD Control Point...")
                await client.start_notify(PMD_CONTROL_UUID, self._on_cp_response)
                await asyncio.sleep(0.3)

                # Subscribe to Data notifications with use_start_notify=True
                # (avoids bluetoothd crashes with AcquireNotify on high-freq streams)
                logger.info("PVS: Subscribing to PMD Data (use_start_notify=True)...")
                await client.start_notify(
                    PMD_DATA_UUID, self._on_pmd_data,
                    bluez={"use_start_notify": True}
                )
                await asyncio.sleep(0.3)

                # Start requested streams
                active_streams = []

                if self.enable_acc:
                    # Try multiple sample rates until one works
                    for rate in self.ACC_RATE_FALLBACKS:
                        ok = await self._start_stream(
                            client, PMD_TYPE_ACC,
                            sample_rate=rate,
                            resolution=self.DEFAULT_ACC_RESOLUTION,
                            range_val=self.DEFAULT_ACC_RANGE,
                        )
                        if ok:
                            active_streams.append(PMD_TYPE_ACC)
                            logger.info(f"PVS: ACC started at {rate} Hz")
                            break
                        await asyncio.sleep(0.2)

                if self.enable_gyro:
                    # Try multiple sample rates until one works
                    for rate in self.GYR_RATE_FALLBACKS:
                        ok = await self._start_stream(
                            client, PMD_TYPE_GYR,
                            sample_rate=rate,
                            resolution=self.DEFAULT_GYR_RESOLUTION,
                            range_val=self.DEFAULT_GYR_RANGE,
                        )
                        if ok:
                            active_streams.append(PMD_TYPE_GYR)
                            logger.info(f"PVS: GYR started at {rate} Hz")
                            break
                        await asyncio.sleep(0.2)

                if self.enable_mag:
                    ok = await self._start_stream(
                        client, PMD_TYPE_MAG,
                        sample_rate=self.DEFAULT_MAG_RATE,
                        resolution=self.DEFAULT_MAG_RESOLUTION,
                    )
                    if ok:
                        active_streams.append(PMD_TYPE_MAG)

                if self.enable_ppi:
                    # PPI doesn't need sample rate / resolution settings
                    ok = await self._start_stream(client, PMD_TYPE_PPI)
                    if ok:
                        active_streams.append(PMD_TYPE_PPI)

                if self.enable_ppg:
                    ok = await self._start_stream(
                        client, PMD_TYPE_PPG,
                        channels=4,
                    )
                    if ok:
                        active_streams.append(PMD_TYPE_PPG)

                if not active_streams:
                    self._set_status(False, "No streams started")
                    logger.error("PVS: Failed to start any streams")
                    return

                stream_names = {
                    PMD_TYPE_ACC: "ACC", PMD_TYPE_GYR: "GYR",
                    PMD_TYPE_MAG: "MAG", PMD_TYPE_PPI: "PPI",
                    PMD_TYPE_PPG: "PPG",
                }
                names = [stream_names.get(s, f"0x{s:02x}") for s in active_streams]
                self._set_status(True, f"Streaming: {', '.join(names)}")
                self._connecting = False
                logger.info(f"PVS: Streaming {', '.join(names)}")

                # Keep alive until cancelled
                while not self._cancel:
                    await asyncio.sleep(0.1)

                # Stop all active streams
                for stream_type in active_streams:
                    await self._stop_stream(client, stream_type)

                # Stop notifications
                try:
                    await client.stop_notify(PMD_DATA_UUID)
                    await client.stop_notify(PMD_CONTROL_UUID)
                except Exception:
                    pass

        except Exception as exc:
            self._set_status(False, f"Error: {exc}")
            logger.error(f"PVS error: {exc}", exc_info=True)
        finally:
            self._set_status(False, "Disconnected")

    def _on_task_done(self, future):
        """Called on BLE thread when the PVS task finishes."""
        try:
            exc = future.exception()
            if exc:
                logger.error(f"PVS task exception: {exc}", exc_info=exc)
        except Exception:
            pass
        self._set_status(False, "Disconnected")
