import socket
import threading

def listen_to_watch(port, arm_label):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(1)
    
    print(f"[*] Listening for {arm_label} on port {port}...")
    
    while True:
        try:
            conn, addr = sock.accept()
            print(f"\n[+] {arm_label} Connected!")
            
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                
                # We prefix the data with the arm label so your terminal makes sense
                print(f"[{arm_label}] {data.decode('utf-8')}", end='')
                
        except Exception as e:
            print(f"Error on {arm_label}: {e}")
            break

# Start two separate threads so neither blocks the other
threading.Thread(target=listen_to_watch, args=(5555, "LEFT ARM"), daemon=True).start()
threading.Thread(target=listen_to_watch, args=(5556, "RIGHT ARM"), daemon=True).start()

try:
    # Keep the main program running while the threads do the work
    while True:
        pass
except KeyboardInterrupt:
    print("\nShutting down listeners.")