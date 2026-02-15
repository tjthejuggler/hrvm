#!/usr/bin/env python3
"""Test ACC streaming using bleakheart library directly.
This validates whether the Polar H10 can deliver ACC data through our BLE stack.
"""
import asyncio
import sys
from bleak import BleakClient, BleakScanner
from bleakheart import PolarMeasurementData

POLAR_PREFIX = 'Polar H10'

async def main():
    print('=== Bleakheart ACC Test ===')
    print('Scanning...')
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name.startswith(POLAR_PREFIX),
        timeout=10.0
    )
    if not device:
        print('ERROR: Polar H10 not found!')
        sys.exit(1)
    print(f'Found: {device.name} ({device.address})')
    
    acc_count = 0
    
    async with BleakClient(device, timeout=30.0) as client:
        print(f'Connected: {client.is_connected}')
        
        # Pair
        try:
            await client.pair()
            print('Paired OK')
        except Exception as e:
            print(f'Pair result: {e}')
        
        # Check MTU
        try:
            mtu = client.mtu_size
            print(f'MTU: {mtu}')
        except Exception as e:
            print(f'MTU check: {e}')
        
        # Create PMD with acc queue
        acc_q = asyncio.Queue()
        pmd = PolarMeasurementData(client, acc_queue=acc_q)
        
        # Get settings
        print('\nQuerying ACC settings...')
        settings = await pmd.available_settings('ACC')
        print(f'ACC settings: {settings}')
        
        # Start streaming
        print('\nStarting ACC stream...')
        result = await pmd.start_streaming('ACC', SAMPLE_RATE=25, RESOLUTION=16, RANGE=8)
        print(f'Start result: {result}')
        
        if result[0] != 0:
            print(f'ERROR: Start failed with code {result[0]}: {result[1]}')
            return
        
        print('ACC streaming started! Collecting for 10 seconds...')
        
        # Collect data for 10 seconds
        for i in range(10):
            await asyncio.sleep(1)
            # Drain queue
            while not acc_q.empty():
                payload = acc_q.get_nowait()
                acc_count += 1
                meas, ts, samples = payload
                if acc_count <= 5 or acc_count % 20 == 0:
                    print(f'  ACC #{acc_count}: ts={ts}, {len(samples)} samples, first={samples[0] if samples else "none"}')
            print(f'  t={i+1}s: {acc_count} total packets')
        
        # Stop
        print('\nStopping...')
        stop_result = await pmd.stop_streaming('ACC')
        print(f'Stop result: {stop_result}')
        print(f'\n=== RESULT: {acc_count} ACC packets received ===')

asyncio.run(main())
