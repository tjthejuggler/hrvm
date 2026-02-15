#!/usr/bin/env python3
"""Standalone diagnostic: connect to Polar H10 and test PMD ACC streaming.

Strategy: Register auto-accept agent, connect, pair with KeyboardDisplay
capability, then access PMD. Uses correct start command format from
bleakheart reference implementation.

Run: source venv/bin/activate && python3 tests/test_pmd_acc_diagnostic.py
"""
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.bluezdbus.manager import get_global_bluez_manager
from dbus_fast.service import ServiceInterface, method
from dbus_fast import Message, MessageType

POLAR_PREFIX = "Polar H10"
HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

AGENT_PATH = "/test/agent"

acc_packets_received = 0
ctrl_event = asyncio.Event()
ctrl_last_response = None


class AutoAcceptAgent(ServiceInterface):
    """BlueZ agent that auto-accepts all pairing with KeyboardDisplay capability."""

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):
        print("  [Agent] Released")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        print(f"  [Agent] Auto-confirming pairing for {device} passkey={passkey}")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        print(f"  [Agent] Auto-authorizing service {uuid} for {device}")

    @method()
    def RequestAuthorization(self, device: "o"):
        print(f"  [Agent] Auto-authorizing device {device}")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        print(f"  [Agent] RequestPinCode for {device}")
        return "0000"

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        print(f"  [Agent] RequestPasskey for {device}")
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        print(f"  [Agent] DisplayPasskey: {passkey}")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        print(f"  [Agent] DisplayPinCode: {pincode}")

    @method()
    def Cancel(self):
        print("  [Agent] Cancelled")


def on_pmd_control(sender, data: bytearray):
    global ctrl_last_response
    ctrl_last_response = data
    # Format: f0 <op> <type> <status> [<params...>]
    if len(data) >= 4:
        op = data[1]
        mtype = data[2]
        status = data[3]
        op_names = {0x01: "QUERY", 0x02: "START", 0x03: "STOP"}
        type_names = {0x00: "ECG", 0x01: "PPG", 0x02: "ACC", 0x03: "PPI", 0x05: "GYRO", 0x06: "MAG"}
        status_str = "SUCCESS" if status == 0 else f"ERROR({status})"
        print(f"  [PMD-CTRL] {op_names.get(op, f'0x{op:02x}')} "
              f"{type_names.get(mtype, f'0x{mtype:02x}')} "
              f"status={status_str}")
        if op == 0x01 and status == 0:
            # Parse settings - skip frame byte at data[4]
            parse_settings(data[5:])
    elif len(data) >= 3:
        print(f"  [PMD-CTRL] op=0x{data[1]:02x} raw={data.hex()}")
    else:
        print(f"  [PMD-CTRL] short: {data.hex()}")
    ctrl_event.set()


def parse_settings(settings: bytes):
    """Parse PMD settings response payload (after frame byte).

    Format per bleakheart/Polar SDK: type(1) + count(1) + values(count * 2 LE)
    """
    print(f"  [Settings] raw ({len(settings)} bytes): {settings.hex()}")
    i = 0
    while i < len(settings):
        stype = settings[i]
        i += 1
        if i >= len(settings):
            break
        count = settings[i]
        i += 1
        values = []
        for _ in range(count):
            if i + 1 < len(settings):
                val = int.from_bytes(settings[i:i + 2], 'little')
                values.append(val)
                i += 2
            else:
                break
        names = {0: "SAMPLE_RATE", 1: "RESOLUTION", 2: "RANGE", 4: "CHANNELS"}
        print(f"  [Settings] {names.get(stype, f'TYPE_{stype}')}: {values}")


def on_pmd_data(sender, data: bytearray):
    global acc_packets_received
    acc_packets_received += 1
    if len(data) < 10:
        print(f"  [PMD-DATA] #{acc_packets_received} too short ({len(data)} bytes): {data.hex()}")
        return
    # Byte 0: measurement type
    mtype = data[0]
    # Bytes 1-8: timestamp (uint64 LE, nanoseconds)
    timestamp = int.from_bytes(data[1:9], 'little')
    # Byte 9: frame type
    frame_type = data[9]

    n_samples = (len(data) - 10) // 6
    if acc_packets_received <= 5 or acc_packets_received % 25 == 0:
        samples_str = ""
        if n_samples > 0:
            x = struct.unpack_from('<h', data, 10)[0]
            y = struct.unpack_from('<h', data, 12)[0]
            z = struct.unpack_from('<h', data, 14)[0]
            samples_str = f" first=({x},{y},{z})"
        print(f"  [PMD-DATA] #{acc_packets_received} type={mtype} frame={frame_type} "
              f"samples={n_samples}{samples_str}")


def on_hr(sender, data: bytearray):
    hr = data[1] if not (data[0] & 0x01) else int.from_bytes(data[1:3], 'little')
    print(f"  [HR] {hr} bpm")


async def wait_ctrl_response(timeout=5.0):
    """Wait for a PMD control response."""
    ctrl_event.clear()
    try:
        await asyncio.wait_for(ctrl_event.wait(), timeout)
        return ctrl_last_response
    except asyncio.TimeoutError:
        print("  [WARN] No PMD control response within timeout")
        return None


async def register_agent_on_bus(bus, capability="KeyboardDisplay"):
    """Register auto-accept agent on D-Bus."""
    agent = AutoAcceptAgent()
    bus.export(AGENT_PATH, agent)

    reply = await bus.call(Message(
        destination="org.bluez", path="/org/bluez",
        interface="org.bluez.AgentManager1", member="RegisterAgent",
        signature="os", body=[AGENT_PATH, capability],
    ))
    if reply.message_type == MessageType.ERROR:
        if "AlreadyExists" in str(reply.body):
            print("  Agent already registered, unregistering first...")
            await bus.call(Message(
                destination="org.bluez", path="/org/bluez",
                interface="org.bluez.AgentManager1", member="UnregisterAgent",
                signature="o", body=[AGENT_PATH],
            ))
            reply = await bus.call(Message(
                destination="org.bluez", path="/org/bluez",
                interface="org.bluez.AgentManager1", member="RegisterAgent",
                signature="os", body=[AGENT_PATH, capability],
            ))
            if reply.message_type == MessageType.ERROR:
                print(f"  RegisterAgent failed: {reply.body}")
                return False
        else:
            print(f"  RegisterAgent failed: {reply.body}")
            return False

    reply = await bus.call(Message(
        destination="org.bluez", path="/org/bluez",
        interface="org.bluez.AgentManager1", member="RequestDefaultAgent",
        signature="o", body=[AGENT_PATH],
    ))
    if reply.message_type == MessageType.ERROR:
        print(f"  RequestDefaultAgent failed: {reply.body}")
        return False

    return True


def build_acc_start_command(sample_rate=200, resolution=16, range_g=2):
    """Build PMD ACC start command using bleakheart-compatible format.

    Format: 0x02 <meas_type> [<setting_idx> <array_len=0x01> <value_le16>]...

    The key difference from our old command: each setting needs an array
    length byte (0x01) between the setting type index and the value.
    """
    cmd = bytearray([0x02, 0x02])  # START, ACC
    # Setting 0: SAMPLE_RATE
    cmd.extend([0x00, 0x01])
    cmd.extend(sample_rate.to_bytes(2, 'little'))
    # Setting 1: RESOLUTION
    cmd.extend([0x01, 0x01])
    cmd.extend(resolution.to_bytes(2, 'little'))
    # Setting 2: RANGE
    cmd.extend([0x02, 0x01])
    cmd.extend(range_g.to_bytes(2, 'little'))
    return cmd


async def main():
    print("=" * 60)
    print("Polar H10 PMD ACC Diagnostic v6")
    print("=" * 60)

    # Step 1: Init bleak + register agent
    print("\n--- Step 1: Register D-Bus agent (KeyboardDisplay) ---")
    manager = await get_global_bluez_manager()
    bus = manager._bus
    agent_ok = await register_agent_on_bus(bus, "KeyboardDisplay")
    print(f"Agent registered: {agent_ok}")

    # Step 2: Scan
    print("\n--- Step 2: Scanning ---")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name.startswith(POLAR_PREFIX),
        timeout=10.0
    )
    if not device:
        print("ERROR: Polar H10 not found!")
        sys.exit(1)
    print(f"Found: {device.name} ({device.address})")

    # Step 3: Connect
    print("\n--- Step 3: Connecting ---")
    client = BleakClient(device, timeout=30.0)
    try:
        await client.connect()
        print(f"Connected: {client.is_connected}")
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    try:
        # Step 4: Pair
        print("\n--- Step 4: Pairing ---")
        try:
            await client.pair()
            print("Pairing succeeded!")
        except Exception as e:
            print(f"Pairing error: {e}")
            if not client.is_connected:
                print("Reconnecting after failed pair...")
                await asyncio.sleep(2)
                try:
                    await client.connect()
                    print(f"Reconnected: {client.is_connected}")
                    try:
                        await client.pair()
                        print("Second pair attempt succeeded!")
                    except Exception as e2:
                        print(f"Second pair attempt: {e2}")
                except Exception as e3:
                    print(f"Reconnect failed: {e3}")
                    return

        print(f"Connected after pair: {client.is_connected}")

        # Step 5: HR test
        print("\n--- Step 5: HR subscription ---")
        try:
            await client.start_notify(HR_UUID, on_hr)
            print("HR OK")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"HR failed: {e}")

        # Step 6: PMD Control
        print("\n--- Step 6: PMD Control subscription ---")
        try:
            await client.start_notify(PMD_CONTROL, on_pmd_control)
            print("PMD Control OK!")
        except Exception as e:
            print(f"PMD Control FAILED: {e}")
            print("\n*** This confirms PMD requires successful pairing ***")
            return

        # Step 7: PMD Data
        print("\n--- Step 7: PMD Data subscription ---")
        try:
            await client.start_notify(PMD_DATA, on_pmd_data)
            print("PMD Data OK!")
        except Exception as e:
            print(f"PMD Data FAILED: {e}")
            return

        await asyncio.sleep(0.5)

        # Step 8: Query ACC settings (and WAIT for response)
        print("\n--- Step 8: Query ACC settings ---")
        await client.write_gatt_char(PMD_CONTROL, bytearray([0x01, 0x02]), response=True)
        resp = await wait_ctrl_response(timeout=5.0)
        if resp and len(resp) >= 4 and resp[3] != 0:
            print(f"  Query returned error status {resp[3]}, aborting")
            return
        print("  Query OK, proceeding to start")

        # Step 9: Start ACC stream with CORRECT command format
        await asyncio.sleep(0.5)
        print("\n--- Step 9: Start ACC stream ---")
        # Use bleakheart-compatible format: type + array_len(0x01) + value(2 LE)
        # Default settings: rate=200Hz, resolution=16bit, range=2G
        start_cmd = build_acc_start_command(sample_rate=200, resolution=16, range_g=2)
        print(f"  Sending start command: {start_cmd.hex()}")
        print(f"  (format: START=0x02, ACC=0x02, "
              f"RATE=[0x00,0x01,{200:#06x}], "
              f"RES=[0x01,0x01,{16:#06x}], "
              f"RANGE=[0x02,0x01,{2:#06x}])")
        try:
            await client.write_gatt_char(PMD_CONTROL, start_cmd, response=True)
            print("  Write succeeded!")
        except Exception as e:
            print(f"  Write failed: {e}")
            # Fallback: try with response=False
            print("  Retrying with response=False...")
            try:
                await client.write_gatt_char(PMD_CONTROL, start_cmd, response=False)
                print("  Write (no-response) succeeded!")
            except Exception as e2:
                print(f"  Write (no-response) also failed: {e2}")
                return

        resp = await wait_ctrl_response(timeout=5.0)

        # Step 10: Wait for data
        print(f"\n--- Step 10: Waiting 10s for ACC data ---")
        for i in range(10):
            await asyncio.sleep(1)
            print(f"  t={i + 1}s: {acc_packets_received} packets")

        print(f"\n{'=' * 60}")
        print(f"RESULT: {acc_packets_received} ACC packets received")
        if acc_packets_received > 0:
            print("SUCCESS! ACC streaming works!")
        else:
            print("FAILURE: No ACC data received")
        print(f"{'=' * 60}")

    finally:
        print("\n--- Cleanup ---")
        # Stop ACC stream
        try:
            stop_cmd = bytearray([0x03, 0x02])  # STOP ACC
            await client.write_gatt_char(PMD_CONTROL, stop_cmd, response=True)
            print("ACC stream stopped")
        except Exception:
            pass
        try:
            await client.unpair()
            print("Unpaired")
        except Exception as e:
            print(f"Unpair: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        # Also remove from bluetoothctl to be safe
        import subprocess
        subprocess.run(["bluetoothctl", "remove", device.address],
                       capture_output=True, timeout=5)
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
