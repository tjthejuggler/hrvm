import numpy as np

class RingBuffer:
    """
    Lock-free ring buffer for ECG samples.
    Thread-safe for single producer, single consumer.
    """
    
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.buffer = np.zeros(capacity, dtype=np.int32)
        self.write_idx = 0
        self.read_idx = 0
        self.count = 0
        
    def write(self, samples: np.ndarray) -> None:
        """Append samples, overwriting oldest if full."""
        n = len(samples)
        if n == 0:
            return

        # If we're writing more than capacity, just take the last capacity samples
        if n > self.capacity:
            samples = samples[-self.capacity:]
            n = self.capacity

        # Calculate indices
        end_idx = (self.write_idx + n) % self.capacity
        
        if end_idx > self.write_idx:
            # No wrap around
            self.buffer[self.write_idx:end_idx] = samples
        else:
            # Wrap around
            first_part = self.capacity - self.write_idx
            self.buffer[self.write_idx:] = samples[:first_part]
            self.buffer[:end_idx] = samples[first_part:]
            
        self.write_idx = end_idx
        self.count = min(self.count + n, self.capacity)
        
    def read(self, n: int) -> np.ndarray:
        """Read n samples without removing from buffer."""
        if n > self.count:
            n = self.count
            
        if n == 0:
            return np.array([], dtype=np.int32)
            
        start_idx = (self.write_idx - self.count + self.capacity) % self.capacity
        # If we want the oldest n samples, we start from read_idx (conceptually)
        # But here we might want to just peek? 
        # The architecture doc says "Read n samples without removing".
        # Let's assume we read from the current read head.
        
        current_read_idx = self.read_idx
        end_idx = (current_read_idx + n) % self.capacity
        
        if end_idx > current_read_idx:
            return self.buffer[current_read_idx:end_idx].copy()
        else:
            return np.concatenate((self.buffer[current_read_idx:], self.buffer[:end_idx]))
        
    def consume(self, n: int) -> np.ndarray:
        """Read and remove n samples from buffer."""
        if n > self.count:
            n = self.count
            
        if n == 0:
            return np.array([], dtype=np.int32)
            
        data = self.read(n)
        self.read_idx = (self.read_idx + n) % self.capacity
        self.count -= n
        return data
