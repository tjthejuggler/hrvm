#!/usr/bin/env python3
"""Test both ECG and ACC streaming to determine if the issue is
PMD-notification-wide or ACC-specific. Also tests HR notifications
as a baseline (HR uses standard BLE notifications, not PMD).
"""
import asyncio
import logging
import sys
from bleak import BleakClient, BleakScanner

logging.basicConfig(level=logging.DEBUG, format='%(name)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger('bleak.backends.bluezdbus.manager').setLevel(logging.WARNING)
logging.getLogger('bleak.backends.bluezdbus.scanner').setLevel(logging.WARNING)

POLAR_PREFIX = 'Polar H10'
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA    = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
HR_CHAR     = "00002a37-0000-1000-8000-00805f9b34fb"

hr_count = 0
data_count = 0
ctrl_count = 0

def on_hr(sender, data: bytearray):
    global hr_count
    hr_count += 1
    flags = data[0]
    if flags & 1:
        hr = int.from_bytes(data[1:3], 'little')
    else:
        hr = data[1]
    if hr_count <= 5 or hr_count % 20 == 0:
        print(f"  HR #{hr_count}: {hr} bpm")

def on_data(sender, data: bytearray):
    global data_count
    data_count += 1
    mtype = data[0] if len(data) > 0 else -1
    type_names = {0: 'ECG', 1: 'PPG', 2: 'ACC', 3: 'PPI'}
    tname = type_names.get(mtype, f'0x{mtype:02x}')
    print(f"  DATA #{data_count}: type={tname}, len={len(data)}, first={data[:min(20, len(data))].hex()}")

def on_ctrl(sender, data: bytearray):
    global ctrl_count
    ctrl_count += 1
    print(f"  CTRL #{ctrl_count}: {data.hex()}")

async def main():
    print('=== ECG vs ACC Streaming Test ===')
    print()
    
    print('Scanning...')
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name.startswith(POLAR_PREFIX),
        timeout=10.0
    )
    if not device:
        print('ERROR: Polar H10 not found!')
        sys.exit(1)
    print(f'Found: {device.name} ({device.address})')
    
    async with BleakClient(device, timeout=30.0) as client:
        print(f'Connected: {client.is_connected}')
        
        # Pair
        try:
            await client.pair()
            print('Paired OK')
        except Exception as e:
            print(f'Pair: {e}')
        
        # Acquire MTU
        try:
            await client._backend._acquire_mtu()
            print(f'MTU: {client.mtu_size}')
        except Exception as e:
            print(f'MTU: {e}')
        
        # === Test 1: HR notifications (baseline) ===
        print('\n=== Test 1: HR Notifications (5 seconds) ===')
        await client.start_notify(HR_CHAR, on_hr)
        for i in range(5):
            await asyncio.sleep(1)
            print(f'  t={i+1}s: {hr_count} HR packets')
        await client.stop_notify(HR_CHAR)
        print(f'HR result: {hr_count} packets')
        
        # === Test 2: ECG streaming ===
        print('\n=== Test 2: ECG Streaming (10 seconds) ===')
        data_count = 0
        ctrl_count = 0
        
        await client.start_notify(PMD_CONTROL, on_ctrl)
        await client.start_notify(PMD_DATA, on_data)
        
        # ECG START: type=0x00, rate=130Hz, resolution=14bit
        ecg_start = bytearray([
            0x02, 0x00,                    # START ECG
            0x00, 0x01, 0x82, 0x00,        # SAMPLE_RATE=130
            0x01, 0x01, 0x0E, 0x00,        # RESOLUTION=14
        ])
        print(f'ECG START: {ecg_start.hex()}')
        await client.write_gatt_char(PMD_CONTROL, ecg_start, response=True)
        await asyncio.sleep(1)
        
        ecg_data_count = data_count
        for i in range(10):
            await asyncio.sleep(1)
            print(f'  t={i+1}s: {data_count} data, {ctrl_count} ctrl')
            if data_count > ecg_data_count + 5:
                print('  ECG data flowing!')
                break
        
        # ECG STOP
        ecg_stop = bytearray([0x03, 0x00])
        await client.write_gatt_char(PMD_CONTROL, ecg_stop, response=True)
        await asyncio.sleep(1)
        ecg_total = data_count
        print(f'ECG result: {ecg_total} data packets')
        
        # === Test 3: ACC streaming ===
        print('\n=== Test 3: ACC Streaming (10 seconds) ===')
        acc_start_count = data_count
        
        # ACC START: type=0x02, rate=25Hz, resolution=16bit, range=8G
        acc_start = bytearray([
            0x02, 0x02,                    # START ACC
            0x00, 0x01, 0x19, 0x00,        # SAMPLE_RATE=25
            0x01, 0x01, 0x10, 0x00,        # RESOLUTION=16
            0x02, 0x01, 0x08, 0x00,        # RANGE=8
        ])
        print(f'ACC START: {acc_start.hex()}')
        await client.write_gatt_char(PMD_CONTROL, acc_start, response=True)
        await asyncio.sleep(1)
        
        for i in range(10):
            await asyncio.sleep(1)
            new_data = data_count - acc_start_count
            print(f'  t={i+1}s: {new_data} new data, {data_count} total, {ctrl_count} ctrl')
            if new_data > 5:
                print('  ACC data flowing!')
                break
        
        # ACC STOP
        acc_stop = bytearray([0x03, 0x02])
        await client.write_gatt_char(PMD_CONTROL, acc_stop, response=True)
        await asyncio.sleep(1)
        acc_total = data_count - ecg_total
        
        # Cleanup
        await client.stop_notify(PMD_DATA)
        await client.stop_notify(PMD_CONTROL)
        
        print(f'\n=== SUMMARY ===')
        print(f'HR:  {hr_count} packets')
        print(f'ECG: {ecg_total} packets')
        print(f'ACC: {acc_total} packets')
        print(f'Total PMD data: {data_count}, ctrl: {ctrl_count}')

asyncio.run(main())
