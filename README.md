# Polar Flow-Sync Real-Time HRV Dashboard

## Overview
A high-performance, multi-process Python application for real-time Heart Rate Variability (HRV) biofeedback using the Polar H10 chest strap.

## Features
- **Real-Time HR & HRV:** Streams heart rate and RR intervals via the standard BLE Heart Rate Measurement characteristic, calculates RMSSD/SDNN in real-time.
- **Heartbeat Chart:** Individual heartbeats displayed as a stem plot with RR intervals (ms) and timing for each beat.
- **Accelerometer (IMU) Chart:** Real-time 3-axis accelerometer data from the Polar H10 PMD service (25 Hz default).
- **RSA Visualization:** "Snake" graph overlaying interpolated Heart Rate on a breathing pacer.
- **Coherence Score:** Real-time metric (0-100) indicating heart rate synchronization with breathing.
- **Collapsible Charts:** All charts (Biofeedback, Heartbeats, Accelerometer, Tachogram, Poincaré, RMSSD History, SDNN History, Coherence History) are individually collapsible tree-node sections — no tabs. Each history chart is full-width.
- **Resonance Frequency Assessment:** Automated protocol to find your optimal breathing rate.
- **Auditory Biofeedback:** Real-time sonification of heart rate for eyes-closed training.
- **Multi-Process Architecture:** Separate processes for BLE data acquisition, Signal Processing, and GUI rendering to ensure low latency.

## Installation

### Prerequisites
- Python 3.10+
- Polar H10 Heart Rate Sensor
- Bluetooth 4.0+ Adapter

### Linux (Ubuntu/Debian) Requirements
On Linux, you need to install the Bluetooth development headers and ensure your user has the correct permissions.

1. Install system dependencies:
   ```bash
   sudo apt-get update
   sudo apt-get install libglib2.0-dev libbluetooth-dev python3-dev portaudio19-dev
   ```

2. Ensure the Bluetooth service is running:
   ```bash
   sudo systemctl start bluetooth
   sudo systemctl enable bluetooth
   ```

### Setup
1. Clone the repository.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Connecting the Polar H10
1. **Wear the device:** Put on the Polar H10 chest strap. Ensure the electrode area is moist and the strap is tight against your skin.
2. **Run the application:**
   ```bash
   python src/main.py
   ```
3. **Connect:** Click the "Connect" button in the top bar. The app will scan for a device named "Polar H10...", resolve it by address (required for reliable Linux/BlueZ connections), and connect automatically.
4. **Data streaming:** Once connected, the app streams HR/RR interval data via BLE HR Measurement (`0x2A37`) and accelerometer data via the Polar PMD service. No manual pairing is required.

### Controls
- **Connect/Disconnect:** Connects to the Polar H10 and begins data streaming.
- **Pacer Settings:** Adjust the target breathing rate (BPM).
- **Audio Feedback:** Toggle real-time heart rate sonification.
- **Resonance Assessment:** Click "Start Assessment" to begin the automated protocol (approx. 15 mins). Follow the on-screen pacer instructions.

## Architecture
- **`src/ble/`**: Bluetooth Low Energy management using `bleak`. Streams HR data via BLE HR Measurement and ACC data via Polar PMD service (`fb005c81/82`).
- **`src/processing/`**: Signal processing (filtering, interpolation, FFT) using `numpy` and `scipy`. Handles `HRBatch`, `ECGBatch`, and forwards `ACCBatch` data.
- **`src/gui/`**: User Interface using `Dear PyGui`. Charts are modular collapsible widgets in `charts.py`.
- **`src/gui/charts.py`**: Collapsible chart widgets: BiofeedbackChart, HeartbeatChart, TachogramChart, PoincareChart, RMSSDHistoryChart, SDNNHistoryChart, CoherenceHistoryChart, ACCChart.
- **`src/database/`**: SQLite storage for session data.
- **`src/utils/ipc.py`**: IPC data classes (`HRBatch`, `ECGBatch`, `ACCBatch`, `ProcessedData`, `BLECommand`).

## Troubleshooting

### Bluetooth Connection Issues (Linux)
If the application fails to find the device or connects and immediately disconnects:

1. **Check Bluetooth Status:**
   ```bash
   systemctl status bluetooth
   ```
2. **Unblock Bluetooth:**
   ```bash
   rfkill list
   sudo rfkill unblock bluetooth
   ```
3. **Reset Bluetooth Adapter:**
   ```bash
   sudo hciconfig hci0 down
   sudo hciconfig hci0 up
   ```
4. **Permissions:** Ensure your user is in the `bluetooth` group (if applicable on your distro) or try running with `sudo` temporarily to rule out permission issues (though not recommended for daily use).

## Technical Details
- **BLE Connection:** Uses `BleakScanner.find_device_by_address()` to obtain a `BLEDevice` object, then passes it to `BleakClient` — this is the key pattern for reliable connections on Linux/BlueZ.
- **HR Parsing:** Parses the standard BLE Heart Rate Measurement characteristic (flags, HR value, RR intervals in 1/1024s units).
- **ACC Streaming:** Uses Polar PMD Control Point (`fb005c81`) to start ACC stream, receives data on PMD Data (`fb005c82`). Parses 3-axis int16 LE samples (milli-G) at 25 Hz.
- **Interpolation:** Cubic Spline / Pchip interpolation for smooth HR visualization.
- **Coherence:** Power Spectral Density (PSD) analysis using Welch's method.
- **Audio:** Real-time sine wave synthesis with smooth frequency transitions.

## License
MIT License

## Last Updated
2026-02-14 20:24 CET
