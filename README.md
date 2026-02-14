# Polar Flow-Sync Real-Time HRV Dashboard

**Last Updated:** 2026-02-14

A Python-based, ultra-low-latency desktop application for real-time Heart Rate Variability (HRV) monitoring using the Polar H10 Heart Rate Sensor.

---

## 🎯 Project Overview

This application interfaces with the Polar H10 via Bluetooth Low Energy (BLE) to capture raw ECG data at 130Hz, processes it in real-time using the Pan-Tompkins algorithm, and visualizes HRV metrics with <150ms end-to-end latency.

### Key Features

- **Real-Time ECG Visualization:** Strip chart displaying 2 seconds of ECG data
- **HRV Metrics:** RMSSD and SDNN calculated in real-time
- **Resonance Frequency Assessment:** Guided breathing protocol to find optimal breathing rate
- **Visual Pacer:** Customizable breathing pacer (Sine, Triangle, Square waves)
- **Multi-Process Architecture:** Separate processes for BLE, signal processing, and GUI
- **User Management:** SQLite-based user profiles and session tracking
- **Configuration Presets:** Save and load processing parameters
- **Artifact Rejection:** Median Absolute Deviation (MAD) filtering
- **Auto-Reconnect:** Robust BLE connection management
- **Mock Mode:** Simulate device data for testing without hardware

---

## 🏗️ Architecture

The system uses a **three-process architecture** for optimal performance:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Process 1     │────▶│   Process 2     │────▶│   Process 3     │
│  BLE Ingestion  │     │Signal Processing│     │  GUI/Database   │
│   (Asyncio)     │     │  (Numba/NumPy)  │     │  (Dear PyGui)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        ▲                        ▲                        │
        │                        │                        │
        └────────────────────────┴────────────────────────┘
                    Control Pipes (Bidirectional)
```

**See [`plans/system_architecture.md`](plans/system_architecture.md) for complete design documentation.**

---

## 📋 Requirements

### Hardware
- **Polar H10 Heart Rate Sensor** with chest strap
- Bluetooth 4.0+ adapter
- Multi-core CPU (recommended: 4+ cores)
- 200 MB RAM minimum

### Software
- Python 3.10+
- Linux, macOS, or Windows
- Bluetooth drivers installed

---

## 🚀 Quick Start

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd hrvm

# Install dependencies using pip
pip install -r requirements.txt
```

### Running the Application

```bash
# Run the application (requires Polar H10)
python3 src/main.py

# Run in Mock Mode (no device required, for testing)
python3 src/main.py --mock

# Run in Mock Mode with auto-connect (useful for headless/testing)
python3 -u src/main.py --mock --auto-connect
```

**Note:** Use the `-u` flag for unbuffered output when running in headless environments or when you need real-time log output.

### First-Time Setup

1. **Power on your Polar H10** and ensure it's in pairing mode (wear it!).
2. **Launch the application**.
3. **Create a user profile** in the User Management window (click "New User").
4. **Select the user** from the dropdown.
5. **Click "Connect"** to scan for and connect to your Polar H10.
6. **Start a session** to begin recording HRV data.

---

## 📊 Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| End-to-End Latency | <150ms | ✅ Achieved |
| GUI Frame Rate | 60 FPS | ✅ Achieved |
| Memory Usage | <200 MB | ✅ Achieved |
| CPU Usage (per core) | <50% | ✅ Achieved |
| BLE Reconnect Time | <5s | ✅ Achieved |

---

## 🗂️ Project Structure

```
hrvm/
├── src/
│   ├── main.py                    # Entry point
│   ├── ble/                       # BLE ingestion (Process 1)
│   │   ├── ble_manager.py
│   │   └── ring_buffer.py
│   ├── processing/                # Signal processing (Process 2)
│   │   ├── signal_processor.py
│   │   └── math_utils.py
│   ├── gui/                       # GUI/rendering (Process 3)
│   │   └── ui_manager.py
│   ├── database/                  # Data persistence
│   │   └── db_manager.py
│   └── utils/                     # Shared utilities
│       └── ipc.py
├── tests/                         # Unit and integration tests
├── plans/                         # Architecture documentation
├── requirements.txt               # Dependencies
└── README.md                      # This file
```

---

## 🔧 Configuration & Usage

### Biofeedback Controls
- **Pacer Settings:** Adjust the breathing pacer rate (BPM) and waveform shape (Sine, Triangle, Square) in the left panel.
- **Resonance Assessment:** Click "Start Assessment" to begin a guided protocol that steps through breathing rates (6.5 -> 4.5 BPM) to identify your resonance frequency.

### Signal Processing Settings
You can adjust processing parameters in real-time via the GUI "Settings" panel:

- **Window Size:** Duration of data used for HRV calculation (default: 60s)
- **Artifact Threshold:** Sensitivity for rejecting noise (default: 3.0 MAD)
- **Filter Cutoffs:** Bandpass filter range (default: 5.0 - 15.0 Hz)

You can save these settings as **Presets** for different users or scenarios.

---

## 📚 Documentation

- **[System Architecture](plans/system_architecture.md)** - Complete design document

---

## 🐛 Troubleshooting

### Mock Mode Shows No Data Flow

**Issue:** Running `python3 src/main.py --mock` results in a static GUI with no data updates.

**Root Cause:** The GUI requires manual interaction (clicking "Connect" button) to start data flow, which isn't possible in headless environments.

**Solution:** Use the `--auto-connect` flag to automatically initiate the connection:

```bash
python3 -u src/main.py --mock --auto-connect
```

**Verification:** You should see log messages indicating:
- `[DEBUG] Auto-connect enabled, sending connect command...`
- `[MOCK] Starting mock data generation...`
- `[DEBUG] First ProcessedData received! HR=X.X`
- `[DEBUG] First ECG plot update with non-zero data`

### GUI Not Displaying (Headless Environment)

**Issue:** DearPyGUI requires a display server (X11/Wayland) to render the GUI window.

**Solution:** For headless testing, the data pipeline still works (as verified by logs), but the visual GUI won't display. Consider:
- Using X11 forwarding: `ssh -X user@host`
- Running on a machine with a display
- Using VNC/remote desktop for GUI access

### Output Buffering Issues

**Issue:** Log messages appear delayed or not at all.

**Solution:** Use Python's unbuffered output mode with the `-u` flag:

```bash
python3 -u src/main.py --mock --auto-connect
```

---

## 🧪 Testing

```bash
# Run all tests
pytest
```

---

## 📝 License

This project is licensed under the MIT License.

---

## 🙏 Acknowledgments

- **Polar Electro** for the H10 sensor and PMD protocol documentation
- **Pan & Tompkins** for the QRS detection algorithm
- **Dear PyGui** community for the excellent GUI framework
- **Bleak** developers for cross-platform BLE support
