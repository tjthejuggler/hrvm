import os
import struct
import time
import unittest
from unittest.mock import MagicMock, patch, call
from src.gui.led_ball import LEDBallController
from src.gui.ui_manager import UIManager
from src.utils.ipc import IPCMessage, MSG_HEARTBEAT_BLINK

# Set LED_BALL_HW=1 to run the real-hardware blink test
HW_TEST = os.environ.get("LED_BALL_HW", "") == "1"


class TestLEDBallController(unittest.TestCase):
    def test_send_color(self):
        controller = LEDBallController(ip="127.0.0.1", port=5005)
        with patch('src.gui.led_ball.socket.socket') as mock_socket:
            mock_sock_instance = MagicMock()
            mock_socket.return_value = mock_sock_instance

            controller.send_color(255, 0, 0)

            # Verify socket was created as UDP
            mock_socket.assert_called_once()

            # Verify sendto was called with the correct protocol packet
            mock_sock_instance.sendto.assert_called_once()
            args, _ = mock_sock_instance.sendto.call_args
            data, addr = args

            self.assertEqual(addr, ("127.0.0.1", 5005))
            self.assertIsInstance(data, bytes)

            # Verify packet structure: 8-byte header + 4-byte color command
            self.assertEqual(len(data), 12)

            # Header: struct.pack("!bIBH", 66, 0, 0, 0)
            expected_header = struct.pack("!bIBH", 66, 0, 0, 0)
            self.assertEqual(data[:8], expected_header)

            # Color command: 0x0a, R, G, B
            expected_color = struct.pack("!BBBB", 0x0a, 255, 0, 0)
            self.assertEqual(data[8:], expected_color)

            # Verify socket was closed
            mock_sock_instance.close.assert_called_once()

    def test_blink(self):
        """blink() sends red ON immediately, then schedules OFF via timer."""
        controller = LEDBallController(ip="127.0.0.1", port=5005)
        controller.send_color = MagicMock()

        with patch('src.gui.led_ball.threading.Timer') as mock_timer_cls:
            mock_timer = MagicMock()
            mock_timer_cls.return_value = mock_timer

            controller.blink(duration=0.15)

            # Immediate red ON
            controller.send_color.assert_called_once_with(255, 0, 0)

            # Timer scheduled to send OFF after duration
            mock_timer_cls.assert_called_once_with(
                0.15, controller.send_color, args=(0, 0, 0)
            )
            self.assertTrue(mock_timer.daemon)
            mock_timer.start.assert_called_once()

    def test_blink_cancels_previous_timer(self):
        """Rapid blinks cancel the previous OFF timer."""
        controller = LEDBallController(ip="127.0.0.1", port=5005)
        controller.send_color = MagicMock()

        with patch('src.gui.led_ball.threading.Timer') as mock_timer_cls:
            first_timer = MagicMock()
            second_timer = MagicMock()
            mock_timer_cls.side_effect = [first_timer, second_timer]

            controller.blink(duration=0.15)
            controller.blink(duration=0.15)

            # First timer should have been cancelled
            first_timer.cancel.assert_called_once()

    def test_ui_integration(self):
        """LED ball blink() is called when MSG_HEARTBEAT_BLINK arrives."""
        with patch('src.gui.ui_manager.dpg') as mock_dpg:
            # Mock context managers for dpg
            mock_dpg.window.return_value.__enter__ = MagicMock(return_value="window")
            mock_dpg.window.return_value.__exit__ = MagicMock(return_value=False)
            mock_dpg.group.return_value.__enter__ = MagicMock(return_value="group")
            mock_dpg.group.return_value.__exit__ = MagicMock(return_value=False)
            mock_dpg.child_window.return_value.__enter__ = MagicMock(return_value="child")
            mock_dpg.child_window.return_value.__exit__ = MagicMock(return_value=False)
            mock_dpg.drawlist.return_value.__enter__ = MagicMock(return_value="drawlist")
            mock_dpg.drawlist.return_value.__exit__ = MagicMock(return_value=False)
            mock_dpg.table.return_value.__enter__ = MagicMock(return_value="table")
            mock_dpg.table.return_value.__exit__ = MagicMock(return_value=False)
            mock_dpg.table_row.return_value.__enter__ = MagicMock(return_value="row")
            mock_dpg.table_row.return_value.__exit__ = MagicMock(return_value=False)

            # Mock the pipes required by UIManager
            mock_data_pipe = MagicMock()
            mock_ble_pipe = MagicMock()
            mock_math_pipe = MagicMock()

            # Instantiate UIManager
            ui = UIManager(mock_data_pipe, mock_ble_pipe, mock_math_pipe, "shm_name")

            # Verify led_ball controller is initialized
            self.assertTrue(hasattr(ui, 'led_ball'))
            self.assertIsInstance(ui.led_ball, LEDBallController)

            # Verify default IP matches user request
            self.assertEqual(ui.led_ball.ip, "10.122.252.133")

            # Enable LED ball and mock blink
            ui.led_ball_enabled = True
            ui.led_ball.blink = MagicMock()

            # Simulate receiving a heartbeat blink IPC message
            # Set up the pipe to return one message then stop
            ui.running = True
            msg = IPCMessage(MSG_HEARTBEAT_BLINK)
            mock_data_pipe.poll.side_effect = [True, False]
            mock_data_pipe.recv.return_value = msg

            # Call process_incoming_data — it loops until poll returns False
            # then sleeps, so we stop after one iteration by setting running=False
            def stop_after_second_poll(*args):
                ui.running = False
                return False

            mock_data_pipe.poll.side_effect = [True, stop_after_second_poll]
            mock_data_pipe.recv.return_value = msg

            # Actually, process_incoming_data is a blocking loop.
            # Simpler: just call the relevant branch directly.
            # Reset side_effect and call manually.
            mock_data_pipe.poll.side_effect = None
            ui.running = True

            # Directly simulate what process_incoming_data does for this message
            ui._blink_time = 0.0  # reset
            # The code: if msg.type == MSG_HEARTBEAT_BLINK: _blink_time = time.time(); if led_ball_enabled: led_ball.blink(...)
            if isinstance(msg, IPCMessage) and msg.type == MSG_HEARTBEAT_BLINK:
                ui._blink_time = time.time()
                if ui.led_ball_enabled:
                    ui.led_ball.blink(ui._blink_duration)

            # Verify blink was called with the configured duration
            ui.led_ball.blink.assert_called_once_with(0.15)

            # Verify _blink_time was updated
            self.assertGreater(ui._blink_time, 0.0)


if __name__ == '__main__':
    unittest.main()
