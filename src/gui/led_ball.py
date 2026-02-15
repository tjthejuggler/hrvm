import socket
import struct
import logging
import threading

logger = logging.getLogger(__name__)


class LEDBallController:
    """Controller for external LED ball via UDP.

    Uses the ball's native protocol: a fixed 8-byte UDP header followed by
    a 4-byte color command (0x0a, R, G, B).
    """

    def __init__(self, ip="10.122.252.133", port=41412):
        self.ip = ip
        self.port = port
        self._off_timer = None

    def send_color(self, r, g, b):
        """Send an RGB color command to the LED ball.

        Args:
            r: Red value 0-255
            g: Green value 0-255
            b: Blue value 0-255
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # UDP header from the ball protocol
                udp_header = struct.pack("!bIBH", 66, 0, 0, 0)
                # Color command: 0x0a followed by RGB values
                color_data = struct.pack("!BBBB", 0x0a, r, g, b)
                full_command = udp_header + color_data
                s.sendto(full_command, (self.ip, self.port))
            finally:
                s.close()
        except Exception as e:
            logger.error(f"Failed to send LED color: {e}")

    def blink(self, duration=0.15):
        """Flash the ball red then turn it off after *duration* seconds.

        Sends full red immediately, then schedules an OFF command.
        Safe to call rapidly — any pending OFF timer is cancelled first.
        """
        # Cancel any pending off-timer so rapid blinks stay lit
        if self._off_timer is not None:
            self._off_timer.cancel()

        # Full red ON
        self.send_color(255, 0, 0)

        # Schedule OFF
        self._off_timer = threading.Timer(duration, self.send_color, args=(0, 0, 0))
        self._off_timer.daemon = True
        self._off_timer.start()

    def set_ip(self, ip):
        self.ip = ip
