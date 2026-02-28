"""ZeroMQ PUB socket publisher for sensor data streams.

Publishes HR, ACC, and ECG batches as JSON messages on a ZeroMQ PUB socket.
External programs subscribe to specific topics to receive live sensor data.

Topics published:
  "hr"  — Heart rate + RR intervals (from Polar H10 HR characteristic)
  "acc" — Accelerometer samples     (from Polar H10 / PVS PMD service)
  "ecg" — ECG samples               (from Polar H10 PMD service)

Default endpoint: tcp://127.0.0.1:5555

Usage (this module):
    publisher = StreamPublisher()
    publisher.start()
    publisher.publish_hr(batch)
    publisher.publish_acc(batch)
    publisher.publish_ecg(batch)
    publisher.stop()
"""

import json
import logging
import time
from typing import Optional

import zmq

from src.utils.ipc import HRBatch, ACCBatch, ECGBatch

logger = logging.getLogger(__name__)

# Default ZeroMQ endpoint — change here or pass to constructor
DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"


class StreamPublisher:
    """Publishes sensor batches over a ZeroMQ PUB socket.

    Thread-safe: ZeroMQ sockets are not thread-safe, but this class is only
    ever called from the BLE process's asyncio callbacks (single thread).
    If you need to call from multiple threads, add a lock around publish calls.
    """

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT):
        self.endpoint = endpoint
        self._ctx: Optional[zmq.Context] = None
        self._socket: Optional[zmq.Socket] = None
        self._active = False

    def start(self) -> None:
        """Open the ZeroMQ context and bind the PUB socket."""
        try:
            self._ctx = zmq.Context()
            self._socket = self._ctx.socket(zmq.PUB)
            self._socket.bind(self.endpoint)
            self._active = True
            logger.info(f"StreamPublisher bound to {self.endpoint}")
        except zmq.ZMQError as e:
            logger.error(f"StreamPublisher failed to bind {self.endpoint}: {e}")
            self._active = False

    def stop(self) -> None:
        """Close the socket and terminate the ZeroMQ context."""
        self._active = False
        if self._socket:
            try:
                self._socket.close(linger=0)
            except Exception:
                pass
            self._socket = None
        if self._ctx:
            try:
                self._ctx.term()
            except Exception:
                pass
            self._ctx = None
        logger.info("StreamPublisher stopped.")

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _send(self, topic: str, payload: dict) -> None:
        """Serialize payload to JSON and send as a multipart ZMQ message."""
        if not self._active or self._socket is None:
            return
        try:
            self._socket.send_multipart([
                topic.encode(),
                json.dumps(payload).encode(),
            ], flags=zmq.NOBLOCK)
        except zmq.Again:
            # No subscribers — drop silently (PUB/SUB is fire-and-forget)
            pass
        except Exception as e:
            logger.warning(f"StreamPublisher send error on topic '{topic}': {e}")

    # ------------------------------------------------------------------
    # Public publish methods — called from BLE notification handlers
    # ------------------------------------------------------------------

    def publish_hr(self, batch: HRBatch) -> None:
        """Publish an HR batch on the 'hr' topic."""
        self._send("hr", {
            "timestamp": batch.timestamp_unix,
            "heart_rate_bpm": batch.heart_rate_bpm,
            "rr_intervals_ms": batch.rr_intervals_ms,
            "sequence_number": batch.sequence_number,
        })

    def publish_acc(self, batch: ACCBatch) -> None:
        """Publish an ACC batch on the 'acc' topic."""
        self._send("acc", {
            "timestamp": batch.timestamp_unix,
            "sample_rate": batch.sample_rate,
            "samples": batch.samples,   # list of [x, y, z] in mg
            "sequence_number": batch.sequence_number,
        })

    def publish_ecg(self, batch: ECGBatch) -> None:
        """Publish an ECG batch on the 'ecg' topic."""
        self._send("ecg", {
            "timestamp": batch.timestamp_unix,
            "sample_rate": batch.sample_rate,
            "samples": batch.samples.tolist(),  # numpy -> plain list
            "sequence_number": batch.sequence_number,
        })
