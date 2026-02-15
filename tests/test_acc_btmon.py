#!/usr/bin/env python3
"""Debug ACC streaming with btmon capture.
Run btmon in a separate terminal first:
  sudo btmon -w /tmp/btmon_acc.log

This test subscribes to notifications BEFORE sending START,
and also tries subscribing to the PMD Data characteristic
by writing to its CCCD descriptor directly.
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

# CCCD UUID for enabling notifications
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

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
    print('=== ACC btmon Debug Test ===')
    print('NOTE: Run "sudo btmon" in another terminal to capture HCI traffic')
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
        
        # Pair first
        try:
            await client.pair()
            print('Paired OK')
        except Exception as e:
            print(f'Pair result: {e}')
        
        # List all services and characteristics
        print('\n--- All Services ---')
        for service in client.services:
            print(f'  Service: {service.uuid} ({service.description})')
            for char in service.characteristics:
                print(f'    Char: {char.uuid} props={char.properties} handle={char.handle}')
                for desc in char.descriptors:
                    print(f'      Desc: {desc.uuid} handle={desc.handle}')
        
        # Find PMD Data characteristic and its CCCD
        print('\n--- PMD Data CCCD ---')
        pmd_data_char = None
        pmd_data_cccd = None
        for service in client.services:
            for char in service.characteristics:
                if PMD_DATA.lower() in str(char.uuid).lower():
                    pmd_data_char = char
                    for desc in char.descriptors:
                        if CCCD_UUID.lower() in str(desc.uuid).lower():
                            pmd_data_cccd = desc
                            break
        
        if pmd_data_char:
            print(f'PMD Data char: handle={pmd_data_char.handle}, props={pmd_data_char.properties}')
        if pmd_data_cccd:
            print(f'PMD Data CCCD: handle={pmd_data_cccd.handle}')
            # Read current CCCD value
            try:
                cccd_val = await client.read_gatt_descriptor(pmd_data_cccd.handle)
                print(f'CCCD current value: {cccd_val.hex()}')
            except Exception as e:
                print(f'CCCD read failed: {e}')
        else:
            print('WARNING: No CCCD found for PMD Data!')
        
        # Subscribe to notifications
        print('\n--- Subscribing ---')
        await client.start_notify(PMD_CONTROL, on_raw_ctrl)
        print('Subscribed to PMD Control')
        
        # Use default notification method (AcquireNotify)
        await client.start_notify(PMD_DATA, on_raw_data)
        print('Subscribed to PMD Data')
        
        # Check CCCD after subscribe
        if pmd_data_cccd:
            try:
                cccd_val = await client.read_gatt_descriptor(pmd_data_cccd.handle)
                print(f'CCCD after subscribe: {cccd_val.hex()}')
            except Exception as e:
                print(f'CCCD read after subscribe failed: {e}')
        
        # Read PMD Control to check available features
        print('\n--- Reading PMD Control ---')
        try:
            pmd_ctrl_val = await client.read_gatt_char(PMD_CONTROL)
            print(f'PMD Control read: {pmd_ctrl_val.hex()}')
            # Parse feature bits
            if len(pmd_ctrl_val) >= 2:
                features = int.from_bytes(pmd_ctrl_val[:2], 'little')
                feat_names = {0: 'ECG', 1: 'PPG', 2: 'ACC', 3: 'PPI', 4: 'GYRO', 5: 'MAG'}
                enabled = [feat_names.get(i, f'BIT{i}') for i in range(16) if features & (1 << i)]
                print(f'  Features bitmask: 0x{features:04x} -> {enabled}')
        except Exception as e:
            print(f'PMD Control read failed: {e}')
        
        # Send START command
        start_cmd = bytearray([
            0x02, 0x02,                    # START ACC
            0x00, 0x01, 0x19, 0x00,        # SAMPLE_RATE=25
            0x01, 0x01, 0x10, 0x00,        # RESOLUTION=16
            0x02, 0x01, 0x08, 0x00,        # RANGE=8
        ])
        print(f'\n--- Sending START ---')
        print(f'Command ({len(start_cmd)} bytes): {start_cmd.hex()}')
        
        await client.write_gatt_char(PMD_CONTROL, start_cmd, response=True)
        print('START written (with response)')
        
        await asyncio.sleep(1)
        print(f'After START: {ctrl_packet_count} ctrl, {data_packet_count} data')
        
        # Wait for data
        print(f'\n--- Waiting 10 seconds ---')
        for i in range(10):
            await asyncio.sleep(1)
            print(f'  t={i+1}s: {data_packet_count} data, {ctrl_packet_count} ctrl')
            if data_packet_count > 0:
                break
        
        # STOP
        print('\n--- STOP ---')
        stop_cmd = bytearray([0x03, 0x02])
        await client.write_gatt_char(PMD_CONTROL, stop_cmd, response=True)
        await asyncio.sleep(1)
        
        await client.stop_notify(PMD_DATA)
        await client.stop_notify(PMD_CONTROL)
        
        print(f'\n=== RESULT: {data_packet_count} data, {ctrl_packet_count} ctrl ===')

asyncio.run(main())
