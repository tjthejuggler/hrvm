import multiprocessing
import time
import logging
import sys
import argparse
import numpy as np
from multiprocessing import Process, Pipe, shared_memory
from multiprocessing.connection import Connection
from src.ble.ble_manager import ble_ingestion_main
from src.processing.signal_processor import signal_processing_main
from src.gui.ui_manager import UIManager
from src.utils.ipc import BLECommand, ProcessedData

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Main")

def main():
    parser = argparse.ArgumentParser(description="HRVM System Main Process")
    parser.add_argument("--mock", action="store_true", help="Run in mock mode (no physical device required)")
    parser.add_argument("--auto-connect", action="store_true", help="Automatically connect on startup (useful for testing)")
    args = parser.parse_args()

    logger.info(f"Starting HRVM System... (Mock Mode: {args.mock})")
    
    # 1. Create Shared Memory for ECG Display
    # 260 samples * 4 bytes (int32) = 1040 bytes
    shm_name = "hrvm_ecg_display"
    try:
        # Try to create, if exists, unlink and recreate to ensure clean state
        try:
            existing_shm = shared_memory.SharedMemory(name=shm_name)
            existing_shm.close()
            existing_shm.unlink()
        except FileNotFoundError:
            pass
            
        shm = shared_memory.SharedMemory(create=True, size=1040, name=shm_name)
        # Initialize with zeros
        buffer = np.ndarray((260,), dtype=np.int32, buffer=shm.buf)
        buffer[:] = 0
    except Exception as e:
        logger.error(f"Failed to create shared memory: {e}")
        return

    # 2. Create Pipes
    
    # Data Pipe 1: BLE -> Signal Processing
    # BLE writes to ble_data_send, Processing reads from proc_input_recv
    proc_input_recv, ble_data_send = Pipe(duplex=False)
    
    # Data Pipe 2: Signal Processing -> GUI
    # Processing writes to proc_output_send, GUI reads from gui_data_recv
    gui_data_recv, proc_output_send = Pipe(duplex=False)
    
    # Control Pipe 1: GUI -> BLE
    # GUI writes to ble_control_send, BLE reads from ble_control_recv
    # Note: BLE might send status back, so duplex=True
    ble_control_recv, ble_control_send = Pipe(duplex=True)
    
    # Control Pipe 2: GUI -> Signal Processing
    # GUI writes to proc_control_send, Processing reads from proc_control_recv
    proc_control_recv, proc_control_send = Pipe(duplex=True)
    
    # 3. Start Processes
    
    # Signal Processing Process
    proc_process = Process(
        target=signal_processing_main,
        args=(proc_input_recv, proc_output_send, proc_control_recv, shm_name),
        name="SignalProcessing"
    )
    proc_process.start()
    logger.info(f"Signal Processing Process started with PID: {proc_process.pid}")
    
    # BLE Process
    ble_process = Process(
        target=ble_ingestion_main,
        args=(ble_data_send, ble_control_recv, args.mock),
        name="BLE_Ingestion"
    )
    ble_process.start()
    logger.info(f"BLE Process started with PID: {ble_process.pid}")
    
    # 4. Run GUI in Main Process
    # Dear PyGui usually prefers running in the main thread
    try:
        logger.info("Creating UIManager...")
        print("[DEBUG] About to instantiate UIManager...")
        ui_manager = UIManager(
            data_pipe=gui_data_recv,
            ble_control_pipe=ble_control_send,
            math_control_pipe=proc_control_send,
            shm_name=shm_name,
            auto_connect=args.auto_connect
        )
        print("[DEBUG] UIManager instantiated successfully")
        logger.info("Starting GUI...")
        print("[DEBUG] About to call ui_manager.run()...")
        ui_manager.run()
        print("[DEBUG] ui_manager.run() returned")
        
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.error(f"GUI crashed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        logger.info("Shutting down...")
        
        # Send stop commands
        try:
            ble_control_send.send(BLECommand(command="exit", params={}))
            proc_control_send.send("STOP")
        except Exception as e:
            logger.error(f"Error sending stop commands: {e}")
        
        # Wait for termination
        ble_process.join(timeout=2.0)
        proc_process.join(timeout=2.0)
        
        if ble_process.is_alive():
            logger.warning("BLE process did not exit, terminating...")
            ble_process.terminate()
            ble_process.join(timeout=1.0) # Ensure it's really gone
        if proc_process.is_alive():
            logger.warning("Processing process did not exit, terminating...")
            proc_process.terminate()
            proc_process.join(timeout=1.0) # Ensure it's really gone
            
        # Clean up shared memory
        try:
            shm.close()
            shm.unlink()
        except FileNotFoundError:
            pass # Already cleaned up
        except Exception as e:
            logger.error(f"Error cleaning up shared memory: {e}")
        
        logger.info("System shutdown complete.")

if __name__ == "__main__":
    # Ensure spawn method for compatibility
    try:
        multiprocessing.set_start_method('spawn')
    except RuntimeError:
        pass # Context already set
    main()
