import asyncio
import struct
import time
import numpy as np
from bleak import BleakClient, BleakScanner
from collections import deque
from pynput import keyboard

# PMD UUIDs
PMD_CONTROL_UUID = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA_UUID = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"

# Commands
ACC_WRITE = bytearray([0x02, 0x02, 0x00, 0x01, 0xC8, 0x00, 0x01, 0x01, 0x10, 0x00, 0x02, 0x01, 0x08, 0x00])
STOP_ACC_WRITE = bytearray([0x03, 0x02]) 

# Settings
SAMPLING_RATE = 200  
BUFFER_SEC = 2 
BUFFER_SIZE = SAMPLING_RATE * BUFFER_SEC

# Math Tuning
SMOOTHING_SAMPLES = 40  # 0.2 seconds
LOOKBACK_SAMPLES = 100  # 0.5 seconds
DROP_FACTOR = 0.15      # (15%) Chest must drop to 15% of max threshold to lose an active breath state

# Globals for streaming
acc_z_buffer = deque(maxlen=BUFFER_SIZE)
first_packet_received = False

# Calibration globals
CALIBRATING = True
CAL_STATE = None  
CAL_DATA = {
    'INHALING': deque(maxlen=400), 
    'EXHALING': deque(maxlen=400), 
    'HOLDING': deque(maxlen=400)
}

# Dynamic thresholds
THRESH_IN = 2.0
THRESH_EX = -2.0

def control_handler(sender, data):
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
                x, y, z = struct.unpack('<hhh', frame_data[i:i+step])
                acc_z_buffer.append(z)

# --- KEYBOARD LISTENER ---
def on_press(key):
    global CAL_STATE, CALIBRATING
    if not CALIBRATING:
        return
    try:
        if key.char == 'z': CAL_STATE = 'INHALING'
        elif key.char == 'x': CAL_STATE = 'EXHALING'
        elif key.char == 'c': CAL_STATE = 'HOLDING'
        elif key.char == 'q': CALIBRATING = False
    except AttributeError:
        pass

def recalculate_thresholds():
    global THRESH_IN, THRESH_EX
    
    holds = list(CAL_DATA['HOLDING'])
    inhales = list(CAL_DATA['INHALING'])
    exhales = list(CAL_DATA['EXHALING'])
    
    noise_floor = np.percentile(np.abs(holds), 85) if holds else 1.0
        
    if inhales and holds:
        median_in = np.median(inhales)
        THRESH_IN = noise_floor + (median_in - noise_floor) * 0.2
    elif inhales:
        THRESH_IN = np.median(inhales) * 0.3
    else:
        THRESH_IN = noise_floor * 1.5
        
    if exhales and holds:
        median_ex = np.median(exhales)
        THRESH_EX = -noise_floor + (median_ex - (-noise_floor)) * 0.2
    elif exhales:
        THRESH_EX = np.median(exhales) * 0.3
    else:
        THRESH_EX = -noise_floor * 1.5

    THRESH_IN = max(1.0, THRESH_IN)
    THRESH_EX = min(-1.0, THRESH_EX)

def get_predicted_phase(delta, current_active_phase):
    if current_active_phase == "INHALING":
        if delta < (THRESH_IN * DROP_FACTOR):
            if delta < THRESH_EX: return "EXHALING"
            else: return "HOLDING"
        return "INHALING" 
        
    elif current_active_phase == "EXHALING":
        if delta > (THRESH_EX * DROP_FACTOR): 
            if delta > THRESH_IN: return "INHALING"
            else: return "HOLDING"
        return "EXHALING"
        
    else: 
        if delta > THRESH_IN: return "INHALING"
        elif delta < THRESH_EX: return "EXHALING"
        return "HOLDING"

async def process_respiration():
    global CALIBRATING, CAL_STATE
    
    while len(acc_z_buffer) < BUFFER_SIZE:
        await asyncio.sleep(0.1)
        
    print("\n" + "="*60)
    print("🎯 LIVE CALIBRATION MODE")
    print("="*60)
    print("Tap and HOLD the keys below to match your breathing:")
    print("  [z] - INHALE")
    print("  [x] - EXHALE")
    print("  [c] - HOLD")
    print("  [q] - FINISH calibration and start tracking")
    print("\nWaiting for first keypress...\n")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    predicted_phase = "HOLDING"
    last_print_time = time.time()

    # --- CALIBRATION PHASE ---
    while CALIBRATING:
        data = np.array(acc_z_buffer)
        current_smoothed = np.mean(data[-SMOOTHING_SAMPLES:])
        past_smoothed = np.mean(data[-(SMOOTHING_SAMPLES + LOOKBACK_SAMPLES) : -LOOKBACK_SAMPLES])
        delta = current_smoothed - past_smoothed
        
        if CAL_STATE:
            CAL_DATA[CAL_STATE].append(delta)
            recalculate_thresholds()
            
        predicted_phase = get_predicted_phase(delta, predicted_phase)
        
        if time.time() - last_print_time > 0.4:
            if CAL_STATE:
                status = "✅" if CAL_STATE == predicted_phase else "⚠️"
                req = ""
                if predicted_phase == "HOLDING" and CAL_STATE == "INHALING": req = f"(Needs > {THRESH_IN:4.1f})"
                if predicted_phase == "HOLDING" and CAL_STATE == "EXHALING": req = f"(Needs < {THRESH_EX:4.1f})"
                if predicted_phase != "HOLDING" and CAL_STATE == "HOLDING": req = f"(Needs {THRESH_EX:4.1f} to {THRESH_IN:4.1f})"
                
                print(f"[{status}] You: {CAL_STATE:8s} | System: {predicted_phase:8s} | Delta: {delta:5.1f}  {req}")
            last_print_time = time.time()
            
        await asyncio.sleep(0.05)

    listener.stop()
    print("\n" + "="*60)
    print("🚀 LIVE TRACKING ACTIVE (Press Ctrl+C to stop)")
    print(f"   Final Thresholds -> In: >{THRESH_IN:4.1f} | Ex: <{THRESH_EX:4.1f}")
    print("="*60 + "\n")

    # --- REAL-TIME TRACKING PHASE ---
    current_phase = None
    candidate_phase = None
    debounce_count = 0
    
    # ASYMMETRIC DEBOUNCING
    DEBOUNCE_BREATH = 3  # ~0.15s (Extremely fast trigger for Inhale/Exhale)
    DEBOUNCE_HOLD = 12   # ~0.60s (Swallows the deadzone, prevents micro-holds!)
    
    last_debug_time = time.time()

    while True:
        data = np.array(acc_z_buffer)
        current_smoothed = np.mean(data[-SMOOTHING_SAMPLES:])
        past_smoothed = np.mean(data[-(SMOOTHING_SAMPLES + LOOKBACK_SAMPLES) : -LOOKBACK_SAMPLES])
        delta = current_smoothed - past_smoothed
        
        raw_phase = get_predicted_phase(delta, current_phase)
            
        if raw_phase == current_phase:
            debounce_count = 0
        else:
            if raw_phase == candidate_phase:
                debounce_count += 1
            else:
                candidate_phase = raw_phase
                debounce_count = 1
                
            # Use a longer limit if the system thinks we are trying to hold
            limit = DEBOUNCE_HOLD if candidate_phase == "HOLDING" else DEBOUNCE_BREATH
                
            if debounce_count >= limit:
                current_phase = candidate_phase
                debounce_count = 0
                
                if current_phase == "INHALING": print("\n🫁 INHALING...  🟢")
                elif current_phase == "HOLDING": print("\n⏸️ HOLDING...   🟡")
                else: print("\n😮‍💨 EXHALING...  🔴")

        if time.time() - last_debug_time > 1.0:
            print(f"🔬 [DEBUG] Delta: {delta:6.1f} | Active Phase: {current_phase}")
            last_debug_time = time.time()
                
        await asyncio.sleep(0.05)

async def main():
    global first_packet_received
    
    print("Scanning for Polar H10...")
    devices = await BleakScanner.discover()
    polar_device = next((d for d in devices if d.name and "Polar H10" in d.name), None)
            
    if not polar_device:
        print("❌ Polar H10 not found.")
        return
        
    print(f"✅ Found {polar_device.name}. Connecting...")
    
    try:
        async with BleakClient(polar_device.address) as client:
            print("✅ Connected!")
            await asyncio.sleep(1.0)
            
            await client.start_notify(PMD_CONTROL_UUID, control_handler)
            await client.start_notify(PMD_DATA_UUID, acc_data_handler)
            
            try: await client.write_gatt_char(PMD_CONTROL_UUID, STOP_ACC_WRITE, response=True)
            except Exception: pass
                
            await asyncio.sleep(1.0)
            
            print("📡 Sending ACC Start Command...")
            await client.write_gatt_char(PMD_CONTROL_UUID, ACC_WRITE, response=True)
            
            wait_time = 0
            while not first_packet_received and wait_time < 60:
                await asyncio.sleep(1)
                wait_time += 1
                
            if not first_packet_received:
                print("\n❌ Stream never started.")
                return

            processing_task = asyncio.create_task(process_respiration())
            
            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping properly...")
                
            processing_task.cancel()
            
            try:
                await client.write_gatt_char(PMD_CONTROL_UUID, STOP_ACC_WRITE, response=True)
                await client.stop_notify(PMD_DATA_UUID)
                await client.stop_notify(PMD_CONTROL_UUID)
            except Exception: pass
            print("Disconnected.")

    except Exception as e:
        print(f"\n❌ Bluetooth Connection Failed: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass