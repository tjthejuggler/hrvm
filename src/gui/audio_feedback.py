import numpy as np
import sounddevice as sd
import threading
import time
import logging

logger = logging.getLogger(__name__)

class AudioFeedback:
    def __init__(self, sample_rate=44100):
        self.sample_rate = sample_rate
        self.running = False
        self.stream = None
        
        # Audio State
        self.current_freq = 220.0
        self.target_freq = 220.0
        self.phase = 0.0
        self.volume = 0.5
        
        # Mapping Configuration
        self.min_hr = 50.0
        self.max_hr = 100.0
        self.min_pitch = 220.0 # A3
        self.max_pitch = 440.0 # A4
        
        # Smoothing
        self.smoothing_factor = 0.1 # How fast to slide to target

    def start(self):
        if self.running:
            return
            
        self.running = True
        try:
            self.stream = sd.OutputStream(
                channels=1,
                samplerate=self.sample_rate,
                callback=self.audio_callback,
                blocksize=1024
            )
            self.stream.start()
            logger.info("Audio feedback started.")
        except Exception as e:
            logger.error(f"Failed to start audio stream: {e}")
            self.running = False

    def stop(self):
        if not self.running:
            return
            
        self.running = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        logger.info("Audio feedback stopped.")

    def update_hr(self, hr_value):
        """
        Update the target frequency based on heart rate.
        """
        if hr_value < 10: # Ignore noise/zeros
            return
            
        # Clamp HR
        hr_clamped = max(self.min_hr, min(self.max_hr, hr_value))
        
        # Map to Pitch
        # Formula: Min_Pitch + (HR - Min_HR) * (Range_Pitch / Range_HR)
        pitch_range = self.max_pitch - self.min_pitch
        hr_range = self.max_hr - self.min_hr
        
        new_freq = self.min_pitch + (hr_clamped - self.min_hr) * (pitch_range / hr_range)
        self.target_freq = new_freq

    def audio_callback(self, outdata, frames, time_info, status):
        if status:
            logger.warning(f"Audio status: {status}")
            
        # Generate sine wave buffer
        t = np.arange(frames) / self.sample_rate
        
        # Smooth frequency transition (Portamento)
        # We can't just jump frequency, or it clicks.
        # Ideally, we'd interpolate frequency across the buffer.
        # For simplicity, we'll just step towards the target each callback.
        
        # Calculate frequency step per sample? Too expensive.
        # Step per buffer:
        diff = self.target_freq - self.current_freq
        if abs(diff) > 0.1:
            self.current_freq += diff * self.smoothing_factor
        else:
            self.current_freq = self.target_freq
            
        # Generate phase increments
        # phase_increment = 2 * np.pi * self.current_freq / self.sample_rate
        # phases = self.phase + np.arange(frames) * phase_increment
        # outdata[:] = self.volume * np.sin(phases).reshape(-1, 1)
        # self.phase = (self.phase + frames * phase_increment) % (2 * np.pi)
        
        # Better: Continuous phase integration for changing frequency
        # But for fixed frequency per buffer, the above is fine.
        # If we want true glissando, we need to integrate frequency over time.
        
        # Let's stick to fixed frequency per buffer for performance, but smooth the transitions between buffers.
        
        phase_increment = 2 * np.pi * self.current_freq / self.sample_rate
        phases = self.phase + np.arange(frames) * phase_increment
        
        # Generate sine wave
        sine_wave = self.volume * np.sin(phases)
        
        # Apply fade in/out if starting/stopping? No, continuous stream.
        
        outdata[:] = sine_wave.reshape(-1, 1)
        
        # Update phase for next buffer
        self.phase = (self.phase + frames * phase_increment) % (2 * np.pi)

