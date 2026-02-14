import asyncio
import logging
import struct
import time
import random
from typing import Optional, Tuple, Dict, Any
from multiprocessing.connection import Connection
import numpy as np
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from src.ble.ring_buffer import RingBuffer
from src.utils.ipc import ECGBatch, BLECommand

# Configure logging
logger = logging.getLogger("ble_process")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

class BleakManager:
    """
    Manages BLE connection to Polar H10 and streams ECG data.
    Runs in dedicated process with asyncio event loop.
    """
    
    # Class Constants
    POLAR_H10_NAME_PREFIX = "Polar H10"
    PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
    PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
    PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
    
    # Command to enable ECG stream:
    ECG_ENABLE_CMD = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
    
    POLAR_EPOCH_OFFSET = 946684800  # 2000-01-01 00:00:00 UTC
    
    def __init__(self, data_pipe: Connection, control_pipe: Connection, mock_mode: bool = False):
        self.data_pipe = data_pipe
        self.control_pipe = control_pipe
        self.client: Optional[BleakClient] = None
        self.ring_buffer = RingBuffer(capacity=130 * 2)  # 2 seconds buffer
        self.sequence_number = 0
        self.is_streaming = False
        self.should_exit = False
        self.mock_mode = mock_mode
        
    async def scan_and_connect(self, timeout: float = 10.0) -> bool:
        """Scan for Polar H10 and establish connection."""
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
            logger.error("Polar H10 not found.")
            return False
            
        logger.info(f"Found device: {device.name} ({device.address})")
        
        self.client = BleakClient(device, disconnected_callback=self._on_disconnect)
        
        try:
            await self.client.connect()
            logger.info(f"Connected to {device.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            return False

    def _on_disconnect(self, client: BleakClient):
        logger.warning("Disconnected from device.")
        self.is_streaming = False
        # In a real scenario, we might trigger a reconnect loop here or notify main process

    async def enable_ecg_stream(self) -> None:
        """Send enable command to PMD Control characteristic."""
        if self.mock_mode:
            logger.info("[MOCK] Subscribing to PMD Data characteristic...")
            logger.info("[MOCK] Sending ECG enable command...")
            self.is_streaming = True
            logger.info("[MOCK] ECG stream enabled.")
            # Start mock data generation task
            asyncio.create_task(self._generate_mock_data())
            return

        if not self.client or not self.client.is_connected:
            logger.error("Cannot enable stream: Not connected.")
            return

        logger.info("Subscribing to PMD Data characteristic...")
        await self.client.start_notify(self.PMD_DATA_UUID, self.notification_handler)
        
        logger.info("Sending ECG enable command...")
        await self.client.write_gatt_char(self.PMD_CONTROL_UUID, self.ECG_ENABLE_CMD)
        self.is_streaming = True
        logger.info("ECG stream enabled.")

    async def _generate_mock_data(self):
        """Generate mock ECG data for testing without device."""
        logger.info("[MOCK] Starting mock data generation...")
        sample_rate = 130
        samples_per_packet = 10
        interval = samples_per_packet / sample_rate
        
        t = 0.0
        batch_count = 0
        while self.is_streaming and not self.should_exit:
            # Generate synthetic ECG signal (sine wave + noise)
            # 1 Hz sine wave (60 BPM)
            samples = []
            for _ in range(samples_per_packet):
                val = int(1000 * np.sin(2 * np.pi * 1.0 * t) + random.gauss(0, 50))
                samples.append(val)
                t += 1.0 / sample_rate
                
            # Create batch
            batch = ECGBatch(
                timestamp_unix=time.time(),
                sample_rate=sample_rate,
                samples=np.array(samples, dtype=np.int32),
                sequence_number=self.sequence_number
            )
            self.sequence_number += 1
            batch_count += 1
            
            try:
                self.data_pipe.send(batch)
                if batch_count % 13 == 0:  # Log every second (13 batches/sec)
                    logger.info(f"[MOCK] Sent batch #{batch_count} (seq={self.sequence_number-1})")
            except Exception as e:
                logger.error(f"[MOCK] Failed to send mock batch: {e}")
                break
                
            await asyncio.sleep(interval)
        
        logger.info(f"[MOCK] Mock data generation stopped. Total batches sent: {batch_count}")

    def notification_handler(self, sender: int, data: bytearray) -> None:
        """
        Parse incoming ECG data packets.
        Format: [Type(1)][Timestamp(8)][FrameType(1)][Samples(3*N)]
        """
        # Basic validation
        if len(data) < 10:
            logger.warning(f"Received short packet: {len(data)} bytes")
            return
            
        # Parse header
        # packet_type = data[0] # Should be 0x00 for ECG data
        timestamp_polar = struct.unpack_from('<Q', data, 1)[0]
        # frame_type = data[9] 
        
        # Convert timestamp to Unix epoch
        # Polar timestamp is in nanoseconds since 2000-01-01
        timestamp_unix = self.POLAR_EPOCH_OFFSET + (timestamp_polar / 1e9)
        
        # Parse samples
        # Samples start at index 10
        # Each sample is 3 bytes (24-bit signed integer), little-endian
        samples_data = data[10:]
        n_samples = len(samples_data) // 3
        
        samples = np.zeros(n_samples, dtype=np.int32)
        
        for i in range(n_samples):
            offset = i * 3
            # Read 3 bytes
            b0 = samples_data[offset]
            b1 = samples_data[offset+1]
            b2 = samples_data[offset+2]
            
            # Combine into 32-bit integer (sign extension handled manually if needed, 
            # but here we can just interpret as 24-bit signed)
            val = (b2 << 16) | (b1 << 8) | b0
            
            # Handle sign extension for 24-bit integer
            if val & 0x800000:
                val -= 0x1000000
                
            samples[i] = val
            
        batch = ECGBatch(
            timestamp_unix=timestamp_unix,
            sample_rate=130,
            samples=samples,
            sequence_number=self.sequence_number
        )
        self.sequence_number += 1
        
        try:
            self.data_pipe.send(batch)
        except Exception as e:
            logger.error(f"Failed to send batch to pipe: {e}")

    async def handle_control_messages(self) -> None:
        """Process commands from GUI (connect/disconnect/battery)."""
        try:
            # Use a loop to process all available messages, but break on error
            while True:
                try:
                    if not self.control_pipe.poll(0): # Non-blocking check
                        break
                        
                    msg = self.control_pipe.recv()
                    if isinstance(msg, BLECommand):
                        logger.info(f"Received command: {msg.command}")
                        if msg.command == "connect":
                            if self.mock_mode:
                                await self.scan_and_connect()
                                await self.enable_ecg_stream()
                                self.control_pipe.send({"status": "connected"})
                            elif not self.client or not self.client.is_connected:
                                if await self.scan_and_connect():
                                    await self.enable_ecg_stream()
                                    self.control_pipe.send({"status": "connected"})
                                else:
                                    self.control_pipe.send({"status": "disconnected"})
                        elif msg.command == "disconnect":
                            if self.mock_mode:
                                self.is_streaming = False
                                logger.info("[MOCK] Disconnected")
                                self.control_pipe.send({"status": "disconnected"})
                            elif self.client and self.client.is_connected:
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
                    # If we get an error reading, we should probably stop trying to read for this cycle
                    break
        except Exception as e:
             logger.error(f"Critical error in control message loop: {e}")
             self.should_exit = True

    async def run(self) -> None:
        """Main event loop for BLE process."""
        logger.info(f"BLE Process started. Mock Mode: {self.mock_mode}")
        logger.info("[DEBUG] Waiting for 'connect' command from GUI...")
        
        # Initial connection attempt
        # In mock mode, we might want to wait for explicit connect command,
        # or just start if that's the desired behavior.
        # The architecture says "Starts on device connection", but usually we wait for UI.
        # Let's wait for UI command to connect, unless we want auto-connect.
        # For now, let's NOT auto-connect on startup, but wait for UI.
        # EXCEPT if we want to test quickly.
        # Let's stick to: Wait for UI command.
        
        while not self.should_exit:
            await self.handle_control_messages()
            await asyncio.sleep(0.1)
            
        if not self.mock_mode and self.client and self.client.is_connected:
            await self.client.disconnect()
        logger.info("BLE Process exiting.")

def ble_ingestion_main(data_pipe: Connection, control_pipe: Connection, mock_mode: bool = False):
    """Entry point for the BLE process."""
    manager = BleakManager(data_pipe, control_pipe, mock_mode=mock_mode)
    asyncio.run(manager.run())
