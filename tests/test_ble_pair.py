#!/usr/bin/env python3
"""
Test script to pair with Polar H10 using a D-Bus agent.
This registers a NoInputNoOutput agent that auto-accepts pairing,
then connects and pairs with the device.
"""
import asyncio
import sys
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from dbus_fast import BusType, Variant

AGENT_PATH = '/test/agent'
DEVICE_PATH = '/org/bluez/hci0/dev_24_AC_AC_14_94_29'


class AutoAcceptAgent(ServiceInterface):
    """A BlueZ agent that auto-accepts all pairing requests (NoInputNoOutput)."""

    def __init__(self):
        super().__init__('org.bluez.Agent1')

    @method()
    def Release(self):
        print('[Agent] Released')

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):
        print(f'[Agent] Auto-confirming pairing for {device} passkey={passkey}')
        # Just return (no error) = accept

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):
        print(f'[Agent] Auto-authorizing service {uuid} for {device}')

    @method()
    def RequestAuthorization(self, device: 'o'):
        print(f'[Agent] Auto-authorizing device {device}')

    @method()
    def Cancel(self):
        print('[Agent] Cancelled')


async def main():
    print("Connecting to system D-Bus...")
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    print(f"Connected to D-Bus: {bus.unique_name}")

    # Export agent
    agent = AutoAcceptAgent()
    bus.export(AGENT_PATH, agent)

    # Register agent with BlueZ
    print("Registering agent...")
    introspection = await bus.introspect('org.bluez', '/org/bluez')
    proxy = bus.get_proxy_object('org.bluez', '/org/bluez', introspection)
    manager = proxy.get_interface('org.bluez.AgentManager1')

    try:
        await manager.call_register_agent(AGENT_PATH, 'NoInputNoOutput')
        await manager.call_request_default_agent(AGENT_PATH)
        print("Agent registered as default!")
    except Exception as e:
        print(f"Failed to register agent: {e}")
        bus.disconnect()
        return

    # Get device interface
    print(f"Getting device interface for {DEVICE_PATH}...")
    try:
        introspection2 = await bus.introspect('org.bluez', DEVICE_PATH)
    except Exception as e:
        print(f"Device not found in BlueZ: {e}")
        print("Make sure the Polar H10 is powered on and has been scanned.")
        await manager.call_unregister_agent(AGENT_PATH)
        bus.disconnect()
        return

    proxy2 = bus.get_proxy_object('org.bluez', DEVICE_PATH, introspection2)
    device = proxy2.get_interface('org.bluez.Device1')
    props = proxy2.get_interface('org.freedesktop.DBus.Properties')

    # Check current state
    paired = await props.call_get('org.bluez.Device1', 'Paired')
    connected = await props.call_get('org.bluez.Device1', 'Connected')
    print(f"Current state - Paired: {paired.value}, Connected: {connected.value}")

    if not paired.value:
        print("Attempting to pair...")
        try:
            await asyncio.wait_for(device.call_pair(), timeout=30)
            print("Pairing successful!")
        except asyncio.TimeoutError:
            print("Pairing timed out after 30s")
        except Exception as e:
            print(f"Pairing error: {e}")

    # Check state after pairing
    paired = await props.call_get('org.bluez.Device1', 'Paired')
    connected = await props.call_get('org.bluez.Device1', 'Connected')
    print(f"After pair - Paired: {paired.value}, Connected: {connected.value}")

    if connected.value:
        # Wait for services to resolve
        print("Waiting for services to resolve...")
        for i in range(10):
            resolved = await props.call_get('org.bluez.Device1', 'ServicesResolved')
            if resolved.value:
                print(f"ServicesResolved: True (after {i+1}s)")
                break
            print(f"  ServicesResolved: False (waiting... {i+1}/10)")
            await asyncio.sleep(1)
        else:
            print("Services never resolved!")

    # Cleanup
    await manager.call_unregister_agent(AGENT_PATH)
    bus.disconnect()
    print("Done.")


if __name__ == '__main__':
    asyncio.run(main())
