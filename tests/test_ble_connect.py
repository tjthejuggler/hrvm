#!/usr/bin/env python3
"""
Test script to connect to Polar H10 using bleak directly.
Tests connection and service discovery without pairing.
"""
import asyncio
import logging
import os

# Enable bleak debug logging
os.environ['BLEAK_LOGGING'] = '1'
logging.basicConfig(level=logging.DEBUG)

from bleak import BleakClient, BleakScanner

POLAR_PREFIX = "Polar H10"
PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"


async def main():
    print("=== Scanning for Polar H10 ===")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name.startswith(POLAR_PREFIX),
        timeout=10.0
    )

    if not device:
        print("Polar H10 not found!")
        return

    print(f"Found: {device.name} ({device.address})")

    def on_disconnect(client):
        print(f"!!! DISCONNECTED from {client.address}")

    print("\n=== Attempting connection (no pairing) ===")
    async with BleakClient(
        device,
        disconnected_callback=on_disconnect,
        timeout=30.0,
    ) as client:
        print(f"Connected: {client.is_connected}")
        print(f"MTU: {client.mtu_size}")

        print("\n=== Services ===")
        for service in client.services:
            print(f"  Service: {service.uuid} - {service.description}")
            for char in service.characteristics:
                print(f"    Char: {char.uuid} - {char.description} [{','.join(char.properties)}]")

        pmd_found = any(s.uuid == PMD_SERVICE_UUID for s in client.services)
        print(f"\n=== PMD Service found: {pmd_found} ===")

        if pmd_found:
            print("SUCCESS: PMD service is accessible without pairing!")
        else:
            print("FAIL: PMD service NOT found. Pairing may be required.")

        # Keep connected briefly
        await asyncio.sleep(2)

    print("\nDisconnected cleanly.")


if __name__ == '__main__':
    asyncio.run(main())
