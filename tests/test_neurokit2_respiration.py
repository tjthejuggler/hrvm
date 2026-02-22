import asyncio
import struct
import warnings
import time
import numpy as np
import neurokit2 as nk
from bleak import BleakClient, BleakScanner
from collections import deque

warnings.filterwarnings("ignore")

# PMD UUIDs
PMD_CONTROL_UUID = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA_UUID = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"

# Start / Stop Commands
ACC_WRITE = bytearray([0x02, 0x02, 0x00, 0x01, 0xC8, 0x00, 0x01, 0x01, 0x10, 0x00, 0x02, 0x01, 0x08, 0x00])
STOP_ACC_WRITE = bytearray([0x03, 0x02]) 

SAMPLING_RATE = 200  
WINDOW_SIZE_SEC = 15 
BUFFER_SIZE = SAMPLING_RATE * WINDOW_SIZE_SEC

acc_z_buffer = deque(maxlen=BUFFER_SIZE)
current_phase = None
first_packet_received = False

def control_handler(sender, data):
    """ Satisfies Polar's requirement to listen to the Control channel """
    pass

def acc_data_handler(sender, data):
    global first_packet_received
    if not first_packet_received:
        first_packet_received = True

    if data[0] == 0x02:
        frame_data = data[10:]
        step = 6
        for i in range(0, len(frame_data), step):
            if i + step <= len(frame_data):
                # Unpack X, Y, Z (we keep Z for chest expansion)
                x, y, z = struct.unpack('<hhh', frame_data[i:i+step])
                acc_z_buffer.append(z)

def filter_signal(signal):
    # Bandpass filter: 0.05 Hz to 0.7 Hz isolates human breathing
    return nk.signal_filter(signal, sampling_rate=50, lowcut=0.05, highcut=0.7, method='butterworth')

async def process_respiration():
    global current_phase
    
    while len(acc_z_buffer) < BUFFER_SIZE:
        if first_packet_received:
            pct = (len(acc_z_buffer) / BUFFER_SIZE) * 100
            print(f"Buffering Data: {pct:.1f}%", end="\r")
        await asyncio.sleep(0.5)
        
    print("\n\n✅ Buffer full! Real-time tracking active...\n")
    last_debug_time = time.time()

    while True:
        # Downsample 200Hz to 50Hz for high-speed processing
        signal = np.array(list(acc_z_buffer))[::4]
        
        try:
            # 1. Clean the signal using NeuroKit
            clean_sig = await asyncio.to_thread(filter_signal, signal)
            
            # 2. Find the instantaneous slope (current point vs 0.3 seconds ago)
            current_val = clean_sig[-1]
            past_val = clean_sig[-15]
            slope = current_val - past_val
            
            # 3. Determine Phase using a small threshold to prevent flickering
            threshold = 1.0 
            
            new_phase = current_phase
            if slope > threshold:
                new_phase = "INHALING"
            elif slope < -threshold:
                new_phase = "EXHALING"
                
            # Print if phase changes
            if new_phase != current_phase and new_phase is not None:
                current_phase = new_phase
                if current_phase == "INHALING":
                    print("\n🫁 INHALING...  🟢")
                else:
                    print("\n😮‍💨 EXHALING...  🔴")

            # --- DEBUG TICKER ---
            if time.time() - last_debug_time > 1.0:
                print(f"🔬 [DEBUG] Raw Z: {signal[-1]:5d} | Clean Z: {current_val:6.1f} | Slope: {slope:6.1f}")
                last_debug_time = time.time()
                
        except Exception as e:
            pass # Suppress math errors while the buffer stabilizes
            
        await asyncio.sleep(0.1)

async def main():
    global first_packet_received
    
    print("Scanning for Polar H10...")
    devices = await BleakScanner.discover()
    polar_device = next((d for d in devices if d.name and "Polar H10" in d.name), None)
            
    if not polar_device:
        print("❌ Polar H10 not found. Make sure the strap is snapped on securely.")
        return
        
    print(f"✅ Found {polar_device.name}. Connecting...")
    
    try:
        async with BleakClient(polar_device.address) as client:
            print("✅ Connected!")
            
            # Give Linux BlueZ a second to stabilize the connection parameters
            await asyncio.sleep(1.0)
            
            await client.start_notify(PMD_CONTROL_UUID, control_handler)
            await client.start_notify(PMD_DATA_UUID, acc_data_handler)
            
            print("🧹 Cleaning up old Bluetooth states...")
            try:
                await client.write_gatt_char(PMD_CONTROL_UUID, STOP_ACC_WRITE, response=True)
            except Exception:
                pass
                
            await asyncio.sleep(1.0)
            
            # SEND ONCE and WAIT PATIENTLY
            print("📡 Sending ACC Start Command...")
            await client.write_gatt_char(PMD_CONTROL_UUID, ACC_WRITE, response=True)
            
            print("⏳ Waiting for Polar H10 to initialize stream (can take up to 60s)...")
            wait_time = 0
            while not first_packet_received and wait_time < 60:
                print(f"   Patiently waiting... {wait_time}s", end="\r")
                await asyncio.sleep(1)
                wait_time += 1
                
            if not first_packet_received:
                print("\n❌ Stream never started. Try moistening the strap pads to ensure it detects a heart rate.")
                return

            print("\n🟢 First ACC packet received! Stream is flowing.")
            processing_task = asyncio.create_task(process_respiration())
            
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping properly...")
                
            processing_task.cancel()
            
            # Clean exit
            try:
                await client.write_gatt_char(PMD_CONTROL_UUID, STOP_ACC_WRITE, response=True)
                await client.stop_notify(PMD_DATA_UUID)
                await client.stop_notify(PMD_CONTROL_UUID)
            except Exception:
                pass
            print("Disconnected.")

    except Exception as e:
        print(f"\n❌ Bluetooth Connection Lost/Failed: {e}")
        print("Try turning your computer's Bluetooth off and on again.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass