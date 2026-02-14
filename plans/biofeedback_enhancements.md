# Polar H10 Biofeedback System - Enhancement Specification

**Document Version:** 2.0  
**Last Updated:** 2026-02-14T11:20:00Z  
**Status:** Design Specification

---

## Executive Summary

This document specifies enhancements to the existing Polar H10 HRV monitoring system to add biofeedback capabilities:

1. **Visual Breathing Pacer** - Configurable waveforms (sine/triangle) with adjustable rates
2. **Real-Time Coherence Metrics** - HRV-breathing synchronization measurement
3. **Resonance Frequency Detection** - Automated optimal breathing rate identification
4. **Full-Screen Biofeedback Interface** - Immersive training visualization

**Architecture Impact:** Minimal changes to existing 3-process design. New components added to Process 2 (Math) and Process 3 (GUI).

---

## Gap Analysis

### Current System (v1.0)
✅ BLE connection with Polar H10  
✅ Real-time ECG processing (Pan-Tompkins)  
✅ HRV metrics (RMSSD, SDNN)  
✅ Multi-process architecture (3 processes)  
✅ User management & session recording  
✅ Configuration presets  
✅ Real-time visualization  

### Required Additions
❌ Breathing pacer engine  
❌ Coherence calculation  
❌ Resonance frequency detection  
❌ Full-screen biofeedback mode  
❌ Enhanced database schema  

---

## 1. Pacer Engine Architecture

### 1.1 Overview
The **PacerEngine** runs in the GUI process (Process 3) at 60 FPS, generating visual breathing cues.

### 1.2 Mathematical Model

**Breathing Cycle Timing:**
```python
breathing_rate = 6.0  # breaths per minute
inhale_ratio = 0.4    # 40% inhale, 60% exhale
cycle_duration = 60.0 / breathing_rate
phase = (current_time % cycle_duration) / cycle_duration  # 0.0 to 1.0
```

**Waveform Functions:**

```python
# Sine Wave (smooth, natural)
def sine_waveform(phase: float, inhale_ratio: float) -> float:
    if phase < inhale_ratio:
        t = phase / inhale_ratio
        return 0.5 * (1.0 - np.cos(np.pi * t))
    else:
        t = (phase - inhale_ratio) / (1.0 - inhale_ratio)
        return 0.5 * (1.0 + np.cos(np.pi * t))

# Triangle Wave (linear, easier to follow)
def triangle_waveform(phase: float, inhale_ratio: float) -> float:
    if phase < inhale_ratio:
        return phase / inhale_ratio
    else:
        t = (phase - inhale_ratio) / (1.0 - inhale_ratio)
        return 1.0 - t
```

### 1.3 Visual Representations

**Expanding Circle (Primary):**
- Center screen position
- Radius: 50px (exhaled) to 200px (inhaled)
- Color gradient: Blue → Green
- Text overlay: "INHALE" / "EXHALE"

**Vertical Bar (Alternative):**
- Center screen, vertical orientation
- Height varies with breathing phase
- Color: Cyan with transparency

### 1.4 PacerEngine Class

```python
from dataclasses import dataclass
from enum import Enum
from collections import deque
import time
import numpy as np

class WaveformType(Enum):
    SINE = "sine"
    TRIANGLE = "triangle"

@dataclass
class PacerConfig:
    breathing_rate: float = 6.0      # breaths per minute
    inhale_ratio: float = 0.4        # fraction for inhale
    waveform_type: WaveformType = WaveformType.SINE
    visual_style: str = "circle"     # "circle" or "bar"

class PacerEngine:
    """
    Generates visual breathing cues at 60 FPS.
    Runs in GUI process.
    """
    
    def __init__(self, config: PacerConfig):
        self.config = config
        self.start_time = None
        self.is_running = False
        self.current_phase = 0.0
        self.current_value = 0.0
        
        # Buffer for coherence (60 seconds at 60 Hz)
        self.breathing_buffer = deque(maxlen=3600)
        self.breathing_timestamps = deque(maxlen=3600)
        
    def start(self):
        self.start_time = time.time()
        self.is_running = True
        
    def update(self, current_time: float) -> float:
        """Update pacer state. Returns breathing value (0.0 to 1.0)."""
        if not self.is_running:
            return self.current_value
            
        elapsed = current_time - self.start_time
        cycle_duration = 60.0 / self.config.breathing_rate
        self.current_phase = (elapsed % cycle_duration) / cycle_duration
        
        # Calculate waveform
        if self.config.waveform_type == WaveformType.SINE:
            self.current_value = self._sine_waveform(
                self.current_phase, self.config.inhale_ratio
            )
        else:
            self.current_value = self._triangle_waveform(
                self.current_phase, self.config.inhale_ratio
            )
            
        # Store for coherence
        self.breathing_buffer.append(self.current_value)
        self.breathing_timestamps.append(current_time)
        
        return self.current_value
        
    def render(self, draw_list):
        """Render visual pacer to DearPyGUI."""
        if self.config.visual_style == "circle":
            self._render_circle(draw_list)
        else:
            self._render_bar(draw_list)
            
    def get_breathing_signal(self, duration: float = 60.0):
        """Get recent breathing signal for coherence calculation."""
        current_time = time.time()
        cutoff_time = current_time - duration
        
        timestamps = np.array(self.breathing_timestamps)
        values = np.array(self.breathing_buffer)
        mask = timestamps >= cutoff_time
        
        return timestamps[mask], values[mask]
```

---

## 2. Coherence Calculation System

### 2.1 Theory
**Coherence** measures synchronization between HRV and breathing signals. High coherence indicates respiratory sinus arrhythmia (RSA) - heart rate synchronized with breathing.

### 2.2 Calculation Method

**Cross-Correlation Approach:**
```python
def calculate_coherence(hrv_signal: np.ndarray, 
                       breathing_signal: np.ndarray) -> float:
    """
    Calculate coherence using normalized cross-correlation.
    Returns: 0.0 (no sync) to 1.0 (perfect sync)
    """
    # Normalize signals
    hrv_norm = (hrv_signal - np.mean(hrv_signal)) / (np.std(hrv_signal) + 1e-8)
    breath_norm = (breathing_signal - np.mean(breathing_signal)) / (np.std(breathing_signal) + 1e-8)
    
    # Cross-correlation at zero lag
    correlation = np.correlate(hrv_norm, breath_norm, mode='valid')[0] / len(hrv_norm)
    
    # Map to 0-1 range
    coherence = (correlation + 1.0) / 2.0
    return np.clip(coherence, 0.0, 1.0)
```

### 2.3 CoherenceEngine Class

```python
class CoherenceEngine:
    """
    Calculates real-time coherence between HRV and breathing.
    Runs in Signal Processing process (Process 2).
    """
    
    def __init__(self, window_size: int = 60):
        self.window_size = window_size
        
        # HRV buffer (1 Hz)
        self.hrv_buffer = deque(maxlen=window_size)
        self.hrv_timestamps = deque(maxlen=window_size)
        
        # Breathing buffer (resampled to 1 Hz)
        self.breathing_buffer = deque(maxlen=window_size)
        self.breathing_timestamps = deque(maxlen=window_size)
        
        # Coherence history
        self.coherence_history = deque(maxlen=600)  # 10 min
        
    def update_hrv(self, timestamp: float, heart_rate: float):
        """Add HRV data point."""
        self.hrv_buffer.append(heart_rate)
        self.hrv_timestamps.append(timestamp)
        
    def update_breathing(self, timestamps: np.ndarray, values: np.ndarray):
        """Add breathing signal from pacer (resampled to 1 Hz)."""
        if len(timestamps) < 2:
            return
            
        # Resample to 1 Hz
        start_time = timestamps[0]
        end_time = timestamps[-1]
        duration = end_time - start_time
        
        if duration < 1.0:
            return
            
        num_samples = int(duration) + 1
        time_grid = np.linspace(start_time, end_time, num_samples)
        resampled_values = np.interp(time_grid, timestamps, values)
        
        for t, v in zip(time_grid, resampled_values):
            self.breathing_buffer.append(v)
            self.breathing_timestamps.append(t)
            
    def calculate_coherence(self) -> Optional[float]:
        """Calculate current coherence score."""
        if len(self.hrv_buffer) < 30 or len(self.breathing_buffer) < 30:
            return None
            
        # Align signals by timestamp
        hrv_times = np.array(self.hrv_timestamps)
        breath_times = np.array(self.breathing_timestamps)
        
        start_time = max(hrv_times[0], breath_times[0])
        end_time = min(hrv_times[-1], breath_times[-1])
        
        if end_time - start_time < 30:
            return None
            
        # Common time grid (1 Hz)
        common_times = np.arange(start_time, end_time, 1.0)
        
        # Interpolate both signals
        hrv_aligned = np.interp(common_times, hrv_times, np.array(self.hrv_buffer))
        breath_aligned = np.interp(common_times, breath_times, np.array(self.breathing_buffer))
        
        # Calculate coherence
        coherence = self._calculate_crosscorr(hrv_aligned, breath_aligned)
        self.coherence_history.append(coherence)
        
        return coherence
        
    def _calculate_crosscorr(self, s1: np.ndarray, s2: np.ndarray) -> float:
        """Normalized cross-correlation."""
        s1_norm = (s1 - np.mean(s1)) / (np.std(s1) + 1e-8)
        s2_norm = (s2 - np.mean(s2)) / (np.std(s2) + 1e-8)
        corr = np.correlate(s1_norm, s2_norm, mode='valid')[0] / len(s1)
        return np.clip((corr + 1.0) / 2.0, 0.0, 1.0)
```

---

## 3. Resonance Frequency Detection

### 3.1 Concept
**Resonance Frequency (RF)** is the breathing rate that produces maximum HRV coherence. Typically 4.5-7.0 breaths/minute for adults.

### 3.2 Detection Algorithm

**Sweep Method:**
1. Test breathing rates from 4.0 to 7.0 BPM (0.5 BPM increments)
2. Measure coherence at each rate for 2 minutes
3. Identify rate with highest average coherence
4. Recommend as user's resonance frequency

```python
class ResonanceFrequencyDetector:
    """
    Automated resonance frequency detection.
    Runs in Signal Processing process (Process 2).
    """
    
    def __init__(self):
        self.test_rates = np.arange(4.0, 7.5, 0.5)  # [4.0, 4.5, ..., 7.0]
        self.current_rate_idx = 0
        self.test_duration = 120.0  # 2 minutes per rate
        self.test_start_time = None
        self.is_testing = False
        
        # Results
        self.coherence_scores = {}  # {rate: [coherence_values]}
        self.resonance_frequency = None
        
    def start_assessment(self):
        """Begin RF assessment protocol."""
        self.current_rate_idx = 0
        self.test_start_time = time.time()
        self.is_testing = True
        self.coherence_scores = {rate: [] for rate in self.test_rates}
        
    def get_current_rate(self) -> float:
        """Get current test breathing rate."""
        if not self.is_testing:
            return 6.0  # default
        return self.test_rates[self.current_rate_idx]
        
    def update(self, coherence: float) -> bool:
        """
        Update with new coherence measurement.
        Returns True if assessment complete.
        """
        if not self.is_testing:
            return False
            
        current_rate = self.test_rates[self.current_rate_idx]
        self.coherence_scores[current_rate].append(coherence)
        
        # Check if current rate test is complete
        elapsed = time.time() - self.test_start_time
        if elapsed >= self.test_duration:
            # Move to next rate
            self.current_rate_idx += 1
            self.test_start_time = time.time()
            
            # Check if all rates tested
            if self.current_rate_idx >= len(self.test_rates):
                self._calculate_resonance_frequency()
                self.is_testing = False
                return True
                
        return False
        
    def _calculate_resonance_frequency(self):
        """Identify rate with highest average coherence."""
        avg_coherence = {}
        for rate, scores in self.coherence_scores.items():
            if len(scores) > 0:
                avg_coherence[rate] = np.mean(scores)
            else:
                avg_coherence[rate] = 0.0
                
        # Find maximum
        self.resonance_frequency = max(avg_coherence, key=avg_coherence.get)
        
    def get_results(self) -> Dict[str, Any]:
        """Get assessment results."""
        return {
            'resonance_frequency': self.resonance_frequency,
            'coherence_scores': self.coherence_scores,
            'test_rates': self.test_rates.tolist()
        }
```

---

## 4. Full-Screen GUI Design

### 4.1 Layout Specification

**Full-Screen Biofeedback Mode:**
```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│                    [Pacer Visual]                       │
│                   (Expanding Circle)                    │
│                                                         │
│                                                         │
│  ┌─────────────────────────────────────────────────┐  │
│  │  Coherence: ████████░░ 0.82                     │  │
│  └─────────────────────────────────────────────────┘  │
│                                                         │
│  HR: 65 BPM    RMSSD: 42 ms    SDNN: 38 ms           │
│                                                         │
│  ┌─────────────────────────────────────────────────┐  │
│  │  [Coherence History Graph]                       │  │
│  └─────────────────────────────────────────────────┘  │
│                                                         │
│  [ESC: Exit]  [SPACE: Pause]  [R: Reset]              │
└─────────────────────────────────────────────────────────┘
```

### 4.2 UI Components

**Primary Elements:**
1. **Pacer Visual** - Center, large (400x400px)
2. **Coherence Bar** - Below pacer, prominent
3. **Metrics Display** - Small, non-distracting
4. **Coherence Graph** - Bottom third, trend visualization
5. **Keyboard Shortcuts** - Minimal text overlay

**Color Scheme:**
- Background: Dark gray (#1a1a1a)
- Pacer: Blue → Green gradient
- Coherence bar: Green (high) → Yellow (medium) → Red (low)
- Text: White with 80% opacity

### 4.3 Implementation

```python
class BiofeedbackFullScreenUI:
    """Full-screen biofeedback interface."""
    
    def __init__(self, ui_manager: UIManager):
        self.ui_manager = ui_manager
        self.is_fullscreen = False
        self.pacer_engine = None
        
    def enter_fullscreen(self):
        """Switch to full-screen biofeedback mode."""
        # Hide normal UI elements
        dpg.hide_item("Primary Window")
        
        # Create full-screen window
        viewport_width = dpg.get_viewport_width()
        viewport_height = dpg.get_viewport_height()
        
        with dpg.window(
            tag="fullscreen_window",
            width=viewport_width,
            height=viewport_height,
            pos=(0, 0),
            no_title_bar=True,
            no_resize=True,
            no_move=True,
            no_collapse=True
        ):
            # Pacer visual (center)
            with dpg.drawlist(
                width=viewport_width,
                height=viewport_height * 0.6,
                tag="pacer_drawlist"
            ):
                pass  # Pacer renders here
                
            # Coherence bar
            dpg.add_progress_bar(
                tag="coherence_bar_fs",
                default_value=0.0,
                width=viewport_width * 0.8,
                height=40
            )
            
            # Metrics
            with dpg.group(horizontal=True):
                dpg.add_text("HR: ", color=(150, 150, 150))
                dpg.add_text("0 BPM", tag="hr_fs")
                dpg.add_spacer(width=50)
                dpg.add_text("RMSSD: ", color=(150, 150, 150))
                dpg.add_text("0 ms", tag="rmssd_fs")
                dpg.add_spacer(width=50)
                dpg.add_text("SDNN: ", color=(150, 150, 150))
                dpg.add_text("0 ms", tag="sdnn_fs")
                
            # Coherence graph
            with dpg.plot(
                label="",
                height=viewport_height * 0.25,
                width=viewport_width * 0.9,
                no_title=True
            ):
                dpg.add_plot_axis(dpg.mvXAxis, label="", tag="coh_x_fs", no_tick_labels=True)
                dpg.add_plot_axis(dpg.mvYAxis, label="Coherence", tag="coh_y_fs")
                dpg.add_line_series([], [], parent="coh_y_fs", tag="coh_series_fs")
                
            # Instructions
            dpg.add_text(
                "[ESC] Exit  [SPACE] Pause  [R] Reset",
                color=(100, 100, 100)
            )
            
        # Register keyboard handlers
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=self.exit_fullscreen)
            dpg.add_key_press_handler(dpg.mvKey_Spacebar, callback=self.toggle_pause)
            dpg.add_key_press_handler(dpg.mvKey_R, callback=self.reset_pacer)
            
        self.is_fullscreen = True
        
    def exit_fullscreen(self):
        """Return to normal UI."""
        dpg.delete_item("fullscreen_window")
        dpg.show_item("Primary Window")
        self.is_fullscreen = False
```

---

## 5. Enhanced Database Schema

### 5.1 New Tables

```sql
-- Pacer Settings Table
CREATE TABLE IF NOT EXISTS pacer_settings (
    setting_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    breathing_rate REAL NOT NULL,
    inhale_ratio REAL NOT NULL,
    waveform_type TEXT NOT NULL,
    visual_style TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Coherence Data Table
CREATE TABLE IF NOT EXISTS coherence_data (
    coherence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    coherence_score REAL NOT NULL,
    breathing_rate REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX idx_coherence_session ON coherence_data(session_id);
CREATE INDEX idx_coherence_timestamp ON coherence_data(timestamp);

-- Resonance Frequency Assessments Table
CREATE TABLE IF NOT EXISTS rf_assessments (
    assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    assessment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resonance_frequency REAL NOT NULL,
    test_results TEXT NOT NULL,  -- JSON: {rate: avg_coherence}
    notes TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE INDEX idx_rf_user ON rf_assessments(user_id);
```

### 5.2 Updated Sessions Table

```sql
-- Add columns to existing sessions table
ALTER TABLE sessions ADD COLUMN avg_coherence REAL;
ALTER TABLE sessions ADD COLUMN breathing_rate REAL;
```

---

## 6. Updated IPC Protocol

### 6.1 New Message Types

```python
@dataclass
class BreathingData:
    """Breathing signal from pacer to math process."""
    timestamps: np.ndarray
    values: np.ndarray
    breathing_rate: float

@dataclass
class CoherenceData:
    """Coherence metrics from math to GUI process."""
    timestamp: float
    coherence_score: float
    breathing_rate: float
    hrv_mean: float

@dataclass
class PacerCommand:
    """Control commands for pacer."""
    command: str  # "start", "stop", "reset", "set_rate"
    params: Dict[str, Any]

@dataclass
class RFAssessmentCommand:
    """Commands for RF detection."""
    command: str  # "start_assessment", "stop_assessment", "get_results"
    params: Dict[str, Any]
```

### 6.2 Enhanced ProcessedData

```python
@dataclass
class ProcessedData:
    """Extended with coherence data."""
    timestamp: float
    ecg_window: np.ndarray
    rr_intervals: List[float]
    heart_rate: float
    hrv_rmssd: float
    hrv_sdnn: float
    quality_score: float
    # NEW FIELDS:
    coherence_score: Optional[float] = None
    breathing_rate: Optional[float] = None
```

---

## 7. Integration Points

### 7.1 Process 2 (Math) Changes

**SignalProcessor class additions:**
```python
class SignalProcessor:
    def __init__(self, ...):
        # ... existing code ...
        self.coherence_engine = CoherenceEngine(window_size=60)
        self.rf_detector = ResonanceFrequencyDetector()
        
    def process_batch(self, batch: ECGBatch) -> Optional[ProcessedData]:
        # ... existing processing ...
        
        # NEW: Update coherence engine
        if len(self.rr_intervals) > 0:
            self.coherence_engine.update_hrv(
                batch.timestamp_unix,
                heart_rate
            )
            
        # NEW: Calculate coherence
        coherence_score = self.coherence_engine.calculate_coherence()
        
        # NEW: Update RF detector if running
        if self.rf_detector.is_testing and coherence_score:
            assessment_complete = self.rf_detector.update(coherence_score)
            if assessment_complete:
                # Send results to GUI
                pass
                
        return ProcessedData(
            # ... existing fields ...
            coherence_score=coherence_score,
            breathing_rate=self.current_breathing_rate
        )
        
    def handle_breathing_update(self, breathing_data: BreathingData):
        """Receive breathing signal from GUI."""
        self.coherence_engine.update_breathing(
            breathing_data.timestamps,
            breathing_data.values
        )
        self.current_breathing_rate = breathing_data.breathing_rate
```

### 7.2 Process 3 (GUI) Changes

**UIManager class additions:**
```python
class UIManager:
    def __init__(self, ...):
        # ... existing code ...
        self.pacer_engine = PacerEngine(PacerConfig())
        self.fullscreen_ui = BiofeedbackFullScreenUI(self)
        self.coherence_history = deque(maxlen=600)
        
    def run(self):
        # ... existing setup ...
        
        while dpg.is_dearpygui_running():
            current_time = time.time()
            
            # Update pacer
            if self.pacer_engine.is_running:
                breathing_value = self.pacer_engine.update(current_time)
                
                # Send breathing signal to math process every second
                if int(current_time) % 1 == 0:
                    timestamps, values = self.pacer_engine.get_breathing_signal(60.0)
                    if len(timestamps) > 0:
                        breathing_data = BreathingData(
                            timestamps=timestamps,
                            values=values,
                            breathing_rate=self.pacer_engine.config.breathing_rate
                        )
                        self.math_control_pipe.send(('BREATHING_UPDATE', breathing_data))
                        
            # Render pacer
            if self.fullscreen_ui.is_fullscreen:
                self.pacer_engine.render(dpg.get_item_configuration("pacer_drawlist"))
                
            # ... existing polling ...
            self.poll_data_pipe()
            self.poll_ble_status()
            self.update_ecg_plot()
            
            dpg.render_dearpygui_frame()
```

---

## 8. Implementation Roadmap

### Phase 1: Pacer Engine (Week 1)
- [ ] Implement [`PacerEngine`](plans/biofeedback_enhancements.md:89) class
- [ ] Add waveform functions (sine, triangle)
- [ ] Create circle and bar visualizations
- [ ] Add pacer controls to GUI
- [ ] Test at 60 FPS

### Phase 2: Coherence Calculation (Week 2)
- [ ] Implement [`CoherenceEngine`](plans/biofeedback_enhancements.md:189) class
- [ ] Add breathing signal IPC
- [ ] Integrate with SignalProcessor
- [ ] Add coherence display to GUI
- [ ] Validate coherence algorithm

### Phase 3: Full-Screen UI (Week 3)
- [ ] Design full-screen layout
- [ ] Implement [`BiofeedbackFullScreenUI`](plans/biofeedback_enhancements.md:389) class
- [ ] Add keyboard shortcuts
- [ ] Create coherence history graph
- [ ] Polish visual design

### Phase 4: Resonance Frequency Detection (Week 4)
- [ ] Implement [`ResonanceFrequencyDetector`](plans/biofeedback_enhancements.md:289) class
- [ ] Create RF assessment UI
- [ ] Add automated rate sweeping
- [ ] Store results in database
- [ ] Generate RF report

### Phase 5: Database & Integration (Week 5)
- [ ] Add new database tables
- [ ] Implement data logging
- [ ] Create session reports
- [ ] Add preset management
- [ ] End-to-end testing

### Phase 6: Testing & Optimization (Week 6)
- [ ] Performance profiling
- [ ] Coherence algorithm validation
- [ ] User acceptance testing
- [ ] Documentation
- [ ] Bug fixes

---

## 9. Performance Considerations

### 9.1 Latency Budget (Maintained)
- BLE → Math: <10ms
- Math processing: <50ms
- Math → GUI: <10ms
- GUI render: <16ms (60 FPS)
- **Total: <86ms** (well under 150ms target)

### 9.2 New Computational Costs
- **Pacer update:** ~0.1ms (negligible)
- **Coherence calculation:** ~5ms (every second)
- **RF detection:** ~2ms (during assessment only)

### 9.3 Memory Impact
- Pacer buffer: 3600 samples × 8 bytes = 28.8 KB
- Coherence buffers: 2 × 60 samples × 8 bytes = 960 bytes
- **Total new memory:** ~30 KB (negligible)

---

## 10. Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Coherence algorithm accuracy | Medium | High | Validate against published research |
| Pacer rendering performance | Low | Medium | Profile at 60 FPS, optimize if needed |
| IPC overhead for breathing signal | Low | Low | Send updates at 1 Hz, not 60 Hz |
| Full-screen mode compatibility | Low | Low | Test on multiple displays |
| RF detection duration too long | Medium | Low | Allow manual rate selection |

---

## 11. Success Criteria

### Functional Requirements
✓ Pacer runs smoothly at 60 FPS  
✓ Coherence calculated in real-time (<1s latency)  
✓ Full-screen mode is immersive and distraction-free  
✓ RF detection completes in <15 minutes  
✓ All data persisted to database  

### Performance Requirements
✓ End-to-end latency remains <150ms  
✓ GUI maintains 60 FPS with pacer active  
✓ Memory usage increase <50 MB  
✓ CPU usage increase <10% per core  

### User Experience Requirements
✓ Pacer is easy to follow  
✓ Coherence feedback is intuitive  
✓ Full-screen mode is calming  
✓ RF assessment is automated  
✓ Results are actionable  

---

## Appendix A: References

1. **Lehrer, P. M., & Gevirtz, R. (2014).** Heart rate variability biofeedback: how and why does it work? *Frontiers in Psychology*, 5, 756.

2. **Vaschillo, E., et al. (2002).** Heart rate variability biofeedback as a method for assessing baroreflex function.