#!/usr/bin/env python3
"""ACC streaming test with proper MTU negotiation and D-Bus StartNotify.

Root cause analysis:
- MTU=23 (default) is too small for PMD data frames (~200+ bytes)
- Must call _acquire_mtu() after connect to negotiate MTU=232
- AcquireNotify socket crashes bluetoothd on high-freq PMD streams
- Must use use_start_notify=True for safe D-Bus signal routing
- Must NOT manually write CCCD (BlueZ manages this internally)
- BlueZ may have up to 30s propagation delay before data flows
"""
import asyncio
import struct
from bleak import BleakClient, BleakScanner

POLAR_PREFIX = "Polar H10"
PMD_CTRL = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"

data_count = 0
ctrl_count = 0


def on_pmd_data(sender, data: bytearray):
    """PMD Data notification handler."""
    global data_count
    data_count += 1
    meas_type = data[0] if len(data) > 0 else -1
    if data_count <= 5 or data_count % 20 == 0:
        print(f"  DATA #{data_count}: len={len(data)}, type={meas_type}, "
              f"first_bytes={data[:16].hex()}")


def on_pmd_ctrl(sender, data: bytearray):
    """PMD Control indication handler."""
    global ctrl_count
    ctrl_count += 1
    print(f"  CTRL #{ctrl_count}: len={len(data)}, raw={data.hex()}")


def build_acc_start(sample_rate=25, resolution=16, range_g=8):
    """Build ACC START command with correct Polar PMD format."""
    cmd = bytearray([0x02, 0x02])  # START, ACC
    # Sample rate (type=0x00, array_len=0x01, value LE16)
    cmd.append(0x00)
    cmd.append(0x01)
    cmd.extend(struct.pack('<H', sample_rate))
    # Resolution (type=0x01, array_len=0x01, value LE16)
    cmd.append(0x01)
    cmd.append(0x01)
    cmd.extend(struct.pack('<H', resolution))
    # Range (type=0x02, array_len=0x01, value LE16)
    cmd.append(0x02)
    cmd.append(0x01)
    cmd.extend(struct.pack('<H', range_g))
    return cmd


async def main():
    print("=" * 60)
    print("ACC STREAMING TEST - MTU + Default AcquireNotify")
    print("=" * 60)

    # Step 1: Scan
    print("\n--- Step 1: Scanning ---")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name.startswith(POLAR_PREFIX),
        timeout=10.0
    )
    if not device:
        print("Polar H10 not found!")
        return
    print(f"Found: {device.name} ({device.address})")

    # Step 2: Connect
    print("\n--- Step 2: Connecting ---")
    async with BleakClient(device, timeout=30.0) as client:
        print(f"Connected: {client.is_connected}")

        # Step 3: Acquire MTU (CRITICAL - must be done before subscribing)
        print("\n--- Step 3: Acquire MTU ---")
        mtu_before = client.mtu_size
        print(f"MTU before _acquire_mtu(): {mtu_before}")
        
        # Access the backend to call _acquire_mtu()
        await client._backend._acquire_mtu()
        
        mtu_after = client.mtu_size
        print(f"MTU after _acquire_mtu(): {mtu_after}")
        
        if mtu_after < 100:
            print("WARNING: MTU still too small for PMD data!")

        # Step 4: Pair
        print("\n--- Step 4: Pairing ---")
        try:
            await client.pair()
            print("Paired successfully")
        except Exception as e:
            print(f"Pair result: {e}")

        # Step 5: Subscribe to PMD Control (indications)
        print("\n--- Step 5: Subscribe PMD Control ---")
        await client.start_notify(PMD_CTRL, on_pmd_ctrl)
        print("Subscribed to PMD Control")

        # Step 6: Subscribe to PMD Data - Force D-Bus signals (StartNotify)
        # AcquireNotify socket crashes bluetoothd on high-freq PMD streams
        print("\n--- Step 6: Subscribe PMD Data (use_start_notify=True) ---")
        await client.start_notify(
            PMD_DATA, on_pmd_data,
            bluez={"use_start_notify": True}
        )
        print("Subscribed to PMD Data (D-Bus StartNotify)")

        # Step 7: Send ACC START
        print("\n--- Step 7: Send ACC START ---")
        start_cmd = build_acc_start(sample_rate=25, resolution=16, range_g=8)
        print(f"START cmd: {start_cmd.hex()}")
        await client.write_gatt_char(PMD_CTRL, start_cmd, response=True)
        print("START command sent")

        # Step 8: Collect data (up to 45s - BlueZ may have 30s propagation delay)
        print("\n--- Step 8: Collecting data (45 seconds max) ---")
        for i in range(45):
            await asyncio.sleep(1)
            print(f"  t={i+1}s: data={data_count}, ctrl={ctrl_count}")
            if data_count > 0 and i >= 9:
                # Data is flowing, collect a few more seconds then break
                break

        # Step 9: Stop
        print("\n--- Step 9: Stop ACC ---")
        stop_cmd = bytearray([0x03, 0x02])
        try:
            await client.write_gatt_char(PMD_CTRL, stop_cmd, response=True)
            print("STOP sent")
        except Exception as e:
            print(f"STOP: {e}")

        await asyncio.sleep(1)

        # Step 10: Cleanup
        print("\n--- Step 10: Cleanup ---")
        try:
            await client.stop_notify(PMD_DATA)
        except Exception as e:
            print(f"Stop notify data: {e}")
        try:
            await client.stop_notify(PMD_CTRL)
        except Exception as e:
            print(f"Stop notify ctrl: {e}")

    print("\n" + "=" * 60)
    print(f"RESULT: {data_count} data packets, {ctrl_count} ctrl packets")
    print(f"MTU was: {mtu_before} -> {mtu_after}")
    if data_count > 0:
        print("SUCCESS - PMD data is flowing!")
    else:
        print("FAILURE - No PMD data received")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
