# Polar Flow-Sync Real-Time HRV Dashboard

## Overview
A high-performance, multi-process Python application for real-time Heart Rate Variability (HRV) biofeedback using the Polar H10 chest strap.

## Features
- **Real-Time HR & HRV:** Streams heart rate and RR intervals via the standard BLE Heart Rate Measurement characteristic, calculates RMSSD/SDNN in real-time.
- **BLE Auto-Reconnect:** Automatically reconnects to the Polar H10 if the Bluetooth connection drops unexpectedly (e.g., walking out of range). Uses exponential backoff (2s → 4s → 8s → ... up to 30s max). All previously active streams (HR, ACC, ECG) are re-enabled on reconnect. Session recording is preserved across disconnects — no data loss. The GUI shows an orange "Reconnecting..." status during attempts. Clicking "Disconnect" during reconnect cancels the auto-reconnect.
- **Session Mode Selection:** When clicking "Connect", a modal popup asks the user to choose a session mode: **Chess**, **Counting**, or **None**. The selected mode is displayed in the top bar and sent to the signal processor via IPC. Only "Chess" mode enables JSON session recording; "Counting" mode shows the Heartbeat Counting Game; "None" shows the standard dashboard. Auto-connect (`--auto-connect`) defaults to "None" mode.
- **Heartbeat Counting Game (Counting Mode):** An interoception training game that appears at the top of the charts section when "Counting" mode is selected. The user clicks "Start" and tries to silently count their own heartbeats. After a random interval (20–80 seconds, hidden from the user), the button changes to "Stop" and an input field becomes active. The user enters their guessed heartbeat count and clicks "Submit". The system calculates the actual BPM from RR intervals collected during the game period and extrapolates the guessed BPM from the user's count. Results are persisted to `counting_game_data.json` and displayed on a scatter chart showing guessed BPM vs actual BPM for each round.
- **Chess-Coach Session Recording:** Records HR sessions to JSON files for integration with the chess-coach analytics web app (only when "Chess" mode is selected). Files are saved to `~/Projects/chess-coach/data/hr_sessions/` in format v1.0 with 1Hz HR, raw RR intervals, and 5s-window RMSSD/SDNN (with artifact rejection). Recording auto-starts on first HR data and auto-stops on shutdown.
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
3. **Connect:** Click the "Connect" button in the top bar. A popup will ask you to select a session mode (**Chess**, **Counting**, or **None**). Choose "Chess" to enable JSON session recording for chess-coach integration. The app will then scan for a device named "Polar H10..." and connect automatically. You may see a system pairing prompt — click "Deny" once to proceed (the GATT connection still works without OS-level pairing).
4. **Data streaming:** Once connected, the app streams HR/RR interval data via BLE HR Measurement (`0x2A37`), accelerometer data, and ECG waveform data via the Polar PMD service. Note: PMD data may take up to 30–45 seconds to begin flowing due to BlueZ propagation delays.

### Controls
- **Connect/Disconnect:** Opens a session mode selection popup (Chess/Counting/None), then connects to the Polar H10 and begins data streaming. The selected mode is shown in the top bar.
- **Pacer Settings:** Adjust the target breathing rate (BPM).
- **Audio Feedback:** Toggle real-time heart rate sonification.
- **LED Ball:** Enable/disable the external LED ball and set its IP address. The ball flashes red on each heartbeat when enabled.
- **Resonance Assessment:** Click "Start Assessment" to begin the automated protocol (approx. 15 mins). Follow the on-screen pacer instructions.

## Polar Verity Sense Support

The app supports the **Polar Verity Sense** optical HR sensor as a secondary device alongside the Polar H10.

### Streams
Two mutually exclusive modes — the device cannot run SDK mode and PPI simultaneously:

**SDK Mode** (ACC + GYR + MAG + HR): enabled when any IMU stream is checked.

| Stream | Type | Rate | Resolution | Range | Channels |
|--------|------|------|-----------|-------|---------|
| ACC    | 0x02 | 52 Hz | 16-bit | 8G | 3 (X,Y,Z) |
| GYR    | 0x05 | 52 Hz | 16-bit | 2000 dps | 3 (X,Y,Z) |
| MAG    | 0x06 | 50 Hz | 16-bit | 50G | 3 (X,Y,Z) |

**Normal Mode** (PPI + HR): enabled only when all IMU streams are unchecked and PPI is checked.

| Stream | Type | Notes |
|--------|------|-------|
| PPI    | 0x03 | Pulse-to-pulse interval (HRV) |

**Hardware limitations confirmed:**
- **PPG (0x15):** Returns `INVALID_MEASUREMENT_TYPE` on this device firmware — not supported. PPG streaming and charts have been removed.
- **SDK mode vs PPI:** Mutually exclusive. The manager sends `SDK_MODE_DISABLE` before attempting PPI start to clear any stale device state from a previous session.
- **HR in SDK mode:** BLE HR service returns 0 bpm because SDK mode disables the internal HR algorithm. HR is only available in Normal (PPI) mode.

### UI Controls
The PVS top bar includes toggle checkboxes: **ACC**, **GYR**, **MAG**, **PPI**. ACC/GYR/MAG default to enabled; PPI defaults to disabled. Toggles are disabled while connected.

### Charts (PVS Graphs section)
- **PVS Acc (mg):** 3-axis accelerometer, 20s scrolling window
- **PVS Gyro (dps):** 3-axis gyroscope, 20s scrolling window
- **PVS Mag (Gauss/10):** 3-axis magnetometer, 20s scrolling window
- **PVS PPI (ms):** Pulse-to-pulse intervals as stem plot (Normal mode only)
- **PVS Heart Rate (BPM):** HR from BLE HR service or PPI stream

## Architecture
- **`src/ble/`**: Bluetooth Low Energy management using `bleak`. Streams HR data via BLE HR Measurement and ACC/ECG data via Polar PMD service (`fb005c81/82`). Includes MTU negotiation, device pairing, D-Bus `StartNotify` for reliable PMD streaming, and automatic reconnection with exponential backoff on unexpected disconnects. Also manages the Polar Verity Sense via `pvs_manager.py` using SDK Mode.
- **`src/ble/pvs_manager.py`**: `PolarVeritySenseManager` manages PVS BLE connection in a background thread. Supports two mutually exclusive modes: SDK Mode (ACC/GYR/MAG) and Normal Mode (PPI). Sends `SDK_MODE_DISABLE` before PPI start to clear stale device state.
- **`src/ble/pvs_parser.py`**: Parses raw PMD data packets for ACC, GYR, MAG, PPI. Supports both raw (0x00) and delta-compressed (0x80) frame types using bitmask `(frame_type & 0x80) == 0x80`. Provides `build_sdk_cmd()` for the proven SDK mode command format.
- **`src/processing/`**: Signal processing (filtering, interpolation, FFT) using `numpy` and `scipy`. Handles `HRBatch`, `ECGBatch`, and forwards `ACCBatch` data.
- **`src/gui/`**: User Interface using `Dear PyGui`. Charts are modular collapsible widgets in `charts.py`.
- **`src/gui/pvs_bar.py`**: `PolarVeritySenseBar` PVS connection bar with stream toggles (ACC, GYR, MAG, PPI) and Connect/Disconnect button.
- **`src/gui/pvs_charts.py`**: PVS chart widgets: `PVSAccChart`, `PVSGyroChart`, `PVSMagChart`, `PVSPPIChart`, `PVSHeartRateChart`.
- **`src/gui/led_ball.py`**: `LEDBallController` — drives an external LED ball over UDP using the ball's native protocol (8-byte header + `0x0a R G B` color command). Integrated with the heartbeat blink in `ui_manager.py`.
- **`src/gui/charts.py`**: Collapsible chart widgets: BiofeedbackChart, HeartbeatChart, TachogramChart, PoincareChart, RMSSDHistoryChart, SDNNHistoryChart, CoherenceHistoryChart, ACCChart, ECGChart.
- **`src/gui/counting_game.py`**: Heartbeat Counting Game module. Contains `CountingGameController` (state machine: idle → counting → input), `CountingGameWidget` (DearPyGui controls + scatter chart), and JSON persistence functions (`load_game_history`, `save_game_entry`). Data is stored in `counting_game_data.json`.
- **`src/recording/`**: Session recording for chess-coach integration. `SessionRecorder` accumulates HR/RR data in memory and writes JSON files on session stop.
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
- **Auto-Reconnect:** On unexpected disconnect, `_on_disconnect()` checks `_user_disconnect` flag. If `False`, schedules `_auto_reconnect()` as an asyncio task with exponential backoff (2s initial, 2× factor, 30s max). Remembers which streams (HR/ACC/ECG) were active and re-enables them after reconnect. The GUI receives `{"status": "reconnecting"}` IPC messages and shows orange "Reconnecting..." text. User can cancel by clicking "Disconnect". Session recording in the signal processor is unaffected — it lives in a separate process and simply resumes receiving data once the BLE link is restored.
- **Session Mode IPC:** The GUI sends `MSG_CMD_SET_SESSION_MODE` via the math control pipe before connecting. The signal processor stores the mode and only starts `SessionRecorder` when mode is `"chess"`. Three modes are defined in `ipc.py`: `SESSION_MODE_CHESS`, `SESSION_MODE_COUNTING`, `SESSION_MODE_NONE`.
- **Counting Game:** When "Counting" mode is selected, `CountingGameWidget` is built at the top of the charts area. During a game round, RR intervals from `ProcessedData` are fed to the controller via `feed_rr()`. The game timer is a random duration (20–80s) chosen at round start. Actual BPM = `(RR_count / duration) × 60`; guessed BPM = `(user_count / duration) × 60`. Results are appended to `counting_game_data.json` and the scatter chart updates immediately.

## License
MIT License

## Last Updated
2026-02-19 11:47 CET — Removed PPG (unsupported on device firmware); fixed PPI INVALID_STATE by sending SDK_MODE_DISABLE before PPI start to clear stale device state.
