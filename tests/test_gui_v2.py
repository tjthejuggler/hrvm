import multiprocessing
import time
import numpy as np
from multiprocessing import Pipe
from src.gui.ui_manager import UIManager
from src.utils.ipc import ProcessedData, BLECommand, ProcessingConfig

def mock_ble_process(pipe):
    """Mocks the BLE process, responding to connect/disconnect."""
    connected = False
    while True:
        if pipe.poll():
            cmd = pipe.recv()
            if isinstance(cmd, BLECommand):
                if cmd.command == "connect":
                    connected = True
                    pipe.send({"status": "connected", "battery": 85})
                elif cmd.command == "disconnect":
                    connected = False
                    pipe.send({"status": "disconnected"})
        
        if connected:
            # Simulate occasional battery updates
            if time.time() % 10 < 0.1:
                pipe.send({"battery": 84})
        
        time.sleep(0.1)

def mock_math_process(pipe):
    """Mocks the Math process, sending fake ProcessedData."""
    t = 0
    while True:
        # Check for config updates
        if pipe.poll():
            msg = pipe.recv()
            if isinstance(msg, ProcessingConfig):
                print(f"[MOCK MATH] Received config update: {msg}")
        
        # Generate fake data
        t += 1
        hr = 60 + 10 * np.sin(t * 0.1)
        rmssd = 30 + 5 * np.cos(t * 0.05)
        coherence = (np.sin(t * 0.2) + 1) / 2 # 0 to 1
        
        data = ProcessedData(
            timestamp=time.time(),
            ecg_window=np.zeros(260), # Empty ECG for now
            rr_intervals=[],
            heart_rate=hr,
            hrv_rmssd=rmssd,
            hrv_sdnn=40.0,
            quality_score=0.95,
            coherence_score=coherence,
            pacer_phase=0.0,
            is_assessing=False
        )
        
        pipe.send(data)
        time.sleep(1.0) # 1 Hz update

if __name__ == "__main__":
    # Create pipes
    gui_data, math_out = Pipe(duplex=False)
    gui_ble, ble_gui = Pipe(duplex=True)
    gui_math, math_gui = Pipe(duplex=True)
    
    # Start mock processes
    p_ble = multiprocessing.Process(target=mock_ble_process, args=(ble_gui,))
    p_math = multiprocessing.Process(target=mock_math_process, args=(math_gui,))
    
    p_ble.start()
    p_math.start()
    
    # Start GUI
    # Note: We pass a dummy shm_name since we aren't creating real SHM here
    ui = UIManager(gui_data, gui_ble, gui_math, shm_name="hrv_shm_test", auto_connect=True)
    
    try:
        ui.run()
    except KeyboardInterrupt:
        pass
    finally:
        p_ble.terminate()
        p_math.terminate()
