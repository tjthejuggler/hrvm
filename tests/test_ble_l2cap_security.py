#!/usr/bin/env python3
"""
Test BLE connection with L2CAP security level set before GATT discovery.

The Polar H10 sends SMP Security Request (0x0b) after connection, but
kernel 6.14 rejects it as "unexpected". The workaround is to set the
BT_SECURITY level on the L2CAP socket to trigger pairing from our side
before the device sends its Security Request.

This script:
1. Opens a raw BLE L2CAP socket with BT_SECURITY_MEDIUM
2. This triggers kernel-level pairing before GATT discovery
3. Then uses bleak to connect (which should find services resolved)
"""
import asyncio
import socket
import struct
import logging
import time

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s.%(msecs)03d %(name)s %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("l2cap_security")
logging.getLogger("dbus_fast").setLevel(logging.WARNING)

# BLE L2CAP constants
BDADDR_LE_PUBLIC = 1
BTPROTO_L2CAP = 0
BT_SECURITY = 4
BT_SECURITY_LOW = 1
BT_SECURITY_MEDIUM = 2  # Triggers pairing (encryption)
BT_SECURITY_HIGH = 3
SOL_BLUETOOTH = 274

# ATT CID for BLE
ATT_CID = 4

DEVICE_ADDR = "24:AC:AC:14:94:29"


def set_l2cap_security(sock, level):
    """Set BT_SECURITY on an L2CAP socket."""
    # struct bt_security { __u8 level; __u8 key_size; }
    buf = struct.pack("BB", level, 0)
    sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY, buf)
    logger.info(f"Set BT_SECURITY to level {level}")


def addr_to_bytes(addr_str):
    """Convert 'AA:BB:CC:DD:EE:FF' to bytes."""
    return bytes(reversed([int(x, 16) for x in addr_str.split(":")]))


async def test_l2cap_connect():
    """Test connecting via raw L2CAP socket with security level set."""
    
    logger.info("=" * 60)
    logger.info("Approach 1: Raw L2CAP socket with BT_SECURITY_MEDIUM")
    logger.info("=" * 60)
    
    # Create BLE L2CAP socket
    # AF_BLUETOOTH=31, SOCK_SEQPACKET=5, BTPROTO_L2CAP=0
    try:
        sock = socket.socket(31, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
    except OSError as e:
        logger.error(f"Failed to create L2CAP socket: {e}")
        logger.info("This may require running as root or CAP_NET_RAW capability")
        return False
    
    # Set security level BEFORE connecting
    try:
        set_l2cap_security(sock, BT_SECURITY_MEDIUM)
    except OSError as e:
        logger.error(f"Failed to set security level: {e}")
        sock.close()
        return False
    
    # Bind to local adapter (any)
    # struct sockaddr_l2 for BLE:
    # sa_family (2) + l2_psm (2) + l2_bdaddr (6) + l2_cid (2) + l2_bdaddr_type (1)
    local_addr = struct.pack("<HH6sHB", 31, 0, b'\x00' * 6, ATT_CID, 0)
    
    try:
        sock.bind(local_addr)
        logger.info("Bound to local adapter")
    except OSError as e:
        logger.error(f"Failed to bind: {e}")
        sock.close()
        return False
    
    # Connect to remote device
    remote_bdaddr = addr_to_bytes(DEVICE_ADDR)
    remote_addr = struct.pack("<HH6sHB", 31, 0, remote_bdaddr, ATT_CID, BDADDR_LE_PUBLIC)
    
    sock.setblocking(False)
    
    logger.info(f"Connecting to {DEVICE_ADDR} via L2CAP ATT with security level MEDIUM...")
    t_start = time.time()
    
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.sock_connect(sock, remote_addr),
            timeout=30.0
        )
        t_conn = time.time() - t_start
        logger.info(f"L2CAP connected in {t_conn:.3f}s!")
        
        # Check security level after connection
        sec = sock.getsockopt(SOL_BLUETOOTH, BT_SECURITY, 2)
        level, key_size = struct.unpack("BB", sec)
        logger.info(f"Security after connect: level={level}, key_size={key_size}")
        
        # Try sending an ATT MTU Exchange Request to trigger GATT
        # ATT opcode 0x02 = Exchange MTU Request, MTU = 517
        att_mtu_req = struct.pack("<BH", 0x02, 517)
        sock.send(att_mtu_req)
        logger.info("Sent ATT Exchange MTU Request")
        
        # Wait for response
        await asyncio.sleep(1)
        try:
            data = sock.recv(1024)
            logger.info(f"Received ATT response: {data.hex()}")
            if data[0] == 0x03:  # Exchange MTU Response
                mtu = struct.unpack("<H", data[1:3])[0]
                logger.info(f"Remote MTU: {mtu}")
            elif data[0] == 0x01:  # Error Response
                logger.warning(f"ATT Error: opcode={data[1]:02x}, handle={struct.unpack('<H', data[2:4])[0]:04x}, error={data[4]:02x}")
        except BlockingIOError:
            logger.warning("No ATT response received")
        
        # Now try a Read By Group Type (discover primary services)
        # ATT opcode 0x10 = Read By Group Type Request
        # Start handle = 0x0001, End handle = 0xFFFF, UUID = 0x2800 (Primary Service)
        att_discover = struct.pack("<BHHH", 0x10, 0x0001, 0xFFFF, 0x2800)
        sock.send(att_discover)
        logger.info("Sent ATT Read By Group Type (discover services)")
        
        await asyncio.sleep(2)
        try:
            data = sock.recv(1024)
            logger.info(f"Received service discovery response ({len(data)} bytes): {data[:40].hex()}...")
            if data[0] == 0x11:  # Read By Group Type Response
                logger.info("SUCCESS! GATT service discovery is working!")
                # Parse services
                attr_len = data[1]
                offset = 2
                while offset + attr_len <= len(data):
                    if attr_len == 6:  # 16-bit UUID
                        start, end, uuid16 = struct.unpack("<HHH", data[offset:offset+6])
                        logger.info(f"  Service: handle={start:04x}-{end:04x}, UUID=0x{uuid16:04x}")
                    elif attr_len == 20:  # 128-bit UUID
                        start, end = struct.unpack("<HH", data[offset:offset+4])
                        uuid_bytes = data[offset+4:offset+20]
                        uuid_str = '-'.join([
                            uuid_bytes[15:11:-1].hex(),
                            uuid_bytes[11:9:-1].hex(),
                            uuid_bytes[9:7:-1].hex(),
                            uuid_bytes[7:5:-1].hex(),
                            uuid_bytes[5::-1].hex()
                        ])
                        logger.info(f"  Service: handle={start:04x}-{end:04x}, UUID={uuid_str}")
                    offset += attr_len
            elif data[0] == 0x01:  # Error Response
                err_code = data[4]
                logger.warning(f"ATT Error on service discovery: error_code=0x{err_code:02x}")
                if err_code == 0x05:
                    logger.warning("Authentication error - security level not sufficient")
                elif err_code == 0x0f:
                    logger.warning("Insufficient encryption")
        except BlockingIOError:
            logger.warning("No service discovery response received")
        
        sock.close()
        logger.info("Socket closed")
        return True
        
    except asyncio.TimeoutError:
        logger.error("L2CAP connection timed out (30s)")
        sock.close()
        return False
    except OSError as e:
        t_err = time.time() - t_start
        logger.error(f"L2CAP connection failed at {t_err:.3f}s: {e}")
        sock.close()
        return False


async def test_bleak_after_pairing():
    """After successful L2CAP security, try bleak connection."""
    from bleak import BleakScanner, BleakClient
    
    logger.info("=" * 60)
    logger.info("Approach 2: Bleak connection after L2CAP security setup")
    logger.info("=" * 60)
    
    # Scan
    device = await BleakScanner.find_device_by_address(DEVICE_ADDR, timeout=10.0)
    if not device:
        logger.error("Device not found")
        return
    
    logger.info(f"Found: {device.name}")
    
    async with BleakClient(device, timeout=30.0) as client:
        logger.info(f"Connected: {client.is_connected}")
        for svc in client.services:
            logger.info(f"  Service: {svc.uuid}")
        

async def main():
    # First try raw L2CAP approach
    success = await test_l2cap_connect()
    
    if success:
        logger.info("\nL2CAP approach worked! Now trying bleak...")
        await asyncio.sleep(2)
        await test_bleak_after_pairing()
    else:
        logger.error("\nL2CAP approach failed. The issue is at the kernel level.")


if __name__ == "__main__":
    asyncio.run(main())
