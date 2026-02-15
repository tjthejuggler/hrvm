#!/usr/bin/env python3
"""Debug ACC streaming - check if AcquireNotify fd receives data.
Also try forcing StartNotify instead of AcquireNotify.
"""
import asyncio
import logging
import os
import sys
from bleak import BleakClient, BleakScanner

logging.basicConfig(level=logging.DEBUG, format='%(name)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger('bleak.backends.bluezdbus.manager').setLevel(logging.WARNING)
logging.getLogger('bleak.backends.bluezdbus.scanner').setLevel(logging.WARNING)

POLAR_PREFIX = 'Polar H10'
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA    = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

data_packet_count = 0
ctrl_packet_count = 0

def on_raw_data(sender, data: bytearray):
    global data_packet_count
    data_packet_count += 1
    meas_type = data[0] if len(data) > 0 else -1
    print(f"  DATA #{data_packet_count}: len={len(data)}, meas_type=0x{meas_type:02x}, "
          f"first_bytes={data[:min(20, len(data))].hex()}")

def on_raw_ctrl(sender, data: bytearray):
    global ctrl_packet_count
    ctrl_packet_count += 1
    print(f"  CTRL #{ctrl_packet_count}: len={len(data)}, raw={data.hex()}")

async def main():
    print('=== ACC FD Debug Test (force StartNotify) ===')
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
        
        # Pair first
        try:
            await client.pair()
            print('Paired OK')
        except Exception as e:
            print(f'Pair result: {e}')
        
        # Subscribe to PMD Control (indicate - uses StartNotify)
        print('\n--- Subscribing to notifications ---')
        await client.start_notify(PMD_CONTROL, on_raw_ctrl)
        print('Subscribed to PMD Control (StartNotify)')
        
        # Subscribe to PMD Data - FORCE StartNotify instead of AcquireNotify
        # The bluez kwarg use_start_notify=True forces D-Bus signal path
        print('Subscribing to PMD Data with FORCED StartNotify...')
        await client.start_notify(PMD_DATA, on_raw_data, bluez={"use_start_notify": True})
        print('Subscribed to PMD Data (forced StartNotify)')
        
        # Build and send START command
        start_cmd = bytearray([
            0x02, 0x02,                    # START ACC
            0x00, 0x01, 0x19, 0x00,        # SAMPLE_RATE=25
            0x01, 0x01, 0x10, 0x00,        # RESOLUTION=16
            0x02, 0x01, 0x08, 0x00,        # RANGE=8
        ])
        print(f'\n--- Sending START command ---')
        print(f'Command: {start_cmd.hex()}')
        
        await client.write_gatt_char(PMD_CONTROL, start_cmd, response=True)
        print('START written')
        
        # Wait for control response
        await asyncio.sleep(1)
        print(f'Control responses: {ctrl_packet_count}')
        
        # Wait for data
        print(f'\n--- Waiting for ACC data (15 seconds) ---')
        for i in range(15):
            await asyncio.sleep(1)
            print(f'  t={i+1}s: {data_packet_count} data packets, {ctrl_packet_count} ctrl packets')
            if data_packet_count > 0:
                print('  >>> DATA RECEIVED! <<<')
                break
        
        # Send STOP
        print('\n--- Sending STOP ---')
        stop_cmd = bytearray([0x03, 0x02])
        await client.write_gatt_char(PMD_CONTROL, stop_cmd, response=True)
        await asyncio.sleep(1)
        
        # Stop notifications
        await client.stop_notify(PMD_DATA)
        await client.stop_notify(PMD_CONTROL)
        
        print(f'\n=== RESULT: {data_packet_count} data packets, {ctrl_packet_count} ctrl packets ===')

asyncio.run(main())
