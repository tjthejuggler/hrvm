import asyncio
import logging
import struct
import time
import random
import math
from typing import Optional, List, Tuple
from multiprocessing.connection import Connection
import numpy as np
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from src.ble.ring_buffer import RingBuffer
from src.ble.dbus_agent import register_agent
from src.utils.ipc import HRBatch, ACCBatch, ECGBatch, BLECommand

# Configure logging
logger = logging.getLogger("ble_process")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Standard BLE Heart Rate Measurement UUID
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# Polar Measurement Data (PMD) Service UUIDs
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

# PMD measurement types
PMD_TYPE_ECG = 0x00
PMD_TYPE_ACC = 0x02


def parse_heart_rate_data(data: bytearray) -> Tuple[int, List[float]]:
    """Parse BLE Heart Rate Measurement characteristic.

    Returns: (heart_rate_bpm, list_of_rr_intervals_in_ms)
    """
    flags = data[0]
    hr_format = flags & 0x01
    rr_present = (flags & 0x10) >> 4
    current_offset = 1

    # Heart rate value
    if hr_format == 0:
        hr = data[current_offset]
        current_offset += 1
    else:
        hr = int.from_bytes(data[current_offset:current_offset + 2], byteorder='little')
        current_offset += 2

    # Skip Energy Expended if present (Bit 3)
    if (flags & 0x08) >> 3:
        current_offset += 2

    # RR intervals (1/1024 second units -> milliseconds)
    rr_intervals: List[float] = []
    if rr_present:
        while current_offset < len(data):
            rr_raw = int.from_bytes(data[current_offset:current_offset + 2], byteorder='little')
            rr_ms = (rr_raw / 1024.0) * 1000.0
            rr_intervals.append(round(rr_ms, 1))
            current_offset += 2

    return hr, rr_intervals


def parse_acc_data(data: bytearray) -> List[Tuple[int, int, int]]:
    """Parse Polar PMD ACC notification data.

    The PMD ACC frame format:
    - Byte 0: Measurement type (0x02 = ACC)
    - Byte 1-8: Timestamp (uint64, little-endian, nanoseconds)
    - Byte 9: Frame type
    - Remaining: ACC samples as signed 16-bit little-endian (x, y, z triplets)

    Returns: list of (x_mg, y_mg, z_mg) tuples
    """
    if len(data) < 10:
        return []

    # Skip header: type(1) + timestamp(8) + frame_type(1) = 10 bytes
    offset = 10
    samples = []

    while offset + 6 <= len(data):
        x = struct.unpack_from('<h', data, offset)[0]
        y = struct.unpack_from('<h', data, offset + 2)[0]
        z = struct.unpack_from('<h', data, offset + 4)[0]
        samples.append((x, y, z))
        offset += 6

    return samples


def parse_ecg_data(data: bytearray) -> List[int]:
    """Parse Polar PMD ECG notification data.

    The PMD ECG frame format:
    - Byte 0: Measurement type (0x00 = ECG)
    - Byte 1-8: Timestamp (uint64, little-endian, nanoseconds)
    - Byte 9: Frame type
    - Remaining: ECG samples as signed 24-bit little-endian (3 bytes each)
      Polar H10 ECG uses 14-bit resolution packed in 3 bytes (sign-extended).

    Returns: list of ECG sample values (microvolts)
    """
    if len(data) < 10:
        return []

    # Skip header: type(1) + timestamp(8) + frame_type(1) = 10 bytes
    offset = 10
    samples = []

    while offset + 3 <= len(data):
        # 3-byte signed little-endian
        raw = data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)
        # Sign extend from 24-bit
        if raw & 0x800000:
            raw -= 0x1000000
        samples.append(raw)
        offset += 3

    return samples


def build_pmd_start_command(meas_type: int, sample_rate: int,
                            resolution: int, range_g: int = 0) -> bytearray:
    """Build a PMD START command with correct Polar format.

    Format: 0x02 <type> [<setting_type:1> <array_len:1> <value:LE16>]...

    This matches the format proven to work in test_acc_mtu_clean.py.
    """
    cmd = bytearray([0x02, meas_type])
    # Sample rate (type=0x00, array_len=0x01, value LE16)
    cmd.append(0x00)
    cmd.append(0x01)
    cmd.extend(struct.pack('<H', sample_rate))
    # Resolution (type=0x01, array_len=0x01, value LE16)
    cmd.append(0x01)
    cmd.append(0x01)
    cmd.extend(struct.pack('<H', resolution))
    # Range (type=0x02) - only for ACC, not ECG
    if range_g > 0:
        cmd.append(0x02)
        cmd.append(0x01)
        cmd.extend(struct.pack('<H', range_g))
    return cmd


class BleakManager:
    """
    Manages BLE connection to Polar H10 and streams HR + ACC + ECG data.
    Uses the standard BLE Heart Rate Measurement characteristic for HR/RR,
    and the Polar PMD service for accelerometer and ECG data.
    Runs in dedicated process with asyncio event loop.

    Key learnings applied from test_acc_mtu_clean.py:
    - MTU must be negotiated via _acquire_mtu() for PMD data frames (>200 bytes)
    - Device must be paired before accessing PMD service
    - PMD Data must use D-Bus StartNotify (use_start_notify=True) to avoid
      bluetoothd crashes with AcquireNotify on high-freq PMD streams
    - PMD start commands use format: type(1) + array_len(1) + value(2) per setting

    Auto-reconnect: When an unexpected disconnect occurs (not user-initiated),
    the manager will automatically retry connection with exponential backoff
    (2s, 4s, 8s, ... up to 30s) and re-enable all previously active streams.
    """

    POLAR_H10_NAME_PREFIX = "Polar H10"

    # Auto-reconnect settings
    RECONNECT_INITIAL_DELAY = 2.0   # seconds
    RECONNECT_MAX_DELAY = 30.0      # seconds
    RECONNECT_BACKOFF_FACTOR = 2.0

    def __init__(self, data_pipe: Connection, control_pipe: Connection, mock_mode: bool = False):
        self.data_pipe = data_pipe
        self.control_pipe = control_pipe
        self.client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self.sequence_number = 0
        self.acc_sequence_number = 0
        self.ecg_sequence_number = 0
        self.is_streaming = False
        self.should_exit = False
        self.mock_mode = mock_mode
        self.acc_streaming = False
        self.ecg_streaming = False
        self._agent_registered = False
        self._pmd_response_event: Optional[asyncio.Event] = None
        self._pmd_response_ok = False

        # Auto-reconnect state
        self._user_disconnect = False      # True when user explicitly disconnects
        self._reconnecting = False         # True while auto-reconnect loop is active
        self._was_streaming_hr = False     # Track which streams were active before disconnect
        self._was_streaming_acc = False
        self._was_streaming_ecg = False
        self._reconnect_task: Optional[asyncio.Task] = None

    async def _ensure_agent(self):
        """Register the auto-accept D-Bus agent once (idempotent)."""
        if self._agent_registered:
            return
        self._agent_registered = await register_agent()

    async def scan_and_connect(self, timeout: float = 10.0) -> bool:
        """Scan for Polar H10 and establish connection with MTU negotiation.

        Applies learnings from test_acc_mtu_clean.py:
        1. Connect to device
        2. Negotiate MTU (critical for PMD data frames >200 bytes)
        3. Pair device (required for PMD service access)
        """
        if self.mock_mode:
            logger.info("[MOCK] Scanning for Polar H10...")
            await asyncio.sleep(1.0)
            logger.info("[MOCK] Found device: Polar H10 Mock (00:11:22:33:44:55)")
            logger.info("[MOCK] Connected to Polar H10 Mock")
            return True

        logger.info("Scanning for Polar H10...")

        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name and d.name.startswith(self.POLAR_H10_NAME_PREFIX),
            timeout=timeout
        )

        if not device:
            logger.error("Polar H10 not found during scan.")
            return False

        logger.info(f"Found device: {device.name} ({device.address})")

        try:
            self.client = BleakClient(
                device,
                disconnected_callback=self._on_disconnect,
                timeout=30.0
            )
            logger.info("Attempting connection...")
            await self.client.connect()
            logger.info(f"Connected: {self.client.is_connected}")

            if not self.client.is_connected:
                logger.error("Connection reported as not connected after connect()")
                return False

            # MTU negotiation (CRITICAL for PMD data - frames are >200 bytes)
            mtu_before = self.client.mtu_size
            logger.info(f"MTU before negotiation: {mtu_before}")
            try:
                await self.client._backend._acquire_mtu()
                mtu_after = self.client.mtu_size
                logger.info(f"MTU after negotiation: {mtu_after}")
                if mtu_after < 100:
                    logger.warning("MTU still too small for PMD data!")
            except Exception as e:
                logger.warning(f"MTU negotiation failed (non-fatal): {e}")

            # Pair device (required for PMD service access)
            try:
                await self.client.pair()
                logger.info("Device paired successfully")
            except Exception as e:
                logger.warning(f"Pairing result (may already be paired): {e}")

            self.device = device
            return True

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            import traceback
            logger.error(f"Connection traceback: {traceback.format_exc()}")
            return False

    def _on_disconnect(self, client: BleakClient):
        """Handle BLE disconnection. Triggers auto-reconnect if not user-initiated."""
        logger.warning("Disconnected from device.")

        # Remember which streams were active before disconnect for re-enabling
        if self.is_streaming:
            self._was_streaming_hr = True
        if self.acc_streaming:
            self._was_streaming_acc = True
        if self.ecg_streaming:
            self._was_streaming_ecg = True

        self.is_streaming = False
        self.acc_streaming = False
        self.ecg_streaming = False

        if self._user_disconnect or self.should_exit:
            # User explicitly disconnected or app is exiting — don't reconnect
            logger.info("User-initiated disconnect; no auto-reconnect.")
            try:
                self.control_pipe.send({"status": "disconnected"})
            except Exception:
                pass
        else:
            # Unexpected disconnect — start auto-reconnect
            logger.info("Unexpected disconnect detected. Starting auto-reconnect...")
            try:
                self.control_pipe.send({"status": "reconnecting"})
            except Exception:
                pass
            self._reconnecting = True
            # Schedule the reconnect coroutine on the running event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._reconnect_task = loop.create_task(self._auto_reconnect())

    async def _auto_reconnect(self):
        """Attempt to reconnect with exponential backoff.

        Retries until successful, user sends disconnect/exit, or should_exit is set.
        On success, re-enables all streams that were active before the disconnect.
        """
        delay = self.RECONNECT_INITIAL_DELAY
        attempt = 0

        while not self.should_exit and not self._user_disconnect:
            attempt += 1
            logger.info(f"Auto-reconnect attempt #{attempt} in {delay:.1f}s...")
            await asyncio.sleep(delay)

            # Check again after sleep — user may have cancelled
            if self.should_exit or self._user_disconnect:
                break

            try:
                success = await self.scan_and_connect(timeout=10.0)
                if success:
                    logger.info(f"Auto-reconnect succeeded on attempt #{attempt}.")

                    # Re-enable HR stream
                    if self._was_streaming_hr:
                        await self.enable_hr_stream()
                        await asyncio.sleep(1.0)

                    # Re-subscribe to PMD and re-enable ACC/ECG
                    if self._was_streaming_acc or self._was_streaming_ecg:
                        pmd_ok = await self._subscribe_pmd()
                        if pmd_ok:
                            if self._was_streaming_acc:
                                await self.enable_acc_stream()
                                await asyncio.sleep(0.5)
                            if self._was_streaming_ecg:
                                await self.enable_ecg_stream()

                    self._reconnecting = False
                    try:
                        self.control_pipe.send({"status": "connected"})
                    except Exception:
                        pass
                    logger.info("All streams re-enabled after reconnect.")
                    return
                else:
                    logger.warning(f"Auto-reconnect attempt #{attempt} failed (scan/connect).")
            except Exception as e:
                logger.warning(f"Auto-reconnect attempt #{attempt} error: {e}")

            # Exponential backoff
            delay = min(delay * self.RECONNECT_BACKOFF_FACTOR, self.RECONNECT_MAX_DELAY)

        # Loop exited without success
        self._reconnecting = False
        logger.info("Auto-reconnect cancelled (user disconnect or exit).")
        try:
            self.control_pipe.send({"status": "disconnected"})
        except Exception:
            pass

    async def enable_hr_stream(self) -> None:
        """Subscribe to the standard BLE Heart Rate Measurement characteristic."""
        if self.mock_mode:
            logger.info("[MOCK] Subscribing to HR notifications...")
            self.is_streaming = True
            asyncio.create_task(self._generate_mock_data())
            return

        if not self.client or not self.client.is_connected:
            logger.error("Cannot enable HR stream: Not connected.")
            return

        # Log discovered services for debugging
        logger.info("Listing discovered services...")
        for service in self.client.services:
            logger.info(f"  Service: {service.uuid}")
            for char in service.characteristics:
                logger.info(f"    Char: {char.uuid} ({char.properties})")

        logger.info("Subscribing to HR Measurement notifications...")
        try:
            await self.client.start_notify(HR_MEASUREMENT_UUID, self._hr_notification_handler)
            self.is_streaming = True
            logger.info("HR stream enabled successfully.")
        except Exception as e:
            logger.error(f"Failed to subscribe to HR notifications: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def _subscribe_pmd(self) -> bool:
        """Subscribe to PMD Control and Data characteristics.

        Uses use_start_notify=True for PMD Data to avoid bluetoothd crashes
        with AcquireNotify on high-frequency PMD streams.

        Returns True if subscriptions succeeded.
        """
        if not self.client or not self.client.is_connected:
            logger.error("Cannot subscribe to PMD: Not connected.")
            return False

        # Check if PMD service is available
        pmd_control = None
        pmd_data = None
        for service in self.client.services:
            for char in service.characteristics:
                if char.uuid.lower() == PMD_CONTROL_UUID.lower():
                    pmd_control = char
                if char.uuid.lower() == PMD_DATA_UUID.lower():
                    pmd_data = char

        if not pmd_control or not pmd_data:
            logger.warning("PMD service not found on device. PMD streaming unavailable.")
            return False

        logger.info(f"PMD Control char properties: {pmd_control.properties}")
        logger.info(f"PMD Data char properties: {pmd_data.properties}")

        try:
            # Subscribe to PMD Control Point indications (for responses)
            logger.info("Subscribing to PMD Control Point indications...")
            await self.client.start_notify(PMD_CONTROL_UUID, self._pmd_control_handler)
            await asyncio.sleep(0.3)

            # Subscribe to PMD Data with use_start_notify=True (D-Bus signals)
            # AcquireNotify socket crashes bluetoothd on high-freq PMD streams
            logger.info("Subscribing to PMD Data (use_start_notify=True)...")
            await self.client.start_notify(
                PMD_DATA_UUID, self._pmd_data_handler,
                bluez={"use_start_notify": True}
            )
            await asyncio.sleep(0.3)
            logger.info("PMD subscriptions active.")
            return True

        except Exception as e:
            logger.warning(f"Failed to subscribe to PMD: {e}")
            import traceback
            logger.warning(f"PMD subscribe traceback: {traceback.format_exc()}")
            return False

    async def enable_acc_stream(self, sample_rate: int = 25) -> None:
        """Start ACC streaming via Polar PMD service.

        Uses the proven command format from test_acc_mtu_clean.py:
        0x02 <type> [<setting_type:1> <array_len:1> <value:LE16>]...
        """
        if self.mock_mode:
            logger.info("[MOCK] Starting ACC stream...")
            self.acc_streaming = True
            asyncio.create_task(self._generate_mock_acc_data())
            return

        if not self.client or not self.client.is_connected:
            logger.error("Cannot enable ACC stream: Not connected.")
            return

        try:
            start_cmd = build_pmd_start_command(
                meas_type=PMD_TYPE_ACC,
                sample_rate=sample_rate,
                resolution=16,
                range_g=8
            )

            logger.info(f"Writing ACC start command: {start_cmd.hex()}")
            self._pmd_response_event = asyncio.Event()
            self._pmd_response_ok = False
            await self.client.write_gatt_char(PMD_CONTROL_UUID, start_cmd, response=True)

            # Wait for PMD control response to confirm start
            try:
                await asyncio.wait_for(self._pmd_response_event.wait(), timeout=3.0)
                if self._pmd_response_ok:
                    self.acc_streaming = True
                    logger.info(f"ACC stream confirmed at {sample_rate} Hz.")
                else:
                    logger.warning("PMD ACC start command returned error.")
            except asyncio.TimeoutError:
                self.acc_streaming = True
                logger.warning("Timeout waiting for ACC start confirmation. "
                               "Assuming stream started.")

        except Exception as e:
            logger.warning(f"Failed to start ACC stream: {e}")
            import traceback
            logger.warning(f"ACC traceback: {traceback.format_exc()}")

    async def enable_ecg_stream(self, sample_rate: int = 130) -> None:
        """Start ECG streaming via Polar PMD service.

        ECG uses PMD type 0x00, 130Hz sample rate, 14-bit resolution.
        """
        if self.mock_mode:
            logger.info("[MOCK] Starting ECG stream...")
            self.ecg_streaming = True
            asyncio.create_task(self._generate_mock_ecg_data())
            return

        if not self.client or not self.client.is_connected:
            logger.error("Cannot enable ECG stream: Not connected.")
            return

        try:
            start_cmd = build_pmd_start_command(
                meas_type=PMD_TYPE_ECG,
                sample_rate=sample_rate,
                resolution=14,
                range_g=0  # No range setting for ECG
            )

            logger.info(f"Writing ECG start command: {start_cmd.hex()}")
            self._pmd_response_event = asyncio.Event()
            self._pmd_response_ok = False
            await self.client.write_gatt_char(PMD_CONTROL_UUID, start_cmd, response=True)

            try:
                await asyncio.wait_for(self._pmd_response_event.wait(), timeout=3.0)
                if self._pmd_response_ok:
                    self.ecg_streaming = True
                    logger.info(f"ECG stream confirmed at {sample_rate} Hz.")
                else:
                    logger.warning("PMD ECG start command returned error.")
            except asyncio.TimeoutError:
                self.ecg_streaming = True
                logger.warning("Timeout waiting for ECG start confirmation. "
                               "Assuming stream started.")

        except Exception as e:
            logger.warning(f"Failed to start ECG stream: {e}")
            import traceback
            logger.warning(f"ECG traceback: {traceback.format_exc()}")

    def _hr_notification_handler(self, sender, data: bytearray) -> None:
        """Handle incoming HR Measurement notifications."""
        hr_bpm, rr_intervals = parse_heart_rate_data(data)

        batch = HRBatch(
            timestamp_unix=time.time(),
            heart_rate_bpm=hr_bpm,
            rr_intervals_ms=rr_intervals,
            sequence_number=self.sequence_number
        )
        self.sequence_number += 1

        try:
            self.data_pipe.send(batch)
        except Exception as e:
            logger.error(f"Failed to send HR batch to pipe: {e}")

    def _pmd_control_handler(self, sender, data: bytearray) -> None:
        """Handle PMD Control Point indications (responses to commands).

        Correct format: f0 <op_code> <measurement_type> <status> [<settings...>]
        - data[0] = 0xf0 (response indicator)
        - data[1] = original op code (0x01=query, 0x02=start, 0x03=stop)
        - data[2] = measurement type (0x00=ECG, 0x02=ACC)
        - data[3] = status (0x00=success, non-zero=error)
        """
        if len(data) < 4:
            logger.debug(f"PMD Control response (short): {data.hex()}")
            if self._pmd_response_event:
                self._pmd_response_event.set()
            return

        response_indicator = data[0]
        op_code = data[1]
        meas_type = data[2]
        status = data[3]

        self._pmd_response_ok = (status == 0)
        type_names = {0x00: "ECG", 0x02: "ACC"}
        type_str = type_names.get(meas_type, f"0x{meas_type:02x}")
        status_str = "SUCCESS" if status == 0 else f"ERROR({status})"

        logger.info(f"PMD Control: op=0x{op_code:02x}, type={type_str}, "
                     f"status={status_str}, raw={data.hex()}")

        if self._pmd_response_event:
            self._pmd_response_event.set()

    def _pmd_data_handler(self, sender, data: bytearray) -> None:
        """Route PMD Data notifications to the correct handler based on type byte."""
        if len(data) < 1:
            return

        meas_type = data[0]
        if meas_type == PMD_TYPE_ACC:
            self._acc_notification_handler(sender, data)
        elif meas_type == PMD_TYPE_ECG:
            self._ecg_notification_handler(sender, data)
        else:
            logger.debug(f"Unknown PMD data type: 0x{meas_type:02x}, len={len(data)}")

    def _acc_notification_handler(self, sender, data: bytearray) -> None:
        """Handle incoming PMD ACC data notifications."""
        if len(data) < 10:
            logger.debug(f"ACC notification too short ({len(data)} bytes): {data.hex()}")
            return

        samples = parse_acc_data(data)
        if not samples:
            logger.debug(f"ACC notification parsed 0 samples from {len(data)} bytes")
            return

        if self.acc_sequence_number % 25 == 0:
            logger.debug(f"ACC batch #{self.acc_sequence_number}: "
                         f"{len(samples)} samples, first={samples[0]}")

        batch = ACCBatch(
            timestamp_unix=time.time(),
            samples=samples,
            sample_rate=25,
            sequence_number=self.acc_sequence_number
        )
        self.acc_sequence_number += 1

        try:
            self.data_pipe.send(batch)
        except Exception as e:
            logger.error(f"Failed to send ACC batch to pipe: {e}")

    def _ecg_notification_handler(self, sender, data: bytearray) -> None:
        """Handle incoming PMD ECG data notifications."""
        if len(data) < 10:
            logger.debug(f"ECG notification too short ({len(data)} bytes): {data.hex()}")
            return

        samples = parse_ecg_data(data)
        if not samples:
            logger.debug(f"ECG notification parsed 0 samples from {len(data)} bytes")
            return

        if self.ecg_sequence_number % 50 == 0:
            logger.debug(f"ECG batch #{self.ecg_sequence_number}: "
                         f"{len(samples)} samples, first={samples[0]}")

        batch = ECGBatch(
            timestamp_unix=time.time(),
            sample_rate=130,
            samples=np.array(samples, dtype=np.int32),
            sequence_number=self.ecg_sequence_number
        )
        self.ecg_sequence_number += 1

        try:
            self.data_pipe.send(batch)
        except Exception as e:
            logger.error(f"Failed to send ECG batch to pipe: {e}")

    async def _generate_mock_data(self):
        """Generate mock HR data for testing without device."""
        logger.info("[MOCK] Starting mock HR data generation...")
        batch_count = 0
        base_hr = 70
        rr_base = 857.0  # ~70 BPM in ms

        while self.is_streaming and not self.should_exit:
            hr = base_hr + random.randint(-5, 5)
            n_rr = random.choice([1, 1, 2])
            rr_intervals = [round(rr_base + random.gauss(0, 30), 1) for _ in range(n_rr)]

            batch = HRBatch(
                timestamp_unix=time.time(),
                heart_rate_bpm=hr,
                rr_intervals_ms=rr_intervals,
                sequence_number=self.sequence_number
            )
            self.sequence_number += 1
            batch_count += 1

            try:
                self.data_pipe.send(batch)
                if batch_count % 10 == 0:
                    logger.info(f"[MOCK] Sent batch #{batch_count} HR={hr} RR={rr_intervals}")
            except Exception as e:
                logger.error(f"[MOCK] Failed to send mock batch: {e}")
                break

            await asyncio.sleep(1.0)

        logger.info(f"[MOCK] Mock data generation stopped. Total batches: {batch_count}")

    async def _generate_mock_acc_data(self):
        """Generate mock ACC data for testing without device."""
        logger.info("[MOCK] Starting mock ACC data generation...")
        batch_count = 0

        while self.acc_streaming and not self.should_exit:
            samples = []
            for _ in range(25):
                x = random.randint(-100, 100)
                y = random.randint(-100, 100)
                z = 1000 + random.randint(-50, 50)
                samples.append((x, y, z))

            batch = ACCBatch(
                timestamp_unix=time.time(),
                samples=samples,
                sample_rate=25,
                sequence_number=self.acc_sequence_number
            )
            self.acc_sequence_number += 1
            batch_count += 1

            try:
                self.data_pipe.send(batch)
            except Exception as e:
                logger.error(f"[MOCK] Failed to send mock ACC batch: {e}")
                break

            await asyncio.sleep(1.0)

        logger.info(f"[MOCK] Mock ACC data generation stopped. Total batches: {batch_count}")

    async def _generate_mock_ecg_data(self):
        """Generate mock ECG data for testing without device."""
        logger.info("[MOCK] Starting mock ECG data generation...")
        batch_count = 0
        t = 0.0
        sample_rate = 130
        dt = 1.0 / sample_rate

        while self.ecg_streaming and not self.should_exit:
            # Generate ~130 samples per batch (1 second at 130Hz)
            samples = []
            for _ in range(sample_rate):
                # Simple synthetic ECG-like waveform
                # P wave + QRS complex + T wave
                phase = (t * 1.2) % 1.0  # ~72 BPM
                val = 0
                if 0.1 < phase < 0.15:  # P wave
                    val = int(200 * math.sin((phase - 0.1) / 0.05 * math.pi))
                elif 0.2 < phase < 0.22:  # Q dip
                    val = int(-300 * math.sin((phase - 0.2) / 0.02 * math.pi))
                elif 0.22 < phase < 0.26:  # R peak
                    val = int(3000 * math.sin((phase - 0.22) / 0.04 * math.pi))
                elif 0.26 < phase < 0.28:  # S dip
                    val = int(-500 * math.sin((phase - 0.26) / 0.02 * math.pi))
                elif 0.35 < phase < 0.45:  # T wave
                    val = int(400 * math.sin((phase - 0.35) / 0.1 * math.pi))
                val += random.randint(-20, 20)  # noise
                samples.append(val)
                t += dt

            batch = ECGBatch(
                timestamp_unix=time.time(),
                sample_rate=sample_rate,
                samples=np.array(samples, dtype=np.int32),
                sequence_number=self.ecg_sequence_number
            )
            self.ecg_sequence_number += 1
            batch_count += 1

            try:
                self.data_pipe.send(batch)
            except Exception as e:
                logger.error(f"[MOCK] Failed to send mock ECG batch: {e}")
                break

            await asyncio.sleep(1.0)

        logger.info(f"[MOCK] Mock ECG data generation stopped. Total batches: {batch_count}")

    def _cancel_reconnect(self):
        """Cancel any active auto-reconnect task."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            self._reconnect_task = None
            logger.info("Auto-reconnect task cancelled.")
        self._reconnecting = False
        self._was_streaming_hr = False
        self._was_streaming_acc = False
        self._was_streaming_ecg = False

    async def handle_control_messages(self) -> None:
        """Process commands from GUI (connect/disconnect/exit)."""
        try:
            while True:
                try:
                    if not self.control_pipe.poll(0):
                        break

                    msg = self.control_pipe.recv()
                    if isinstance(msg, BLECommand):
                        logger.info(f"Received command: {msg.command}")
                        if msg.command == "connect":
                            # User explicitly connecting — cancel any reconnect
                            self._user_disconnect = False
                            self._cancel_reconnect()

                            if self.mock_mode:
                                await self.scan_and_connect()
                                await self.enable_hr_stream()
                                await asyncio.sleep(1.0)
                                await self.enable_acc_stream()
                                await asyncio.sleep(0.5)
                                await self.enable_ecg_stream()
                                self.control_pipe.send({"status": "connected"})
                            elif not self.client or not self.client.is_connected:
                                try:
                                    if await self.scan_and_connect():
                                        await self.enable_hr_stream()
                                        await asyncio.sleep(1.0)
                                        # Subscribe to PMD once, then start streams
                                        pmd_ok = await self._subscribe_pmd()
                                        if pmd_ok:
                                            await self.enable_acc_stream()
                                            await asyncio.sleep(0.5)
                                            await self.enable_ecg_stream()
                                        self.control_pipe.send({"status": "connected"})
                                    else:
                                        self.control_pipe.send({"status": "disconnected"})
                                except Exception as e:
                                    logger.error(f"Connection/stream failed: {e}")
                                    import traceback
                                    logger.error(f"Traceback: {traceback.format_exc()}")
                                    self.control_pipe.send({"status": "disconnected"})
                        elif msg.command == "disconnect":
                            # User explicitly disconnecting — prevent auto-reconnect
                            self._user_disconnect = True
                            self._cancel_reconnect()

                            if self.mock_mode:
                                self.is_streaming = False
                                self.acc_streaming = False
                                self.ecg_streaming = False
                                logger.info("[MOCK] Disconnected")
                                self.control_pipe.send({"status": "disconnected"})
                            elif self.client and self.client.is_connected:
                                # Stop PMD streams
                                try:
                                    if self.ecg_streaming:
                                        stop_ecg = bytearray([0x03, PMD_TYPE_ECG])
                                        await self.client.write_gatt_char(
                                            PMD_CONTROL_UUID, stop_ecg, response=True)
                                        self.ecg_streaming = False
                                except Exception:
                                    pass
                                try:
                                    if self.acc_streaming:
                                        stop_acc = bytearray([0x03, PMD_TYPE_ACC])
                                        await self.client.write_gatt_char(
                                            PMD_CONTROL_UUID, stop_acc, response=True)
                                        self.acc_streaming = False
                                except Exception:
                                    pass
                                try:
                                    await self.client.stop_notify(HR_MEASUREMENT_UUID)
                                except Exception:
                                    pass
                                try:
                                    await self.client.stop_notify(PMD_DATA_UUID)
                                except Exception:
                                    pass
                                try:
                                    await self.client.stop_notify(PMD_CONTROL_UUID)
                                except Exception:
                                    pass
                                await self.client.disconnect()
                                self.control_pipe.send({"status": "disconnected"})
                            else:
                                # Not connected (maybe reconnecting was cancelled)
                                self.control_pipe.send({"status": "disconnected"})
                        elif msg.command == "exit":
                            self._cancel_reconnect()
                            self.should_exit = True
                except EOFError:
                    logger.warning("Control pipe closed.")
                    self.should_exit = True
                    break
                except Exception as e:
                    logger.error(f"Error handling control message: {e}")
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    break
        except Exception as e:
            logger.error(f"Critical error in control message loop: {e}")
            self.should_exit = True

    async def run(self) -> None:
        """Main event loop for BLE process."""
        logger.info(f"BLE Process started. Mock Mode: {self.mock_mode}")
        logger.info("Waiting for 'connect' command from GUI...")

        while not self.should_exit:
            await self.handle_control_messages()
            await asyncio.sleep(0.1)

        # Cleanup
        if not self.mock_mode and self.client and self.client.is_connected:
            try:
                if self.ecg_streaming:
                    stop_ecg = bytearray([0x03, PMD_TYPE_ECG])
                    await self.client.write_gatt_char(
                        PMD_CONTROL_UUID, stop_ecg, response=True)
            except Exception:
                pass
            try:
                if self.acc_streaming:
                    stop_acc = bytearray([0x03, PMD_TYPE_ACC])
                    await self.client.write_gatt_char(
                        PMD_CONTROL_UUID, stop_acc, response=True)
            except Exception:
                pass
            try:
                await self.client.stop_notify(HR_MEASUREMENT_UUID)
            except Exception:
                pass
            try:
                await self.client.stop_notify(PMD_DATA_UUID)
            except Exception:
                pass
            try:
                await self.client.stop_notify(PMD_CONTROL_UUID)
            except Exception:
                pass
            await self.client.disconnect()
        logger.info("BLE Process exiting.")


def ble_ingestion_main(data_pipe: Connection, control_pipe: Connection, mock_mode: bool = False):
    """Entry point for the BLE process."""
    manager = BleakManager(data_pipe, control_pipe, mock_mode=mock_mode)
    asyncio.run(manager.run())
