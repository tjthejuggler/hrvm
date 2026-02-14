#!/usr/bin/env python3
"""
Test: Register a D-Bus agent on bleak's own bus, then connect with pair=True.
This ensures the agent is available when BlueZ requests pairing confirmation.
"""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

from bleak import BleakClient, BleakScanner
from bleak.backends.bluezdbus.manager import get_global_bluez_manager
from dbus_fast.service import ServiceInterface, method

POLAR_PREFIX = "Polar H10"
AGENT_PATH = '/org/hrvm/agent'


class AutoAcceptAgent(ServiceInterface):
    """BlueZ agent that auto-accepts all pairing (NoInputNoOutput capability)."""

    def __init__(self):
        super().__init__('org.bluez.Agent1')

    @method()
    def Release(self):
        logger.info('[Agent] Released')

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):
        logger.info(f'[Agent] Auto-confirming pairing for {device} passkey={passkey}')

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):
        logger.info(f'[Agent] Auto-authorizing service {uuid} for {device}')

    @method()
    def RequestAuthorization(self, device: 'o'):
        logger.info(f'[Agent] Auto-authorizing device {device}')

    @method()
    def Cancel(self):
        logger.info('[Agent] Cancelled')


async def register_agent_on_bleak_bus():
    """Register our agent on bleak's internal D-Bus connection."""
    # Get bleak's global BlueZ manager - this initializes bleak's D-Bus connection
    manager = await get_global_bluez_manager()

    # Access bleak's internal D-Bus bus
    bus = manager._bus

    # Export our agent on bleak's bus
    agent = AutoAcceptAgent()
    bus.export(AGENT_PATH, agent)

    # Register with BlueZ AgentManager
    from dbus_fast import Message, MessageType
    reply = await bus.call(
        Message(
            destination='org.bluez',
            path='/org/bluez',
            interface='org.bluez.AgentManager1',
            member='RegisterAgent',
            signature='os',
            body=[AGENT_PATH, 'NoInputNoOutput'],
        )
    )
    if reply.message_type == MessageType.ERROR:
        logger.error(f"Failed to register agent: {reply.body}")
        return False

    reply = await bus.call(
        Message(
            destination='org.bluez',
            path='/org/bluez',
            interface='org.bluez.AgentManager1',
            member='RequestDefaultAgent',
            signature='o',
            body=[AGENT_PATH],
        )
    )
    if reply.message_type == MessageType.ERROR:
        logger.error(f"Failed to set default agent: {reply.body}")
        return False

    logger.info("Agent registered on bleak's D-Bus connection!")
    return True


async def main():
    print("=== Step 1: Register D-Bus agent ===")
    if not await register_agent_on_bleak_bus():
        print("FAILED to register agent. Exiting.")
        return

    print("\n=== Step 2: Scan for Polar H10 ===")
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

    print("\n=== Step 3: Connect with pair=True ===")
    try:
        async with BleakClient(
            device,
            disconnected_callback=on_disconnect,
            timeout=60.0,  # Extra time for pairing
            pair=True,
        ) as client:
            print(f"Connected: {client.is_connected}")

            print("\n=== Services ===")
            for service in client.services:
                print(f"  Service: {service.uuid} - {service.description}")
                for char in service.characteristics:
                    print(f"    Char: {char.uuid} [{','.join(char.properties)}]")

            pmd_found = any(
                s.uuid == "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
                for s in client.services
            )
            print(f"\n=== PMD Service found: {pmd_found} ===")

            if pmd_found:
                print("SUCCESS!")
            else:
                print("PMD not found even after pairing.")

            await asyncio.sleep(2)

        print("\nDisconnected cleanly.")
    except Exception as e:
        print(f"\nConnection failed: {e}")


if __name__ == '__main__':
    asyncio.run(main())
