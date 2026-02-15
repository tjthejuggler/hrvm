# Polar Flow-Sync Real-Time HRV Dashboard

## Overview
A high-performance, multi-process Python application for real-time Heart Rate Variability (HRV) biofeedback using the Polar H10 chest strap.

## Features
- **Real-Time HR & HRV:** Streams heart rate and RR intervals via the standard BLE Heart Rate Measurement characteristic, calculates RMSSD/SDNN in real-time.
- **Heartbeat Blink Indicator:** A small circle next to the HR display that flashes red on each detected heartbeat (via ECG R-wave detection) and fades back to black within 150ms. Uses a fast visual-only path from the ECG stream (~100-150ms latency) while keeping the standard HR service for accurate metrics.
- **External LED Ball Support:** Optionally drives an external LED ball (UDP, port 41412) in sync with the heartbeat blink. Uses the ball's native protocol (8-byte header + 4-byte `0x0a R G B` color command). Enable/disable and set the IP address from the "LED Ball" section in the left settings panel. Default IP: `10.122.252.133`.
- **Heartbeat Chart:** Individual heartbeats displayed as a stem plot with RR intervals (ms) and timing for each beat.
- **Accelerometer (IMU) Chart:** Real-time 3-axis accelerometer data from the Polar H10 PMD service (25 Hz default).
- **ECG Waveform Chart:** Real-time ECG waveform from the Polar H10 PMD service (130 Hz, 14-bit resolution). Displays a 5-second scrolling window of microvolt samples.
- **RSA Visualization:** "Snake" graph overlaying interpolated Heart Rate on a breathing pacer.
- **Coherence Score:** Real-time metric (0-100) indicating heart rate synchronization with breathing.
- **Collapsible Charts:** All charts (Biofeedback, Heartbeats, Accelerometer, ECG, Tachogram, Poincaré, RMSSD History, SDNN History, Coherence History) are individually collapsible tree-node sections — no tabs. Each history chart is full-width.
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
3. **Connect:** Click the "Connect" button in the top bar. The app will scan for a device named "Polar H10..." and connect automatically. You may see a system pairing prompt — click "Deny" once to proceed (the GATT connection still works without OS-level pairing).
4. **Data streaming:** Once connected, the app streams HR/RR interval data via BLE HR Measurement (`0x2A37`), accelerometer data, and ECG waveform data via the Polar PMD service. Note: PMD data may take up to 30–45 seconds to begin flowing due to BlueZ propagation delays.

### Controls
- **Connect/Disconnect:** Connects to the Polar H10 and begins data streaming.
- **Pacer Settings:** Adjust the target breathing rate (BPM).
- **Audio Feedback:** Toggle real-time heart rate sonification.
- **LED Ball:** Enable/disable the external LED ball and set its IP address. The ball flashes red on each heartbeat when enabled.
- **Resonance Assessment:** Click "Start Assessment" to begin the automated protocol (approx. 15 mins). Follow the on-screen pacer instructions.

## Architecture
- **`src/ble/`**: Bluetooth Low Energy management using `bleak`. Streams HR data via BLE HR Measurement and ACC/ECG data via Polar PMD service (`fb005c81/82`). Includes MTU negotiation, device pairing, and D-Bus `StartNotify` for reliable PMD streaming.
- **`src/processing/`**: Signal processing (filtering, interpolation, FFT) using `numpy` and `scipy`. Handles `HRBatch`, `ECGBatch`, and forwards `ACCBatch` data.
- **`src/gui/`**: User Interface using `Dear PyGui`. Charts are modular collapsible widgets in `charts.py`.
- **`src/gui/led_ball.py`**: `LEDBallController` — drives an external LED ball over UDP using the ball's native protocol (8-byte header + `0x0a R G B` color command). Integrated with the heartbeat blink in `ui_manager.py`.
- **`src/gui/charts.py`**: Collapsible chart widgets: BiofeedbackChart, HeartbeatChart, TachogramChart, PoincareChart, RMSSDHistoryChart, SDNNHistoryChart, CoherenceHistoryChart, ACCChart, ECGChart.
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
- **BLE Connection:** Uses `BleakScanner.find_device_by_filter()` with a single scan, then passes the `BLEDevice` to `BleakClient`. After connection, negotiates MTU via `_acquire_mtu()` (needed for PMD frames >200 bytes) and pairs via `client.pair()` (required for PMD service access).
- **HR Parsing:** Parses the standard BLE Heart Rate Measurement characteristic (flags, HR value, RR intervals in 1/1024s units).
- **PMD Notifications:** Uses D-Bus `StartNotify` (`use_start_notify=True`) instead of the default `AcquireNotify` for PMD Data characteristic. This prevents bluetoothd crashes on high-frequency PMD streams.
- **PMD Start Command Format:** `0x02 <type> [<setting_type:1> <array_len:1> <value:LE16>]...` per setting. Matches the format proven in `tests/test_acc_mtu_clean.py`.
- **ACC Streaming:** PMD type `0x02`, 25 Hz, 16-bit resolution, 8G range. Parses 3-axis int16 LE samples (milli-G).
- **ECG Streaming:** PMD type `0x00`, 130 Hz, 14-bit resolution. Start command: `02 00 00 01 82 00 01 01 0e 00`. Parses 3-byte signed microvolt samples (~73 per packet).
- **PMD Control Response:** Format is `f0 <op_code> <measurement_type> <status> [settings...]`. Previous code had a bug parsing `data[2]` as status when it's actually the measurement type byte.
- **Signal Processor Pipeline:** BLE → Signal Processor → GUI. The signal processor forwards raw `ACCBatch` and `ECGBatch` to the GUI for chart display, while also processing ECG for peak detection/HRV metrics. Debug logging traces the first 3 batches of each type through the pipeline.
- **Interpolation:** Cubic Spline / Pchip interpolation for smooth HR visualization.
- **Coherence:** Power Spectral Density (PSD) analysis using Welch's method.
- **Audio:** Real-time sine wave synthesis with smooth frequency transitions.

## License
MIT License

## Last Updated
2026-02-15 14:04 CET
