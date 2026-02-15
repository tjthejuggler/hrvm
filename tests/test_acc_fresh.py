#!/usr/bin/env python3
"""Fresh ACC streaming test - removes device from BlueZ cache first,
then reconnects with clean state. Also tries explicit CCCD write
and both notification methods.
"""
import asyncio
import logging
import os
import sys
import struct
from bleak import BleakClient, BleakScanner
from bleak.backends.bluezdbus.manager import get_global_bluez_manager

logging.basicConfig(level=logging.DEBUG, format='%(name)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger('bleak.backends.bluezdbus.manager').setLevel(logging.WARNING)
logging.getLogger('bleak.backends.bluezdbus.scanner').setLevel(logging.WARNING)

POLAR_PREFIX = 'Polar H10'
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA    = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
CCCD_UUID   = "00002902-0000-1000-8000-00805f9b34fb"

data_packet_count = 0
ctrl_packet_count = 0

def on_raw_data(sender, data: bytearray):
    global data_packet_count
    data_packet_count += 1
    print(f"  DATA #{data_packet_count}: len={len(data)}, raw={data[:min(30, len(data))].hex()}")

def on_raw_ctrl(sender, data: bytearray):
    global ctrl_packet_count
    ctrl_packet_count += 1
    print(f"  CTRL #{ctrl_packet_count}: len={len(data)}, raw={data.hex()}")

async def main():
    print('=== Fresh ACC Streaming Test ===')
    print()
    
    # Step 1: Remove device from BlueZ cache
    print('--- Step 1: Remove device from BlueZ cache ---')
    try:
        result = await asyncio.create_subprocess_exec(
            'bluetoothctl', 'remove', '24:AC:AC:14:94:29',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()
        print(f'  bluetoothctl remove: {stdout.decode().strip()}')
    except Exception as e:
        print(f'  Remove failed (OK if not cached): {e}')
    
    await asyncio.sleep(2)
    
    # Step 2: Scan for device
    print('\n--- Step 2: Scanning ---')
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name.startswith(POLAR_PREFIX),
        timeout=15.0
    )
    if not device:
        print('ERROR: Polar H10 not found!')
        sys.exit(1)
    print(f'Found: {device.name} ({device.address})')
    
    # Step 3: Connect fresh
    print('\n--- Step 3: Connecting ---')
    async with BleakClient(device, timeout=30.0) as client:
        print(f'Connected: {client.is_connected}')
        
        # Step 4: Pair
        print('\n--- Step 4: Pairing ---')
        try:
            await client.pair()
            print('Paired OK')
        except Exception as e:
            print(f'Pair result: {e}')
        
        # Step 5: Acquire MTU
        print('\n--- Step 5: MTU ---')
        try:
            backend = client._backend
            await backend._acquire_mtu()
            mtu = client.mtu_size
            print(f'MTU: {mtu}')
        except Exception as e:
            print(f'MTU acquire failed: {e}')
        
        # Step 6: Check PMD Data CCCD before subscribe
        print('\n--- Step 6: CCCD check ---')
        pmd_data_char = None
        pmd_data_cccd = None
        for service in client.services:
            for char in service.characteristics:
                if PMD_DATA.lower() in str(char.uuid).lower():
                    pmd_data_char = char
                    for desc in char.descriptors:
                        if CCCD_UUID.lower() in str(desc.uuid).lower():
                            pmd_data_cccd = desc
        
        if pmd_data_cccd:
            try:
                cccd_val = await client.read_gatt_descriptor(pmd_data_cccd.handle)
                print(f'CCCD before subscribe: {cccd_val.hex()}')
            except Exception as e:
                print(f'CCCD read failed: {e}')
        
        # Step 7: Subscribe to PMD Control (indicate)
        print('\n--- Step 7: Subscribe PMD Control ---')
        await client.start_notify(PMD_CONTROL, on_raw_ctrl)
        print('Subscribed to PMD Control (indicate)')
        
        # Step 8: Subscribe to PMD Data using StartNotify (NOT AcquireNotify)
        # This forces D-Bus signal-based notifications instead of fd-based
        print('\n--- Step 8: Subscribe PMD Data (force StartNotify) ---')
        await client.start_notify(PMD_DATA, on_raw_data, 
                                   bluez={"use_start_notify": True})
        print('Subscribed to PMD Data (StartNotify forced)')
        
        # Step 9: Explicitly write CCCD to enable notifications
        print('\n--- Step 9: Explicit CCCD write ---')
        if pmd_data_cccd:
            try:
                # Write 0x0001 (enable notifications) to CCCD
                await client.write_gatt_descriptor(pmd_data_cccd.handle, 
                                                    bytearray([0x01, 0x00]))
                print('CCCD written: 0100 (notifications enabled)')
            except Exception as e:
                print(f'CCCD write failed: {e}')
            
            try:
                cccd_val = await client.read_gatt_descriptor(pmd_data_cccd.handle)
                print(f'CCCD after write: {cccd_val.hex()}')
            except Exception as e:
                print(f'CCCD read failed: {e}')
        
        # Step 10: Read PMD Control features
        print('\n--- Step 10: PMD Features ---')
        try:
            features_raw = await client.read_gatt_char(PMD_CONTROL)
            print(f'PMD Control: {features_raw.hex()}')
        except Exception as e:
            print(f'PMD Control read: {e}')
        
        # Step 11: Send START command
        start_cmd = bytearray([
            0x02, 0x02,                    # START ACC
            0x00, 0x01, 0xC8, 0x00,        # SAMPLE_RATE=200 (try higher rate)
            0x01, 0x01, 0x10, 0x00,        # RESOLUTION=16
            0x02, 0x01, 0x08, 0x00,        # RANGE=8
        ])
        print(f'\n--- Step 11: START (rate=200) ---')
        print(f'Command: {start_cmd.hex()}')
        
        await client.write_gatt_char(PMD_CONTROL, start_cmd, response=True)
        print('START written')
        
        await asyncio.sleep(1)
        print(f'After START: {ctrl_packet_count} ctrl, {data_packet_count} data')
        
        # Step 12: Wait for data
        print(f'\n--- Step 12: Waiting 15 seconds ---')
        for i in range(15):
            await asyncio.sleep(1)
            print(f'  t={i+1}s: {data_packet_count} data, {ctrl_packet_count} ctrl')
            if data_packet_count > 0:
                print('  >>> DATA RECEIVED! <<<')
                break
        
        # Step 13: STOP
        print('\n--- Step 13: STOP ---')
        stop_cmd = bytearray([0x03, 0x02])
        await client.write_gatt_char(PMD_CONTROL, stop_cmd, response=True)
        await asyncio.sleep(1)
        
        await client.stop_notify(PMD_DATA)
        await client.stop_notify(PMD_CONTROL)
        
        print(f'\n=== RESULT: {data_packet_count} data, {ctrl_packet_count} ctrl ===')

asyncio.run(main())
