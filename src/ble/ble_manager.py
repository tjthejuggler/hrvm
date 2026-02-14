import asyncio
import logging
import time
import random
from typing import Optional, List, Tuple
from multiprocessing.connection import Connection
import numpy as np
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from src.ble.ring_buffer import RingBuffer
from src.utils.ipc import HRBatch, BLECommand

# Configure logging
logger = logging.getLogger("ble_process")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Standard BLE Heart Rate Measurement UUID
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


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


class BleakManager:
    """
    Manages BLE connection to Polar H10 and streams HR data.
    Uses the standard BLE Heart Rate Measurement characteristic.
    Runs in dedicated process with asyncio event loop.
    """

    POLAR_H10_NAME_PREFIX = "Polar H10"

    def __init__(self, data_pipe: Connection, control_pipe: Connection, mock_mode: bool = False):
        self.data_pipe = data_pipe
        self.control_pipe = control_pipe
        self.client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self.sequence_number = 0
        self.is_streaming = False
        self.should_exit = False
        self.mock_mode = mock_mode

    async def scan_and_connect(self, timeout: float = 10.0) -> bool:
        """Scan for Polar H10 and establish connection using the proven pattern."""
        if self.mock_mode:
            logger.info("[MOCK] Scanning for Polar H10...")
            await asyncio.sleep(1.0)
            logger.info("[MOCK] Found device: Polar H10 Mock (00:11:22:33:44:55)")
            logger.info("[MOCK] Connected to Polar H10 Mock")
            return True

        logger.info("Scanning for Polar H10...")

        # Step 1: Find device by name filter to get address
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name and d.name.startswith(self.POLAR_H10_NAME_PREFIX),
            timeout=timeout
        )

        if not device:
            logger.error("Polar H10 not found during scan.")
            return False

        logger.info(f"Found device: {device.name} ({device.address})")

        # Step 2: Use find_device_by_address to get a reliable BLEDevice object
        # This is the KEY pattern that works on Linux/BlueZ
        logger.info(f"Resolving device by address: {device.address}...")
        resolved_device = await BleakScanner.find_device_by_address(
            device.address, timeout=timeout
        )

        # Use the resolved BLEDevice object (not raw address string)
        target = resolved_device if resolved_device else device.address
        logger.info(f"Using target: {target} (type: {type(target).__name__})")

        try:
            self.client = BleakClient(
                target,
                disconnected_callback=self._on_disconnect,
                timeout=20.0
            )
            logger.info("Attempting connection...")
            await self.client.connect()
            logger.info(f"Connected: {self.client.is_connected}")

            if not self.client.is_connected:
                logger.error("Connection reported as not connected after connect()")
                return False

            self.device = resolved_device or device
            return True

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            import traceback
            logger.error(f"Connection traceback: {traceback.format_exc()}")
            return False

    def _on_disconnect(self, client: BleakClient):
        logger.warning("Disconnected from device.")
        self.is_streaming = False
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

    async def _generate_mock_data(self):
        """Generate mock HR data for testing without device."""
        logger.info("[MOCK] Starting mock HR data generation...")
        batch_count = 0
        base_hr = 70
        rr_base = 857.0  # ~70 BPM in ms

        while self.is_streaming and not self.should_exit:
            # Simulate HR with slight variation
            hr = base_hr + random.randint(-5, 5)
            # Simulate 1-2 RR intervals per notification (realistic for ~1Hz updates)
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

            await asyncio.sleep(1.0)  # HR notifications come ~1/sec

        logger.info(f"[MOCK] Mock data generation stopped. Total batches: {batch_count}")

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
                            if self.mock_mode:
                                await self.scan_and_connect()
                                await self.enable_hr_stream()
                                self.control_pipe.send({"status": "connected"})
                            elif not self.client or not self.client.is_connected:
                                try:
                                    if await self.scan_and_connect():
                                        await self.enable_hr_stream()
                                        self.control_pipe.send({"status": "connected"})
                                    else:
                                        self.control_pipe.send({"status": "disconnected"})
                                except Exception as e:
                                    logger.error(f"Connection/stream failed: {e}")
                                    import traceback
                                    logger.error(f"Traceback: {traceback.format_exc()}")
                                    self.control_pipe.send({"status": "disconnected"})
                        elif msg.command == "disconnect":
                            if self.mock_mode:
                                self.is_streaming = False
                                logger.info("[MOCK] Disconnected")
                                self.control_pipe.send({"status": "disconnected"})
                            elif self.client and self.client.is_connected:
                                try:
                                    await self.client.stop_notify(HR_MEASUREMENT_UUID)
                                except Exception:
                                    pass
                                await self.client.disconnect()
                                self.control_pipe.send({"status": "disconnected"})
                        elif msg.command == "exit":
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
                await self.client.stop_notify(HR_MEASUREMENT_UUID)
            except Exception:
                pass
            await self.client.disconnect()
        logger.info("BLE Process exiting.")


def ble_ingestion_main(data_pipe: Connection, control_pipe: Connection, mock_mode: bool = False):
    """Entry point for the BLE process."""
    manager = BleakManager(data_pipe, control_pipe, mock_mode=mock_mode)
    asyncio.run(manager.run())
