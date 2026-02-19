"""Polar Verity Sense BLE connection manager.

Manages the BLE connection to a Polar Verity Sense device using bleak.
Runs BLE operations in a background thread with its own asyncio event loop
(same pattern as GenkiWaveManager).

Uses SDK Mode (type 0x09) to enable raw high-frequency data streams:
  - ACC  (0x02): accelerometer, 52Hz, 16-bit, 8G, 3ch
  - GYR  (0x05): gyroscope, 52Hz, 16-bit, 2000dps, 3ch
  - MAG  (0x06): magnetometer, 50Hz, 16-bit, 50G, 3ch
  - PPI  (0x03): pulse-to-pulse interval (HRV) — mutually exclusive with SDK mode

Note: PPG (0x15) is NOT supported on this device firmware
(returns INVALID_MEASUREMENT_TYPE). SDK mode and PPI are mutually exclusive.

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
    PMD_TYPE_ACC, PMD_TYPE_PPI, PMD_TYPE_GYR, PMD_TYPE_MAG,
    parse_pmd_data,
    build_sdk_cmd, build_stop_command,
    PVSDataPacket,
)

logger = logging.getLogger(__name__)

# Polar Verity Sense PMD Service UUIDs
PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

# Standard BLE Heart Rate Service
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# Device name prefixes to scan for
PVS_NAME_PREFIXES = ("Polar Sense", "Polar Verity Sense")

# SDK Mode command bytes
SDK_MODE_ENABLE = bytearray([0x02, 0x09])
SDK_MODE_DISABLE = bytearray([0x03, 0x09])


@dataclass
class PVSSample:
    """A single timestamped sample from the Polar Verity Sense.

    Depending on which streams are active, some fields may be None.
    """
    timestamp: float
    acc: Optional[tuple] = None        # (x, y, z) in mg
    gyro: Optional[tuple] = None       # (x, y, z) in dps
    mag: Optional[tuple] = None        # (x, y, z) in Gauss/10
    ppi_ms: Optional[int] = None       # Pulse-to-pulse interval in ms
    ppi_hr: Optional[int] = None       # Heart rate from PPI
    hr_bpm: Optional[int] = None       # Heart rate from BLE HR service


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

    Uses SDK Mode to enable raw high-frequency data streams. The stream
    settings are hardcoded to the known-good values from the working test
    script (test_pvs_sdk_stream.py), avoiding the unreliable query-then-start
    approach.

    PPG data is fed into a PPGHeartRateCalculator to derive HR in BPM.

    Usage:
        manager = PolarVeritySenseManager()
        manager.connect()  # scans and connects
        ...
        samples = manager.poll()  # returns list of PVSSample
        ...
        manager.disconnect()
        manager.shutdown()
    """

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
        self.enable_mag = True
        # PPI: disabled by default — mutually exclusive with SDK mode on this device.
        # Enable PPI only if you want HR/PPI data without IMU streams.
        self.enable_ppi = False
        self.enable_hr = True     # Standard BLE Heart Rate service (works in SDK mode too)

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
        """Convert parsed packet to PVSSample objects and enqueue them.

        Uses wall-clock time (time.time()) as the base for the last sample in
        the packet, then reconstructs earlier sample times by subtracting
        index * dt. This keeps all timestamps in Unix epoch space (compatible
        with HR samples from the BLE HR service) while still giving each sample
        its correct evenly-spaced timestamp.
        """
        # Sample rates for each stream type (Hz)
        _SAMPLE_RATES = {
            0x02: 52.0,   # ACC
            0x05: 52.0,   # GYR
            0x06: 50.0,   # MAG
        }
        sample_rate = _SAMPLE_RATES.get(packet.measurement_type, 50.0)
        dt = 1.0 / sample_rate
        now = time.time()

        with self._samples_lock:
            if packet.acc_samples:
                n = len(packet.acc_samples)
                # now = time of last sample; earlier samples go back by dt each
                for i, s in enumerate(packet.acc_samples):
                    t = now - (n - 1 - i) * dt
                    self._samples.append(PVSSample(
                        timestamp=t,
                        acc=(s.x, s.y, s.z),
                    ))

            if packet.gyro_samples:
                n = len(packet.gyro_samples)
                for i, s in enumerate(packet.gyro_samples):
                    t = now - (n - 1 - i) * dt
                    self._samples.append(PVSSample(
                        timestamp=t,
                        gyro=(s.x, s.y, s.z),
                    ))

            if packet.mag_samples:
                n = len(packet.mag_samples)
                for i, s in enumerate(packet.mag_samples):
                    t = now - (n - 1 - i) * dt
                    self._samples.append(PVSSample(
                        timestamp=t,
                        mag=(s.x, s.y, s.z),
                    ))

            if packet.ppi_samples:
                for s in packet.ppi_samples:
                    self._samples.append(PVSSample(
                        timestamp=now,
                        ppi_ms=s.ppi_ms,
                        ppi_hr=s.hr,
                    ))

    def _on_pmd_data(self, sender, data: bytearray):
        """Callback for PMD Data notifications."""
        packet = parse_pmd_data(data)
        if packet:
            self._enqueue_samples(packet)

    def _on_hr_notification(self, sender, data: bytearray):
        """Callback for standard BLE Heart Rate Measurement notifications.

        Heart Rate Measurement format (Bluetooth SIG):
          Byte 0: Flags
            - Bit 0: HR format (0 = uint8, 1 = uint16)
            - Bit 4: RR-interval present
          Byte 1 (or 1-2): Heart rate value
          Remaining: RR intervals (uint16 LE, in 1/1024 sec units)
        """
        if len(data) < 2:
            return

        flags = data[0]
        hr_format_16bit = bool(flags & 0x01)

        if hr_format_16bit:
            if len(data) < 3:
                return
            hr_bpm = struct.unpack_from('<H', data, 1)[0]
        else:
            hr_bpm = data[1]

        now = time.time()
        with self._samples_lock:
            self._samples.append(PVSSample(
                timestamp=now,
                hr_bpm=hr_bpm,
            ))

        logger.debug(f"PVS HR: {hr_bpm} bpm")

    def _on_cp_response(self, sender, data: bytearray):
        """Callback for PMD Control Point notifications (responses).

        Response format: [0xF0, op_code, measurement_type, status, ...]
        """
        logger.debug(f"PVS CP notification: {data.hex()}")
        if len(data) >= 4:
            logger.info(f"  [CTRL] Op:0x{data[1]:02x} Type:0x{data[2]:02x} "
                        f"Status:0x{data[3]:02x}")
        self._cp_response_data = data
        if self._cp_response_event:
            self._cp_response_event.set()

    async def _send_cmd(self, client: BleakClient, cmd: bytearray,
                        label: str, timeout: float = 3.0) -> bool:
        """Send a PMD command and wait for success response.

        Returns True if command succeeded (status 0x00 or 0x06=already active).
        """
        self._cp_response_event = asyncio.Event()
        self._cp_response_data = None

        logger.info(f"  Sending {label}: {cmd.hex()}")
        await client.write_gatt_char(PMD_CONTROL_UUID, cmd, response=True)

        try:
            await asyncio.wait_for(self._cp_response_event.wait(), timeout=timeout)
            resp = self._cp_response_data
            if resp and len(resp) >= 4:
                status = resp[3]
                if status in (0x00, 0x06):  # 0x06 = already active
                    logger.info(f"  -> {label} OK (status=0x{status:02x})")
                    return True
                else:
                    logger.warning(f"  -> {label} FAILED (status=0x{status:02x})")
                    return False
        except asyncio.TimeoutError:
            logger.warning(f"  -> {label} TIMEOUT")

        return False

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

                # Subscribe to Control Point notifications (for responses)
                logger.info("PVS: Subscribing to PMD Control Point...")
                await client.start_notify(PMD_CONTROL_UUID, self._on_cp_response)
                await asyncio.sleep(0.5)

                # Subscribe to Data notifications
                logger.info("PVS: Subscribing to PMD Data...")
                await client.start_notify(PMD_DATA_UUID, self._on_pmd_data)
                await asyncio.sleep(0.3)

                # Subscribe to standard BLE Heart Rate service if available
                hr_streaming = False
                if self.enable_hr:
                    try:
                        logger.info("PVS: Subscribing to Heart Rate service...")
                        await client.start_notify(
                            HR_MEASUREMENT_UUID, self._on_hr_notification
                        )
                        hr_streaming = True
                        logger.info("PVS: Heart Rate notifications started")
                        await asyncio.sleep(0.2)
                    except Exception as e:
                        logger.warning(f"PVS: HR subscription failed (non-fatal): {e}")

                active_streams = []

                # SDK mode and PPI are mutually exclusive on this device:
                #   - SDK mode active  -> PPI returns INVALID_STATE (0x0c)
                #   - PPI active       -> SDK mode returns INVALID_STATE (0x0c)
                # Strategy: if any IMU stream is requested, use SDK mode (skip PPI).
                # If only PPI/HR is requested, skip SDK mode so PPI can start.
                need_sdk = self.enable_acc or self.enable_gyro or self.enable_mag

                if not need_sdk and self.enable_ppi:
                    # Normal mode: disable SDK mode first to clear any stale device
                    # state from a previous session, then start PPI.
                    logger.info("PVS: Disabling SDK Mode (clear stale state)...")
                    try:
                        await client.write_gatt_char(
                            PMD_CONTROL_UUID, SDK_MODE_DISABLE, response=True
                        )
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass  # Ignore — device may not have had SDK mode active

                    ppi_cmd = bytearray([0x02, PMD_TYPE_PPI])
                    ok = await self._send_cmd(client, ppi_cmd, "START_PPI")
                    if ok:
                        active_streams.append(PMD_TYPE_PPI)
                    await asyncio.sleep(0.2)

                if need_sdk:
                    # Enable SDK Mode — required for raw IMU streaming.
                    # Disables internal HR algorithms; PPI cannot run simultaneously.
                    logger.info("PVS: Enabling SDK Mode...")
                    sdk_ok = await self._send_cmd(client, SDK_MODE_ENABLE, "SDK_MODE_ENABLE")
                    if not sdk_ok:
                        logger.warning("PVS: SDK Mode failed — falling back to PPI/HR only")
                        if self.enable_ppi:
                            ppi_cmd = bytearray([0x02, PMD_TYPE_PPI])
                            ok = await self._send_cmd(client, ppi_cmd, "START_PPI")
                            if ok:
                                active_streams.append(PMD_TYPE_PPI)
                    await asyncio.sleep(0.5)

                # Start IMU streams (only attempted when SDK mode is active)
                if self.enable_acc and need_sdk:
                    # 52Hz, 16-bit, 8G, 3ch
                    cmd = build_sdk_cmd(PMD_TYPE_ACC, 52, 16, 8, 3)
                    ok = await self._send_cmd(client, cmd, "START_ACC")
                    if ok:
                        active_streams.append(PMD_TYPE_ACC)
                    await asyncio.sleep(0.2)

                if self.enable_gyro and need_sdk:
                    # 52Hz, 16-bit, 2000dps, 3ch
                    cmd = build_sdk_cmd(PMD_TYPE_GYR, 52, 16, 2000, 3)
                    ok = await self._send_cmd(client, cmd, "START_GYR")
                    if ok:
                        active_streams.append(PMD_TYPE_GYR)
                    await asyncio.sleep(0.2)

                if self.enable_mag and need_sdk:
                    # 50Hz, 16-bit, 50G, 3ch
                    cmd = build_sdk_cmd(PMD_TYPE_MAG, 50, 16, 50, 3)
                    ok = await self._send_cmd(client, cmd, "START_MAG")
                    if ok:
                        active_streams.append(PMD_TYPE_MAG)
                    await asyncio.sleep(0.2)

                if not active_streams and not hr_streaming:
                    self._set_status(False, "No streams started")
                    logger.error("PVS: Failed to start any streams")
                    return

                stream_names = {
                    PMD_TYPE_ACC: "ACC", PMD_TYPE_GYR: "GYR",
                    PMD_TYPE_MAG: "MAG", PMD_TYPE_PPI: "PPI",
                }
                names = [stream_names.get(s, f"0x{s:02x}") for s in active_streams]
                if hr_streaming:
                    names.append("HR")
                self._set_status(True, f"Streaming: {', '.join(names)}")
                self._connecting = False
                logger.info(f"PVS: Streaming {', '.join(names)}")

                # Keep alive until cancelled
                while not self._cancel:
                    await asyncio.sleep(0.1)

                logger.info("PVS: Stopping streams...")

                # Stop all active streams
                for stream_type in active_streams:
                    try:
                        stop_cmd = bytearray([0x03, stream_type])
                        await client.write_gatt_char(PMD_CONTROL_UUID, stop_cmd, response=True)
                        logger.info(f"PVS: Stopped stream 0x{stream_type:02x}")
                    except Exception:
                        pass

                await asyncio.sleep(0.5)

                # Disable SDK Mode (re-enable internal algorithms)
                try:
                    await client.write_gatt_char(PMD_CONTROL_UUID, SDK_MODE_DISABLE,
                                                  response=True)
                    logger.info("PVS: SDK Mode disabled")
                except Exception:
                    pass

                # Stop notifications
                try:
                    await client.stop_notify(PMD_DATA_UUID)
                    await client.stop_notify(PMD_CONTROL_UUID)
                    if hr_streaming:
                        await client.stop_notify(HR_MEASUREMENT_UUID)
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
