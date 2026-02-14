#!/usr/bin/env python3
"""Raw L2CAP BLE socket test with security level set before connect.

Uses ctypes to call connect() directly with the correct sockaddr_l2 struct,
bypassing Python's socket address parsing which doesn't support BLE L2CAP.
"""
import socket
import struct
import time
import ctypes
import ctypes.util

# Load libc for raw connect()
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

# Constants
AF_BLUETOOTH = 31
SOCK_SEQPACKET = 5
BTPROTO_L2CAP = 0
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_LOW = 1
BT_SECURITY_MEDIUM = 2
ATT_CID = 4
BDADDR_LE_PUBLIC = 1

DEVICE_ADDR = "24:AC:AC:14:94:29"


class sockaddr_l2(ctypes.Structure):
    _fields_ = [
        ("l2_family", ctypes.c_ushort),   # sa_family_t
        ("l2_psm", ctypes.c_ushort),      # __le16
        ("l2_bdaddr", ctypes.c_ubyte * 6),  # bdaddr_t (6 bytes, little-endian)
        ("l2_cid", ctypes.c_ushort),      # __le16
        ("l2_bdaddr_type", ctypes.c_ubyte),  # __u8
    ]


def addr_to_bytes(addr_str):
    """Convert 'AA:BB:CC:DD:EE:FF' to bdaddr_t (reversed byte order)."""
    parts = [int(x, 16) for x in addr_str.split(":")]
    return (ctypes.c_ubyte * 6)(*reversed(parts))


def main():
    # Create BLE L2CAP socket
    sock = socket.socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
    fd = sock.fileno()
    print(f"Socket created, fd={fd}")

    # Set security level BEFORE connecting
    sec_buf = struct.pack("BB6x", BT_SECURITY_MEDIUM, 0)
    sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY, sec_buf)
    print(f"Security level set to MEDIUM ({BT_SECURITY_MEDIUM})")

    # Prepare remote address
    addr = sockaddr_l2()
    addr.l2_family = AF_BLUETOOTH
    addr.l2_psm = 0
    addr.l2_bdaddr = addr_to_bytes(DEVICE_ADDR)
    addr.l2_cid = ATT_CID
    addr.l2_bdaddr_type = BDADDR_LE_PUBLIC

    print(f"Connecting to {DEVICE_ADDR} (LE Public, ATT CID={ATT_CID})...")
    t_start = time.time()

    # Use ctypes to call connect() directly
    ret = libc.connect(fd, ctypes.byref(addr), ctypes.sizeof(addr))
    if ret != 0:
        errno = ctypes.get_errno()
        import os
        err_msg = os.strerror(errno)
        t_err = time.time() - t_start
        print(f"Connect failed after {t_err:.3f}s: errno={errno} ({err_msg})")
        sock.close()
        return

    t_conn = time.time() - t_start
    print(f"CONNECTED in {t_conn:.3f}s!")

    # Check security level after connection
    sec = sock.getsockopt(SOL_BLUETOOTH, BT_SECURITY, 8)
    level = sec[0]
    key_size = sec[1]
    print(f"Security after connect: level={level}, key_size={key_size}")

    # Send ATT MTU Exchange Request (opcode 0x02, MTU=517)
    mtu_req = struct.pack("<BH", 0x02, 517)
    sock.send(mtu_req)
    print("Sent ATT Exchange MTU Request")

    sock.settimeout(5)
    try:
        data = sock.recv(1024)
        print(f"ATT Response ({len(data)} bytes): {data.hex()}")
        if data[0] == 0x03:  # Exchange MTU Response
            mtu = struct.unpack("<H", data[1:3])[0]
            print(f"  Remote MTU: {mtu}")
        elif data[0] == 0x01:  # Error Response
            print(f"  ATT Error: opcode=0x{data[1]:02x}, handle=0x{struct.unpack('<H', data[2:4])[0]:04x}, error=0x{data[4]:02x}")
    except socket.timeout:
        print("No ATT response (timeout)")

    # Discover primary services (Read By Group Type, UUID=0x2800)
    discover = struct.pack("<BHHH", 0x10, 0x0001, 0xFFFF, 0x2800)
    sock.send(discover)
    print("Sent ATT Read By Group Type (discover primary services)")

    try:
        data = sock.recv(1024)
        print(f"Service discovery response ({len(data)} bytes): {data.hex()}")
        if data[0] == 0x11:  # Read By Group Type Response
            print("SUCCESS! GATT services discovered:")
            attr_len = data[1]
            offset = 2
            while offset + attr_len <= len(data):
                if attr_len == 6:  # 16-bit UUID
                    start, end, uuid16 = struct.unpack("<HHH", data[offset:offset + 6])
                    print(f"  Service: handles 0x{start:04x}-0x{end:04x}, UUID=0x{uuid16:04x}")
                elif attr_len == 20:  # 128-bit UUID
                    start, end = struct.unpack("<HH", data[offset:offset + 4])
                    uuid_bytes = data[offset + 4:offset + 20]
                    # Convert to standard UUID string format
                    import uuid
                    u = uuid.UUID(bytes=bytes(reversed(uuid_bytes)))
                    print(f"  Service: handles 0x{start:04x}-0x{end:04x}, UUID={u}")
                offset += attr_len

            # Continue discovering (there may be more services)
            if attr_len == 6:
                last_end = struct.unpack("<H", data[offset - attr_len + 2:offset - attr_len + 4])[0]
            elif attr_len == 20:
                last_end = struct.unpack("<H", data[offset - attr_len + 2:offset - attr_len + 4])[0]
            else:
                last_end = 0xFFFF

            while last_end < 0xFFFF:
                discover2 = struct.pack("<BHHH", 0x10, last_end + 1, 0xFFFF, 0x2800)
                sock.send(discover2)
                try:
                    data2 = sock.recv(1024)
                    if data2[0] == 0x11:
                        attr_len2 = data2[1]
                        offset2 = 2
                        while offset2 + attr_len2 <= len(data2):
                            if attr_len2 == 6:
                                start, end, uuid16 = struct.unpack("<HHH", data2[offset2:offset2 + 6])
                                print(f"  Service: handles 0x{start:04x}-0x{end:04x}, UUID=0x{uuid16:04x}")
                                last_end = end
                            elif attr_len2 == 20:
                                start, end = struct.unpack("<HH", data2[offset2:offset2 + 4])
                                uuid_bytes2 = data2[offset2 + 4:offset2 + 20]
                                import uuid
                                u = uuid.UUID(bytes=bytes(reversed(uuid_bytes2)))
                                print(f"  Service: handles 0x{start:04x}-0x{end:04x}, UUID={u}")
                                last_end = end
                            offset2 += attr_len2
                    elif data2[0] == 0x01:  # Error (Attribute Not Found = end of services)
                        print("  (end of services)")
                        break
                except socket.timeout:
                    break

        elif data[0] == 0x01:  # Error Response
            err_code = data[4]
            print(f"ATT Error on service discovery: error=0x{err_code:02x}")
            if err_code == 0x05:
                print("  -> Insufficient Authentication (need pairing)")
            elif err_code == 0x0f:
                print("  -> Insufficient Encryption")
            elif err_code == 0x0a:
                print("  -> Attribute Not Found")
    except socket.timeout:
        print("No service discovery response (timeout)")

    sock.close()
    print("\nDone - socket closed")


if __name__ == "__main__":
    main()
