# Polar Flow-Sync Real-Time HRV Dashboard

## Overview
A high-performance, multi-process Python application for real-time Heart Rate Variability (HRV) biofeedback using the Polar H10 chest strap.

## Features
- **Real-Time HR & HRV:** Streams heart rate and RR intervals via the standard BLE Heart Rate Measurement characteristic, calculates RMSSD/SDNN in real-time.
- **Unified HRV Section:** A dedicated **HRV** collapsible section in the GRAPHS area shows HRV charts (Tachogram, Poincaré, RMSSD, SDNN, Coherence) sourced from whichever HR device is active. The Polar H10 is preferred; the Polar Verity Sense (PPI stream) is used as a fallback when the H10 is not connected and the PVS is streaming HR. The section appears automatically when any HR source is active and hides when none is connected.
- **BLE Auto-Reconnect:** Automatically reconnects to the Polar H10 if the Bluetooth connection drops unexpectedly (e.g., walking out of range). Uses exponential backoff (2s → 4s → 8s → ... up to 30s max). All previously active streams (HR, ACC, ECG) are re-enabled on reconnect. Session recording is preserved across disconnects — no data loss. The GUI shows an orange "Reconnecting..." status during attempts. Clicking "Disconnect" during reconnect cancels the auto-reconnect.
- **Session Mode Selection:** When clicking "Connect", a modal popup asks the user to choose a session mode: **Chess**, **Counting**, or **None**. The selected mode is displayed in the top bar and sent to the signal processor via IPC. Only "Chess" mode enables JSON session recording; "Counting" mode shows the Heartbeat Counting Game; "None" shows the standard dashboard. Auto-connect (`--auto-connect`) defaults to "None" mode.
- **Resonance Breathing App:** A self-contained breathing pacer app in the APPS section. Provides manual timing inputs (Inhale / Hold Full / Exhale / Hold Empty in seconds), a live cycle-duration and BPM readout, a **Start/Stop** button that drives an animated breathing circle (expanding/contracting with colour-coded phases), and a **Session History** bar chart showing the resonance score achieved in each past session. Sessions are persisted to the SQLite database (`breathing_sessions` table). The resonance score is fed from the live coherence metric while a session is active.
- **Heartbeat Counting Game (Counting Mode):** An interoception training game that appears at the top of the charts section when "Counting" mode is selected. The user clicks "Start" and tries to silently count their own heartbeats. After a random interval (20–80 seconds, hidden from the user), the button changes to "Stop" and an input field becomes active. The user enters their guessed heartbeat count and clicks "Submit". The system calculates the actual BPM from RR intervals collected during the game period and extrapolates the guessed BPM from the user's count. Results are persisted to `counting_game_data.json` and displayed on a scatter chart showing guessed BPM vs actual BPM for each round.
- **Chess-Coach Session Recording:** Records HR sessions to JSON files for integration with the chess-coach analytics web app (only when "Chess" mode is selected). Files are saved to `~/Projects/chess-coach/data/hr_sessions/` in format v1.0 with 1Hz HR, raw RR intervals, and 5s-window RMSSD/SDNN (with artifact rejection). Recording auto-starts on first HR data and auto-stops on shutdown.
- **Heartbeat Blink Indicator:** A small circle next to the HR display that flashes red on each detected heartbeat (via ECG R-wave detection) and fades back to black within 150ms. Uses a fast visual-only path from the ECG stream (~100-150ms latency) while keeping the standard HR service for accurate metrics.
- **External LED Ball Support:** Optionally drives an external LED ball (UDP, port 41412) in sync with the heartbeat blink. Uses the ball's native protocol (8-byte header + 4-byte `0x0a R G B` color command). Enable/disable and set the IP address from the "LED Ball" section in the left settings panel. Default IP: `10.122.252.133`.
- **Heartbeat Chart:** Individual heartbeats displayed as a stem plot with RR intervals (ms) and timing for each beat.
- **Accelerometer (IMU) Charts:** Real-time 3-axis accelerometer data from the Polar H10 PMD service (25 Hz default). Includes one combined chart (all 3 axes overlaid) plus individual charts for each axis (X, Y, Z) — all collapsible independently.
- **ACC-Based Respiration Detection:** *(updated 2026-02-22)* Automatic real-time breathing phase detection (INHALE / EXHALE / HOLD) derived from the Polar H10 accelerometer Z-axis **or** the Genki Wave 3-axis accelerometer (user-selectable). A dedicated **Breathing Phase** chart shows the current phase as a lung-fullness curve. **6 breathing profiles:** `standing`, `sitting`, `laying` (with hold state) and `standing_nohold`, `sitting_nohold`, `laying_nohold` (inhale/exhale only). **Breath Source** selector in Settings chooses which device drives the chart and breath rate display. H10 calibration saved to `acc_breath_cal.json`; Genki calibration saved to `genki_breath_cal.json`.
- **Genki Wave Respiration:** *(2026-02-22)* The Genki Wave top bar now includes a **Breath Profile** dropdown and **Calibrate Breath** button. During calibration all 3 axes (X, Y, Z) are recorded simultaneously; on finish the axis with the highest inhale/exhale separation is auto-selected and stored. Same 6 profiles and no-hold logic as the H10 engine. Calibration popup shows axis selection feedback and the auto-selected axis after finishing.
- **ECG Waveform Chart:** Real-time ECG waveform from the Polar H10 PMD service (130 Hz, 14-bit resolution). Displays a 5-second scrolling window of microvolt samples.
- **Coherence Score (ACC-driven):** *(2026-02-22)* The Coherence History chart now uses the ACC-detected real-time breath rate when available, instead of the hardcoded pacer target. The signal processor receives the live breath rate via IPC (`SET_ACC_BREATH_RATE`) and uses it as the reference frequency for coherence calculation. Falls back to the pacer target BPM when no ACC breath rate is available.
- **Coherence Score:** Real-time metric (0-100) indicating heart rate synchronization with breathing.
- **Collapsible Charts:** All charts are individually collapsible tree-node sections — no tabs. Each history chart is full-width. All HRV-related charts (Tachogram, Poincaré, RMSSD, SDNN, Coherence) use a **~60-second scrolling window** — data builds up until one minute is reached, then old data scrolls out of view as new data arrives. **Collapsed charts are never rendered** — `update_plot()` checks the open/closed state of each tree node before doing any DPG work, reducing CPU usage when sections are folded.
- **Resonance Breathing → LTX Ball Output:** *(2026-02-23)* The **Manual Breathing** tab of the Resonance Breathing app now has a **"Use LTX Ball"** checkbox. When enabled, the connected LTX LED ball is driven in real-time by the breath bar and the **assessment leaderboard score** (0–260+): **brightness follows the breath bar** (bright on inhale, dim on exhale; minimum 5% so the ball never goes fully dark). At the **peak of each inhale** (bar reaches full) the ball flashes to black for 100 ms then returns to full brightness. At the **trough of each exhale** (bar reaches empty) the ball flashes to full brightness for 100 ms then returns to its dim level. The **base color** is determined by the best leaderboard score seen so far in the current session (seeded from history on startup): Red (0–80) → Orange (80–130) → Green (130–170) → Blue (170–210) → Pink (210–230) → Yellow (230–245) → White (245+). Blue is calibrated to appear around scores of ~200, which represents very good coherence. Pink, Yellow, and White are intentionally rare to give users a long-term goal. The feature uses the same `LEDBallController` instance already configured in Settings (IP address, enable/disable).
- **Resonance Frequency Assessment:** *(updated 2026-02-22)* Automated protocol to find your optimal breathing rate. Now includes a **"Use ACC Breathing"** checkbox in the Assessment tab. When checked, the coherence score for each test block is calculated using the real-time ACC-detected breath rate instead of the prescribed pacer rate (the pacer is still shown and followed visually). Results are stored in **two separate leaderboards** — "Prescribed Rate" and "ACC Breathing" — accessible as sub-tabs in the Assessment Leaderboard tab, so sessions from each method can be compared independently. The source of each result (`prescribed` or `acc`) is also saved to `rf_history.json`.
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
- **Resonance Breathing App:** Found in the APPS section. Set inhale/hold/exhale/hold timings, then click **Start** to begin a guided breathing session with the animated circle. Click **Stop** to end the session and save it to history.
- **Audio Feedback:** Toggle real-time heart rate sonification.
- **LED Ball:** Enable/disable the external LED ball and set its IP address. The ball flashes red on each heartbeat when enabled.

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

## TicWatch Support

The app supports two **TicWatch** smartwatches (Left and Right) running a custom Wear OS app that streams IMU data.  Each watch is a fully **independent device** — you can use just one or both at the same time.

Two connection modes are supported, selectable per watch via a dropdown before clicking **Start**:

| Mode | Transport | When to use |
|------|-----------|-------------|
| **ADB** (default) | TCP via `adb reverse` tunnel | Watch connected to host via USB / ADB Wi-Fi |
| **UDP** | Direct UDP over Wi-Fi | Both devices on the same network, no USB needed |

Ports are **hardcoded**: Left = **5555**, Right = **5556**.

### ADB mode — setup (run once per watch per session)
The host runs a **TCP server**; the watch is the TCP client.  `adb reverse` makes the watch's loopback address point back to the host, so no Wi-Fi IP address is ever needed.

```
Watch app  →  adb reverse  →  host localhost:PORT  →  our TCP server
```

```bash
# Left watch  → port 5555
adb -s <LEFT_WATCH_SERIAL>  reverse tcp:5555 tcp:5555

# Right watch → port 5556
adb -s <RIGHT_WATCH_SERIAL> reverse tcp:5556 tcp:5556
```

Find serial numbers with `adb devices`.  If only one watch is connected you can omit `-s <serial>`.

### UDP mode — setup
No ADB setup required.  Configure the watch app to send UDP datagrams to the host's IP address on port **5555** (Left) or **5556** (Right).  Both devices must be on the same Wi-Fi network.

### Protocol
The watch sends newline-terminated UTF-8 lines (same format for both modes):

```
"A,x.xx,y.yy,z.zz\n"   — Accelerometer
"G,x.xx,y.yy,z.zz\n"   — Gyroscope
```

### UI Controls
Each watch has its **own separate top bar row**:

| Row | Colour | Controls |
|-----|--------|----------|
| **TicWatch Left** | Purple/violet | port label · **ADB/UDP dropdown** · status dot · **Start / Stop** |
| **TicWatch Right** | Cyan/teal | port label · **ADB/UDP dropdown** · status dot · **Start / Stop** |

- Select **ADB** or **UDP** from the dropdown *before* clicking Start.  The dropdown is locked while the listener is running.
- Click **Start** to open the server and wait for the watch to connect.
- The status dot turns **yellow** while waiting, **green** when streaming, **red** when stopped.
- Starting or stopping one watch has no effect on the other.
- In ADB mode, if the watch app is restarted the server automatically accepts the new connection — no need to click Stop/Start again.

### Charts
Each watch has its own collapsible graph subsection that appears automatically when data arrives and hides when the listener is stopped:
- **TW Left/Right Acc (m/s²):** 3-axis accelerometer, 20s scrolling window
- **TW Left/Right Gyro (rad/s):** 3-axis gyroscope, 20s scrolling window
- **TW Left/Right Mag (µT):** 3-axis magnetometer, 20s scrolling window

### LTX Controller Integration
Both watches are available as independent trigger sources in the LTX Controller:
- **TicWatch Left** — Accelerometer, Gyroscope, Magnetometer
- **TicWatch Right** — Accelerometer, Gyroscope, Magnetometer

### Files
- **`src/ble/ticwatch_manager.py`**: `SingleTicWatchManager` — per-watch server supporting ADB (TCP) and UDP modes. Call `set_mode(MODE_ADB)` or `set_mode(MODE_UDP)` before `start()`. Ports hardcoded (`PORT_LEFT=5555`, `PORT_RIGHT=5556`). In ADB mode accepts one TCP connection at a time and re-listens automatically after disconnect. In UDP mode receives datagrams continuously.
- **`src/gui/ticwatch_bar.py`**: `TicWatchLeftBar` (purple) and `TicWatchRightBar` (cyan) — each an independent bar row with port label, ADB/UDP mode dropdown, status dot, and Start/Stop button. The dropdown is disabled while the listener is running.
- **`src/gui/ticwatch_charts.py`**: Chart widgets for both watches: `TicWatchLeftAccChart`, `TicWatchLeftGyroChart`, `TicWatchLeftMagChart`, `TicWatchRightAccChart`, `TicWatchRightGyroChart`, `TicWatchRightMagChart`.

## Architecture
- **`src/ble/`**: Bluetooth Low Energy management using `bleak`. Streams HR data via BLE HR Measurement and ACC/ECG data via Polar PMD service (`fb005c81/82`). Includes MTU negotiation, device pairing, D-Bus `StartNotify` for reliable PMD streaming, and automatic reconnection with exponential backoff on unexpected disconnects. Also manages the Polar Verity Sense via `pvs_manager.py` using SDK Mode.
- **`src/ble/pvs_manager.py`**: `PolarVeritySenseManager` manages PVS BLE connection in a background thread. Supports two mutually exclusive modes: SDK Mode (ACC/GYR/MAG) and Normal Mode (PPI). Sends `SDK_MODE_DISABLE` before PPI start to clear stale device state. Uses the device's own `timestamp_ns` (from the PMD packet header) to compute per-sample timestamps via `_device_ts_to_wall()`, eliminating backwards-going timestamps caused by per-packet `time.time()` jitter when ACC and GYR packets arrive interleaved.
- **`src/ble/pvs_parser.py`**: Parses raw PMD data packets for ACC, GYR, MAG, PPI. Supports both raw (0x00) and delta-compressed (0x80) frame types using bitmask `(frame_type & 0x80) == 0x80`. Provides `build_sdk_cmd()` for the proven SDK mode command format.
- **`src/processing/`**: Signal processing (filtering, interpolation, FFT) using `numpy` and `scipy`. Handles `HRBatch`, `ECGBatch`, and forwards `ACCBatch` data.
- **`src/gui/`**: User Interface using `Dear PyGui`. Charts are modular collapsible widgets in `charts.py`.
- **`src/gui/pvs_bar.py`**: `PolarVeritySenseBar` PVS connection bar with stream toggles (ACC, GYR, MAG, PPI) and Connect/Disconnect button.
- **`src/gui/pvs_charts.py`**: PVS chart widgets: `PVSAccChart`, `PVSGyroChart`, `PVSMagChart`, `PVSPPIChart`, `PVSHeartRateChart`.
- **`src/gui/led_ball.py`**: `LEDBallController` — drives an external LED ball over UDP using the ball's native protocol (8-byte header + `0x0a R G B` color command). Integrated with the heartbeat blink in `ui_manager.py`.
- **`src/gui/charts.py`**: Collapsible chart widgets: BiofeedbackChart, HeartbeatChart, TachogramChart, PoincareChart, RMSSDHistoryChart, SDNNHistoryChart, CoherenceHistoryChart, ACCChart, ACCXChart, ACCYChart, ACCZChart, ECGChart. Also contains the device-agnostic HRV section charts: `HRVTachogramChart`, `HRVPoincareChart`, `HRVRMSSDChart`, `HRVSDNNChart`, `HRVCoherenceChart` — all with 60s scrolling windows. All charts inherit `is_visible()` from `CollapsibleChart` and skip rendering when their tree node is collapsed.
- **`src/gui/counting_game.py`**: Heartbeat Counting Game module. Contains `CountingGameController` (state machine: idle → counting → input), `CountingGameWidget` (DearPyGui controls + scatter chart), and JSON persistence functions (`load_game_history`, `save_game_entry`). Data is stored in `counting_game_data.json`.
- **`src/gui/resonance_breathing.py`**: Resonance Breathing App. `ResonanceBreathingWidget` provides manual timing inputs, a Start/Stop button, an animated `PacerEngine` breathing circle, and a session-history bar chart. Sessions are persisted via `DatabaseManager.save_breathing_session()` and loaded on startup via `get_breathing_sessions()`.
- **`src/gui/rapid_change_game.py`**: Rapid Change Game. `RapidChangeController` (state machine: idle → racing → returning → finished) and `RapidChangeWidget` (DearPyGui controls + bar chart + past-config list). Settings: mode (one_way / return), Start HR, End/Peak HR, and **Breathing Only** checkbox. All threshold crossings require **5 consecutive readings** beyond the target before the condition is confirmed, preventing false triggers from noisy sensor data. History is persisted to `rapid_change_data.json`; the bar chart and past-config list filter by the exact current settings (including `breathing_only`).
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
2026-02-23 10:48 CET — **Resonance Frequency Assessment: Session History tab**. The Assessment Leaderboard area now has a third sub-tab: **Session History**. Unlike the existing Prescribed Rate and ACC Breathing leaderboard tabs (which show cumulative best-per-BPM for the current session), the Session History tab shows **every individual test block ever completed**, loaded from `rf_history.json` on startup and updated live after each assessment. Each row shows: Date/Time, BPM, Ratio, Score (color-coded by coherence tier), Phase/PLV %, PT Amp, LFnu %, and Source (Prescribed / ACC). Controls: **Filter Source** (All / Prescribed / ACC), **Sort by** (any column), **Sort direction** (Ascending / Descending), and a **Clear History** button that wipes `rf_history.json` and resets the in-memory history. The history is rebuilt from the flat leaderboard arrays stored inside each run entry in `rf_history.json` — no schema changes required; existing history files are fully compatible.

2026-02-23 10:48 CET — **Resonance Frequency Assessment: Session History tab**. The Assessment Leaderboard area now has a third sub-tab: **Session History**. Unlike the existing Prescribed Rate and ACC Breathing leaderboard tabs (which show cumulative best-per-BPM for the current session), the Session History tab shows **every individual test block ever completed**, loaded from `rf_history.json` on startup and updated live after each assessment. Each row shows: Date/Time, BPM, Ratio, Score (color-coded by coherence tier), Phase/PLV %, PT Amp, LFnu %, and Source (Prescribed / ACC). Controls: **Filter Source** (All / Prescribed / ACC), **Sort by** (any column), **Sort direction** (Ascending / Descending), and a **Clear History** button that wipes `rf_history.json` and resets the in-memory history. The history is rebuilt from the flat leaderboard arrays stored inside each run entry in `rf_history.json` — no schema changes required; existing history files are fully compatible.

2026-02-22 17:22 CET — **Genki Wave respiration + breath source selector**. New `src/processing/genki_respiration.py` (`GenkiRespirationEngine`) adds Genki Wave ACC-based breathing detection. Key differences from the H10 engine: receives 3-axis (X, Y, Z) samples simultaneously; calibration records all 3 axes; `finish_calibration()` auto-selects the axis with the highest inhale/exhale separation (|median_in − median_ex|); stores `(axis_idx, thresh_in, thresh_ex)` in `genki_breath_cal.json` (separate from H10's `acc_breath_cal.json`). The Genki Wave top bar gains a **Breath Profile** dropdown and **Calibrate Breath** button; the calibration popup shows live delta, threshold, and axis feedback. A new **Breath Source** radio button in the Settings popup (`H10 (ACC Z-axis)` / `Genki Wave (auto-axis)`) controls which engine drives the Breathing Phase chart and breath rate display. The inactive engine still runs in the background so switching sources is instant. Shared helper `_weighted_percentile_standalone()` extracted to module level in `acc_respiration.py` and reused by both engines.

2026-02-22 16:28 CET — **ACC respiration: faster response, better calibration, 6 profiles**. Three improvements to the ACC breathing engine (`src/processing/acc_respiration.py`): (1) **Faster delta computation** — smoothing window reduced from 0.2 s to 0.1 s and lookback window from 0.5 s to 0.3 s, so the lung-fullness chart responds to breathing changes ~2× faster. Debounce for inhale/exhale transitions reduced from 3 to 2 frames (~0.10 s). (2) **Better calibration weighting** — calibration now uses exponentially-weighted percentiles (recent key-press samples have more influence than older ones via a 0.995 decay factor). Threshold is placed at 50% between the noise floor and the signal peak (was 20%), giving a much more decisive trigger. (3) **6 breathing profiles** — added `standing_nohold`, `sitting_nohold`, `laying_nohold` alongside the existing 3. No-hold profiles have only INHALE / EXHALE states; the HOLD button is hidden in the calibration popup and `[C]` key is not registered. The `_get_predicted_phase()` method never returns `"HOLDING"` for no-hold profiles. Stale calibration data (old thresholds of ±172) cleared from `acc_breath_cal.json`.

2026-02-22 12:45 CET — **H10 ACC individual axis charts + collapsed-chart CPU optimisation**. The Polar H10 accelerometer section now shows four charts: one combined chart (all 3 axes overlaid) plus individual charts for X, Y, and Z axes — each independently collapsible. All charts in `charts.py` now skip `update_plot()` rendering entirely when their tree node is collapsed (`is_visible()` check on `CollapsibleChart` base class), reducing CPU and GPU load when sections are folded. No gyro charts are added for the H10 as the device does not expose a gyroscope stream.

2026-02-22 10:03 CET — **TicWatch: ADB/UDP mode selection**. `SingleTicWatchManager` now supports both ADB (TCP via `adb reverse`) and WiFi (UDP) connection modes. A new `set_mode()` method selects the mode before `start()`. Each TicWatch bar row gains an **ADB / UDP dropdown** that is locked while the listener is running. The mode is applied at connect time; status messages include the active mode label (e.g. "Waiting for watch… (UDP)"). No changes to ports, protocol, or chart code.

2026-02-21 19:27 CET — **TicWatch finalised: hardcoded ports, adb reverse, no config needed**. Ports are now hardcoded (Left=5555, Right=5556) — no port inputs in the UI. Corrected ADB command from `forward` to `reverse`. Removed all port config persistence. Bar rows simplified to label + status dot + Start/Stop only.

2026-02-21 19:18 CET — **TicWatch protocol corrected to TCP/ADB tunnel**. The working test (`tests/test_ticwatch.py`) revealed the protocol is TCP (not UDP): the host is the TCP server, the watch connects via `adb reverse`. Rewrote `src/ble/ticwatch_manager.py` as a per-device TCP server (`SingleTicWatchManager`) that accepts one connection at a time and re-listens automatically after disconnect. No IP address is ever needed — the ADB tunnel always connects to localhost.

2026-02-21 14:55 CET — **TicWatch Left & Right integration (independent devices)**. Each TicWatch is now a fully independent device with its own top bar row (Left=purple, Right=cyan), its own port input, its own Start/Stop button, and its own graph subsection. New files: `src/ble/ticwatch_manager.py`, `src/gui/ticwatch_bar.py` (`TicWatchLeftBar` + `TicWatchRightBar`), `src/gui/ticwatch_charts.py`. Both watches integrated into `ui_manager.py` render loop and LTX Controller trigger sources.

2026-02-21 10:42 CET — **UI polish: resonance breathing info panel, record button labels, persistent recording types**. Three changes in this update: (1) **Resonance Breathing info panel** — replaced the single small timing text line with a horizontal panel of five large (size-22 font) labelled metrics: INHALE (seconds), EXHALE (seconds), RATE (BPM), RATIO (1:N), and TIME LEFT (MM:SS). All five update live during assessment and manual breathing sessions. (2) **Record/Stop button labels** — replaced the `⏺`/`⏹` Unicode symbols (which rendered as `?` on the system font) with plain ASCII `[REC]` and `[STOP]` labels. (3) **Persistent recording types** — custom recording-type labels added via the `+ Add` popup are now saved to `recording_types.json` in the project root and reloaded on every app start, so they survive restarts.

2026-02-20 15:53 CET — **Graph fixes: coherence saw-teeth & HR Y-axis range**. Fixed the coherence score graph dropping to 0 between updates (saw-tooth pattern): the signal processor now carries forward the last valid coherence score instead of emitting 0.0 on every HR batch that falls within the 1-second throttle window. A score of 0.0 is only used as the initial value before any valid reading arrives. Fixed the HR (Heart Rate & Pacer) chart Y-axis being too wide: instead of a hard-coded 40–120 BPM range, the Y-axis is now dynamically set to ±10% of the actual data range visible in the chart (with a minimum 2 BPM margin), updated on every new HR data point.

2026-02-20 10:57 CET — **Rapid Change: Breathing Only setting & 5-reading confirmation guards**. Added a **Breathing Only** checkbox to the Rapid Change game settings. This setting is part of the configuration key, so the bar chart and past-config list only show records with the exact same settings (including `breathing_only`). Changing the checkbox immediately reloads the chart. All session-end threshold crossings now require **5 consecutive readings** beyond the target before the condition is confirmed — this applies to: reaching the end HR in one-way mode, reaching the peak HR in return mode, and returning to the start HR in return mode. The `breathing_only` flag is saved in each game entry in `rapid_change_data.json`.

2026-02-20 10:20 CET — **Recording Control Row & RR Recorder**. The Rec button, recording-type dropdown, `+ Add` button, and Settings button have been moved to a dedicated **recording control row** that sits above all device rows. A recording-type dropdown (default options: `chess`, `meditation`, `movie`) lets the user choose the context before starting a recording. For **all** recording types a new `RRRecorder` (in `src/recording/session_recorder.py`) captures a flat list of every RR interval and saves it as `{unix_epoch_seconds}.json` in `/home/twain/Projects/hrvm/recordings/` with fields `type`, `started_at`, and `rr_values`. For the **chess** type the existing full `SessionRecorder` (HR + RR + RMSSD/SDNN windows) is also run in parallel, saving to `~/Projects/chess-coach/data/hr_sessions/`. The `+ Add` button opens a small modal popup where the user can type a new recording-type label; it is immediately added to the dropdown. The Settings button was moved from the H10 row to the recording row.

2026-02-19 16:30 CET — Added **Unified HRV Section** to the GRAPHS area. HRV charts (Tachogram, Poincaré, RMSSD, SDNN, Coherence) are now device-agnostic: they use the Polar H10 as the primary source and fall back to the Polar Verity Sense (PPI stream) when the H10 is not connected and the PVS is streaming HR. The HRV section is shown/hidden automatically based on which HR source is active. All HRV charts now use a ~60-second scrolling window (data builds up then old data scrolls out). The top-bar RMSSD/SDNN metrics and the JSON Rec button also use the PVS as a fallback source. `pvs_bar.py` gained an `on_hr_streaming_changed` callback hook used by `UIManager` to track PVS HR streaming state.

2026-02-19 14:06 CET — Added **Resonance Breathing App** to the APPS section. Removed the breathing circle animation and pacer settings from the main top area. The new app provides manual timing inputs (inhale/hold/exhale/hold), a Start/Stop button, an animated breathing circle (PacerEngine), and a session-history bar chart showing resonance scores per session. Sessions are persisted to a new `breathing_sessions` table in the SQLite database.
