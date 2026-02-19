#!/usr/bin/env python3
"""
Polar Verity Sense - The "Get Everything" Script
------------------------------------------------
1. Enables SDK Mode (High Frequency Raw Data).
2. Streams ACC, GYR, MAG (IMU Data).
3. Streams PPG (Raw Optical Data) -> You must calculate HR from this!
4. PARSES the raw bytes to show real integer values.
"""
import asyncio
import struct
import sys
import signal

from bleak import BleakClient, BleakScanner

# --- Configuration ---
PVS_PREFIX = "Polar Sense"
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA    = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

# Global control
stop_event = asyncio.Event()
ctrl_event = asyncio.Event()
ctrl_last_response = None

def signal_handler(sig, frame):
    print("\n[!] Ctrl+C pressed. Stopping...")
    stop_event.set()

def on_pmd_control(sender, data: bytearray):
    global ctrl_last_response
    ctrl_last_response = data
    if len(data) >= 4:
        # data[3] is status (0x00=Success, 0x06=Already Active)
        print(f"  [CTRL] Op:0x{data[1]:02x} Type:0x{data[2]:02x} Status:0x{data[3]:02x}")
    ctrl_event.set()

def parse_24bit_le(data, offset):
    """Helper to read 3-byte Little Endian integer (used in PPG)."""
    return data[offset] | (data[offset+1] << 8) | (data[offset+2] << 16)

def on_pmd_data(sender, data: bytearray):
    """
    Parses incoming raw data packets.
    For demonstration, we decode the 'Reference Sample' (the first sample)
    of each Delta Frame to show real values.
    """
    if len(data) < 10: return
    
    mtype = data[0]
    frame_byte = data[9]
    is_delta = (frame_byte & 0x80) == 0x80
    
    # Delta Frames (0x80) start with a full-resolution Reference Sample at index 10
    # We will parse just this first sample to prove we have values.
    # (A full production driver would iterate the following bits for the rest)
    
    # --- ACCELEROMETER (Type 2) ---
    # Format: 3 channels (X,Y,Z), 16-bit signed
    if mtype == 0x02 and is_delta:
        # 3 * 2 bytes = 6 bytes total
        x, y, z = struct.unpack_from('<hhh', data, 10)
        print(f"  [ACC] X:{x:+5d} Y:{y:+5d} Z:{z:+5d}  (len={len(data)})")

    # --- GYROSCOPE (Type 5) ---
    # Format: 3 channels (X,Y,Z), 16-bit signed
    elif mtype == 0x05 and is_delta:
        x, y, z = struct.unpack_from('<hhh', data, 10)
        print(f"  [GYR] X:{x:+5d} Y:{y:+5d} Z:{z:+5d}  (len={len(data)})")

    # --- MAGNETOMETER (Type 6) ---
    # Format: 3 channels (X,Y,Z), 16-bit signed
    elif mtype == 0x06 and is_delta:
        x, y, z = struct.unpack_from('<hhh', data, 10)
        print(f"  [MAG] X:{x:+5d} Y:{y:+5d} Z:{z:+5d}  (len={len(data)})")

    # --- PPG / RAW OPTICAL (Type 21 / 0x15) ---
    # This is the Raw Heart Rate Data.
    # Format: 4 channels (PPG0, PPG1, PPG2, Ambient), usually 22-bit (stored as 3 bytes)
    elif mtype == 0x15 and is_delta:
        # 4 channels * 3 bytes = 12 bytes total
        # We manually unpack 24-bit integers
        ppg0 = parse_24bit_le(data, 10)
        ppg1 = parse_24bit_le(data, 13)
        ppg2 = parse_24bit_le(data, 16)
        amb  = parse_24bit_le(data, 19)
        print(f"  [PPG] L1:{ppg0:06d} L2:{ppg1:06d} L3:{ppg2:06d} Amb:{amb:06d}")

async def send_cmd(client, cmd, label):
    """Send command and wait for success."""
    global ctrl_last_response
    ctrl_event.clear()
    ctrl_last_response = None
    print(f"\n  Sending {label}...")
    await client.write_gatt_char(PMD_CONTROL, cmd, response=True)
    try:
        await asyncio.wait_for(ctrl_event.wait(), timeout=3.0)
        if ctrl_last_response and len(ctrl_last_response) >= 4:
            if ctrl_last_response[3] in [0x00, 0x06]:
                print(f"  -> {label} OK")
                return True
            else:
                print(f"  -> {label} FAILED Status:0x{ctrl_last_response[3]:02x}")
    except asyncio.TimeoutError:
        print(f"  -> {label} TIMEOUT")
    return False

def build_cmd(mtype, rate, res, range_val, channels):
    """Generic builder for the Polar PMD start command."""
    cmd = bytearray([0x02, mtype])
    # Rate (2 bytes)
    cmd.extend([0x00, 0x01])
    cmd.extend(struct.pack("<H", rate))
    # Resolution (2 bytes)
    cmd.extend([0x01, 0x01])
    cmd.extend(struct.pack("<H", res))
    # Range (2 bytes)
    cmd.extend([0x02, 0x01])
    cmd.extend(struct.pack("<H", range_val))
    # Channels (1 byte!)
    cmd.extend([0x04, 0x01])
    cmd.extend([channels])
    return cmd

async def main():
    print("Finding Polar Verity Sense...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and PVS_PREFIX in d.name, timeout=10.0
    )
    if not device:
        print("Device not found.")
        return

    print(f"Connecting to {device.name}...")
    async with BleakClient(device) as client:
        if not client.is_connected: return

        await client.start_notify(PMD_CONTROL, on_pmd_control)
        await client.start_notify(PMD_DATA, on_pmd_data)
        await asyncio.sleep(0.5)

        # 1. ENABLE SDK MODE (Critical for Raw Data)
        await send_cmd(client, bytearray([0x02, 0x09]), "SDK_MODE_ENABLE")
        
        # 2. START ACCELEROMETER (0x02)
        # 52Hz, 16-bit, 8G, 3ch
        await send_cmd(client, build_cmd(0x02, 52, 16, 8, 3), "START_ACC")

        # 3. START GYROSCOPE (0x05)
        # 52Hz, 16-bit, 2000dps, 3ch
        await send_cmd(client, build_cmd(0x05, 52, 16, 2000, 3), "START_GYR")
        
        # 4. START MAGNETOMETER (0x06)
        # 50Hz, 16-bit, 50G, 3ch
        await send_cmd(client, build_cmd(0x06, 50, 16, 50, 3), "START_MAG")

        # 5. START PPG (0x15) - Raw Heart Rate Data
        # Settings: 135Hz, 22-bit, 4 channels
        # Note: Range is often dummy 0x00 for PPG, or specific gain. 
        # We assume standard defaults for PVS SDK.
        # If this fails, query settings first (OpCode 0x01, 0x15).
        # We try strict settings observed in other PVS apps.
        # Rate=135(0x87), Res=22(0x16), Range=0(0), Ch=4
        cmd_ppg = build_cmd(0x15, 135, 22, 0, 4)
        await send_cmd(client, cmd_ppg, "START_PPG")

        print("\n--- STREAMING RAW DATA ---")
        print("Data Format:")
        print("  ACC/GYR/MAG: X, Y, Z integers")
        print("  PPG: Light intensity (Use this to calculate HR)")
        print("Press Ctrl+C to stop.\n")
        
        signal.signal(signal.SIGINT, signal_handler)
        while not stop_event.is_set():
            await asyncio.sleep(1)

        print("\nStopping streams...")
        # Stop all types
        for t in [0x02, 0x05, 0x06, 0x15]:
            await client.write_gatt_char(PMD_CONTROL, bytearray([0x03, t]))
        
        await asyncio.sleep(1.0)
        await send_cmd(client, bytearray([0x03, 0x09]), "SDK_MODE_DISABLE")
        print("Done.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass