import socket
import threading
import argparse
import sys

def listen_tcp(port, arm_label):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(1)
    
    print(f"[*] ADB/TCP: Listening for {arm_label} on port {port}...")
    
    while True:
        try:
            conn, addr = sock.accept()
            print(f"\n[+] {arm_label} Connected via ADB Tunnel!")
            
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                print(f"[{arm_label}] {data.decode('utf-8')}", end='')
                
        except Exception as e:
            print(f"Error on {arm_label} TCP: {e}")
            break

def listen_udp(port, arm_label):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    
    print(f"[*] WiFi/UDP: Listening for {arm_label} on port {port}...")
    
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            print(f"[{arm_label}] {data.decode('utf-8')}", end='')
        except Exception as e:
            print(f"Error on {arm_label} UDP: {e}")
            break

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZenSignal Dual-Watch IMU Listener")
    # This group makes it so you MUST provide exactly one of these flags
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--adb', action='store_true', help="Use TCP for ADB reverse port forwarding")
    group.add_argument('--udp', action='store_true', help="Use UDP for direct WiFi streaming")
    
    args = parser.parse_args()

    if args.adb:
        print("--- STARTING IN ADB (TCP) MODE ---")
        threading.Thread(target=listen_tcp, args=(5555, "LEFT ARM"), daemon=True).start()
        threading.Thread(target=listen_tcp, args=(5556, "RIGHT ARM"), daemon=True).start()
    elif args.udp:
        print("--- STARTING IN WIFI (UDP) MODE ---")
        threading.Thread(target=listen_udp, args=(5555, "LEFT ARM"), daemon=True).start()
        threading.Thread(target=listen_udp, args=(5556, "RIGHT ARM"), daemon=True).start()

    try:
        # Keep the main thread alive while background listeners do the work
        while True:
            pass
    except KeyboardInterrupt:
        print("\nShutting down listeners.")
        sys.exit(0)