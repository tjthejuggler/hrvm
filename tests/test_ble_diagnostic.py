#!/usr/bin/env python3
"""
Comprehensive BLE diagnostic for Polar H10 connection issues.

This script:
1. Cleans up any stale device state
2. Scans for the Polar H10
3. Connects and monitors GATT service discovery at D-Bus level
4. Reports detailed timing and state changes

Run with: python3 tests/test_ble_diagnostic.py
"""
import asyncio
import logging
import time
import sys

# Enable full bleak debug logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s.%(msecs)03d %(name)s %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("diagnostic")

# Suppress noisy loggers but keep bleak
logging.getLogger("dbus_fast").setLevel(logging.WARNING)


async def main():
    from bleak import BleakScanner, BleakClient
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager

    DEVICE_NAME = "Polar H10"
    DEVICE_ADDR = "24:AC:AC:14:94:29"

    # Step 1: Initialize bleak's BlueZ manager and inspect its state
    logger.info("=" * 60)
    logger.info("STEP 1: Initializing BlueZ manager")
    logger.info("=" * 60)

    manager = await get_global_bluez_manager()
    bus = manager._bus
    logger.info(f"BlueZ manager initialized, bus connected: {bus.connected}")
    logger.info(f"Bus unique name: {bus.unique_name}")

    # Check if device already exists in BlueZ
    device_path = f"/org/bluez/hci0/dev_{DEVICE_ADDR.replace(':', '_')}"
    try:
        is_connected = manager.is_connected(device_path)
        is_paired = manager.is_paired(device_path)
        logger.info(f"Device already known to BlueZ: connected={is_connected}, paired={is_paired}")
    except Exception:
        logger.info("Device not yet known to BlueZ (good - clean state)")

    # Step 2: Scan for the device
    logger.info("=" * 60)
    logger.info("STEP 2: Scanning for Polar H10")
    logger.info("=" * 60)

    device = None
    adv_data = None

    def detection_callback(dev, advertisement_data):
        nonlocal device, adv_data
        if dev.address == DEVICE_ADDR or (dev.name and DEVICE_NAME in dev.name):
            device = dev
            adv_data = advertisement_data
            logger.info(f"Found device: {dev.name} ({dev.address})")
            logger.info(f"  RSSI: {advertisement_data.rssi}")
            logger.info(f"  Service UUIDs: {advertisement_data.service_uuids}")
            logger.info(f"  Manufacturer data: {advertisement_data.manufacturer_data}")
            logger.info(f"  Service data: {advertisement_data.service_data}")
            logger.info(f"  TX Power: {advertisement_data.tx_power}")

    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    
    # Wait up to 10 seconds for device
    for i in range(20):
        if device:
            break
        await asyncio.sleep(0.5)
    
    await scanner.stop()

    if not device:
        logger.error("Polar H10 not found! Is it active (being worn)?")
        logger.error("The Polar H10 only advertises when it detects skin contact.")
        return

    logger.info(f"Device found after scanning")

    # Step 3: Connect WITHOUT pairing first
    logger.info("=" * 60)
    logger.info("STEP 3: Connecting to device (no pairing)")
    logger.info("=" * 60)

    connected_event = asyncio.Event()
    disconnected_event = asyncio.Event()
    services_resolved = asyncio.Event()

    def on_disconnect(client):
        logger.warning(f"DISCONNECTED callback fired at {time.time():.3f}")
        disconnected_event.set()

    # Monitor D-Bus property changes for the device
    original_parse_msg = manager._parse_msg

    def patched_parse_msg(message):
        # Intercept property changes for our device
        try:
            if message.member == "PropertiesChanged" and message.body:
                interface, changed, invalidated = message.body
                if interface == "org.bluez.Device1":
                    path = message.path
                    if DEVICE_ADDR.replace(":", "_") in (path or ""):
                        for key, variant in changed.items():
                            val = variant.value if hasattr(variant, 'value') else variant
                            logger.info(f"  D-Bus Property Change: {key} = {val}")
                            if key == "ServicesResolved" and val:
                                services_resolved.set()
                            if key == "Connected" and val:
                                connected_event.set()
                            if key == "Connected" and not val:
                                disconnected_event.set()
        except Exception as e:
            logger.debug(f"Parse intercept error: {e}")
        
        # Call original
        original_parse_msg(message)

    manager._parse_msg = patched_parse_msg

    try:
        logger.info(f"Creating BleakClient for {device.address}")
        client = BleakClient(
            device,
            disconnected_callback=on_disconnect,
            timeout=30.0,
        )

        t_start = time.time()
        logger.info(f"Calling client.connect() at t=0.000")

        try:
            await client.connect()
            t_connected = time.time() - t_start
            logger.info(f"client.connect() returned successfully at t={t_connected:.3f}")
            logger.info(f"Connected: {client.is_connected}")

            # List discovered services
            logger.info("=" * 60)
            logger.info("STEP 4: Discovered Services")
            logger.info("=" * 60)
            for service in client.services:
                logger.info(f"  Service: {service.uuid} - {service.description}")
                for char in service.characteristics:
                    logger.info(f"    Char: {char.uuid} - {char.description} [{','.join(char.properties)}]")

            # Check for PMD service
            PMD_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
            pmd = client.services.get_service(PMD_UUID)
            if pmd:
                logger.info(f"PMD Service FOUND!")
            else:
                logger.warning(f"PMD Service NOT found in discovered services")
                logger.info("This may mean pairing IS required for PMD access")

            await client.disconnect()
            logger.info("Disconnected cleanly")

        except Exception as e:
            t_error = time.time() - t_start
            logger.error(f"Connection failed at t={t_error:.3f}: {type(e).__name__}: {e}")

            # If service discovery failed, let's try a different approach:
            # Use raw D-Bus to connect and wait longer
            logger.info("=" * 60)
            logger.info("STEP 5: Trying raw D-Bus connect with extended wait")
            logger.info("=" * 60)

            # First disconnect if connected
            try:
                reply = await bus.call(
                    bus.make_method_message(
                        "org.bluez",
                        device_path,
                        "org.bluez.Device1",
                        "Disconnect"
                    )
                )
            except Exception:
                pass

            await asyncio.sleep(2)

            # Connect via D-Bus directly
            connected_event.clear()
            disconnected_event.clear()
            services_resolved.clear()

            t_start2 = time.time()
            logger.info(f"Calling D-Bus Connect() at t=0.000")

            try:
                from dbus_fast import Message, MessageType

                reply = await bus.call(
                    Message(
                        destination="org.bluez",
                        path=device_path,
                        interface="org.bluez.Device1",
                        member="Connect",
                    )
                )
                t_conn = time.time() - t_start2
                logger.info(f"D-Bus Connect() returned at t={t_conn:.3f}, type={reply.message_type}")

                if reply.message_type == MessageType.ERROR:
                    logger.error(f"Connect error: {reply.error_name}: {reply.body}")
                else:
                    logger.info("Connect succeeded, waiting for ServicesResolved...")

                    # Wait up to 45 seconds for services
                    try:
                        await asyncio.wait_for(services_resolved.wait(), timeout=45.0)
                        t_resolved = time.time() - t_start2
                        logger.info(f"ServicesResolved=true at t={t_resolved:.3f}!")
                    except asyncio.TimeoutError:
                        t_timeout = time.time() - t_start2
                        logger.error(f"ServicesResolved never became true (waited {t_timeout:.1f}s)")

                        # Check current state
                        try:
                            is_conn = manager.is_connected(device_path)
                            logger.info(f"Still connected: {is_conn}")
                        except Exception:
                            logger.info("Device no longer in BlueZ")

            except Exception as e2:
                logger.error(f"D-Bus Connect failed: {e2}")

    finally:
        # Restore original parse_msg
        manager._parse_msg = original_parse_msg

        # Cleanup
        try:
            reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path=device_path,
                    interface="org.bluez.Device1",
                    member="Disconnect",
                )
            )
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
