"""TicWatch IMU manager — supports ADB (TCP) and WiFi (UDP) modes.

ADB mode (default):
    The custom Wear OS app connects via an ADB reverse tunnel.
    The host runs a TCP server; the watch is the TCP client.

    Setup (run once per watch per session in a terminal):
        adb -s <LEFT_WATCH_SERIAL>  reverse tcp:5555 tcp:5555
        adb -s <RIGHT_WATCH_SERIAL> reverse tcp:5556 tcp:5556

UDP mode:
    The watch sends UDP datagrams directly over WiFi to the host IP.
    No ADB setup required — both devices must be on the same network.

Ports:
    Left  watch → port 5555
    Right watch → port 5556

Each `SingleTicWatchManager` listens on its own port in the selected mode.
The manager re-listens automatically after disconnect (TCP) or continues
receiving datagrams (UDP), so the watch app can be restarted without
restarting the host app.
"""

import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

logger = logging.getLogger(__name__)

# Hardcoded ports — must match the watch app and adb reverse commands
PORT_LEFT  = 5555
PORT_RIGHT = 5556

_SENSOR_MAP = {"A": "acc", "G": "gyro", "M": "mag"}

# Connection mode constants
MODE_ADB = "adb"
MODE_UDP = "udp"


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class TicWatchSample:
    """One IMU sample from a TicWatch."""
    timestamp: float
    sensor: str          # "acc" | "gyro" | "mag"
    x: float
    y: float
    z: float


# ---------------------------------------------------------------------------
# Per-device manager — ADB (TCP) or UDP
# ---------------------------------------------------------------------------

class SingleTicWatchManager:
    """Server for one TicWatch device, supporting ADB (TCP) or UDP mode.

    In ADB mode:  listens for a TCP connection from the watch via ADB tunnel.
    In UDP mode:  listens for UDP datagrams sent directly over WiFi.

    Usage::

        mgr = SingleTicWatchManager("left", PORT_LEFT)
        mgr.set_mode(MODE_UDP)   # optional, default is MODE_ADB
        mgr.start()
        samples = mgr.poll()     # call each GUI frame
        mgr.stop()
    """

    def __init__(self, label: str, port: int):
        self._label = label   # "left" or "right" — for logging only
        self._port  = port
        self._mode  = MODE_ADB  # default

        self._buf: Deque[TicWatchSample] = deque(maxlen=2000)
        self._buf_lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._server_sock: Optional[socket.socket] = None

        self._status = "Stopped"
        self._status_lock = threading.Lock()
        self._last_packet: float = 0.0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> str:
        with self._status_lock:
            return self._status

    # ------------------------------------------------------------------
    # Mode selection
    # ------------------------------------------------------------------

    def set_mode(self, mode: str):
        """Set connection mode (MODE_ADB or MODE_UDP).

        Must be called before start(); has no effect while running.
        """
        if self._running:
            logger.warning(f"TicWatch [{self._label}]: cannot change mode while running")
            return
        if mode not in (MODE_ADB, MODE_UDP):
            raise ValueError(f"Unknown mode: {mode!r}. Use MODE_ADB or MODE_UDP.")
        self._mode = mode

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self):
        """Open the server socket and begin listening in the selected mode."""
        if self._running:
            return

        if self._mode == MODE_ADB:
            self._start_tcp()
        else:
            self._start_udp()

    def _start_tcp(self):
        """Bind a TCP server socket and start the ADB accept loop."""
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("0.0.0.0", self._port))
            self._server_sock.listen(1)
            self._server_sock.settimeout(1.0)
        except OSError as e:
            logger.error(f"TicWatch [{self._label}]: cannot bind TCP port {self._port}: {e}")
            self._set_status(f"Error: {e}")
            return

        self._running = True
        self._set_status("Waiting for watch… (ADB)")
        self._thread = threading.Thread(
            target=self._tcp_server_loop,
            daemon=True,
            name=f"TicWatch-{self._label}-tcp",
        )
        self._thread.start()
        logger.info(f"TicWatch [{self._label}] ADB/TCP server started on port {self._port}")

    def _start_udp(self):
        """Bind a UDP socket and start the datagram receive loop."""
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("0.0.0.0", self._port))
            self._server_sock.settimeout(1.0)
        except OSError as e:
            logger.error(f"TicWatch [{self._label}]: cannot bind UDP port {self._port}: {e}")
            self._set_status(f"Error: {e}")
            return

        self._running = True
        self._set_status("Waiting for watch… (UDP)")
        self._thread = threading.Thread(
            target=self._udp_recv_loop,
            daemon=True,
            name=f"TicWatch-{self._label}-udp",
        )
        self._thread.start()
        logger.info(f"TicWatch [{self._label}] WiFi/UDP server started on port {self._port}")

    def stop(self):
        """Stop the server and close all sockets."""
        if not self._running:
            return
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        self._set_status("Stopped")
        logger.info(f"TicWatch [{self._label}] stopped")

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def poll(self) -> List[TicWatchSample]:
        """Drain and return all buffered samples.  Call each GUI frame."""
        with self._buf_lock:
            samples = list(self._buf)
            self._buf.clear()
        if self._running and self._last_packet > 0:
            if time.time() - self._last_packet > 5.0:
                self._set_status("No data (timeout)")
        return samples

    # ------------------------------------------------------------------
    # Internal — TCP (ADB) loop
    # ------------------------------------------------------------------

    def _tcp_server_loop(self):
        """Accept TCP connections in a loop; re-listen after each disconnect."""
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed by stop()

            logger.info(f"TicWatch [{self._label}]: watch connected from {addr}")
            self._set_status("Connected — streaming (ADB)")
            self._tcp_recv_loop(conn)
            logger.info(f"TicWatch [{self._label}]: watch disconnected, re-listening")
            if self._running:
                self._set_status("Waiting for watch… (ADB)")

    def _tcp_recv_loop(self, conn: socket.socket):
        """Receive lines from a connected watch until it disconnects."""
        conn.settimeout(2.0)
        buf = b""
        try:
            while self._running:
                try:
                    chunk = conn.recv(1024)
                except socket.timeout:
                    if self._last_packet > 0 and time.time() - self._last_packet > 5.0:
                        self._set_status("No data (timeout)")
                    continue
                if not chunk:
                    break

                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    sample = _parse_line(line, self._label)
                    if sample is not None:
                        with self._buf_lock:
                            self._buf.append(sample)
                        self._last_packet = sample.timestamp
                        self._set_status(f"Streaming ({sample.sensor})")
        except Exception as e:
            logger.debug(f"TicWatch [{self._label}] TCP recv error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal — UDP (WiFi) loop
    # ------------------------------------------------------------------

    def _udp_recv_loop(self):
        """Receive UDP datagrams continuously until stopped."""
        buf = b""
        while self._running:
            try:
                data, addr = self._server_sock.recvfrom(1024)
            except socket.timeout:
                if self._last_packet > 0 and time.time() - self._last_packet > 5.0:
                    self._set_status("No data (timeout)")
                continue
            except OSError:
                break  # socket closed by stop()

            if self._status.startswith("Waiting"):
                logger.info(f"TicWatch [{self._label}]: first UDP packet from {addr}")
                self._set_status("Connected — streaming (UDP)")

            # UDP datagrams may contain multiple newline-separated lines
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                sample = _parse_line(line, self._label)
                if sample is not None:
                    with self._buf_lock:
                        self._buf.append(sample)
                    self._last_packet = sample.timestamp
                    self._set_status(f"Streaming ({sample.sensor})")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, msg: str):
        with self._status_lock:
            self._status = msg


# ---------------------------------------------------------------------------
# Packet parser
# ---------------------------------------------------------------------------

def _parse_line(line: bytes, source: str) -> Optional[TicWatchSample]:
    """Parse one newline-terminated payload line into a TicWatchSample."""
    try:
        text = line.decode("utf-8").strip()
        if not text:
            return None
        parts = text.split(",")
        if len(parts) != 4:
            return None
        sensor = _SENSOR_MAP.get(parts[0].upper())
        if sensor is None:
            return None
        return TicWatchSample(
            timestamp=time.time(),
            sensor=sensor,
            x=float(parts[1]),
            y=float(parts[2]),
            z=float(parts[3]),
        )
    except Exception as e:
        logger.debug(f"TicWatch [{source}]: bad line '{line}': {e}")
        return None
