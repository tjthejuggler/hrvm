# HRVM ZeroMQ Sensor Stream API

**Last updated: 2026-02-27**

This document is the complete reference for subscribing to live sensor data published by the HRVM application over ZeroMQ. Any program — in any language — can receive real-time HR, ACC, and ECG data from the connected Polar device while HRVM is running.

---

## Overview

HRVM binds a **ZeroMQ PUB socket** when it starts. It publishes sensor data as soon as the Polar H10 is connected and streaming. Your program connects a **ZeroMQ SUB socket** and subscribes to the topics it needs.

- **Transport**: TCP, localhost only (by default)
- **Endpoint**: `tcp://127.0.0.1:5555`
- **Pattern**: PUB/SUB (fire-and-forget; no acknowledgement)
- **Message format**: Two-part multipart message — `[topic_bytes, json_bytes]`
- **Encoding**: UTF-8 JSON

---

## Topics

| Topic string | Sensor data | Source device | Rate |
|---|---|---|---|
| `hr` | Heart rate + RR intervals | Polar H10 HR characteristic | ~1 Hz (1 per heartbeat) |
| `acc` | 3-axis accelerometer | Polar H10 PMD service | ~25 Hz (batched) |
| `ecg` | Raw ECG samples | Polar H10 PMD service | ~130 Hz (batched) |

Subscribe to one, two, or all three topics independently.

---

## Message Format

Every message is a **ZeroMQ multipart message with exactly 2 frames**:

```
Frame 0: topic  (bytes, e.g. b"hr")
Frame 1: payload (bytes, UTF-8 JSON)
```

### `hr` payload

```json
{
  "timestamp": 1740672061.432,
  "heart_rate_bpm": 72,
  "rr_intervals_ms": [834.0, 841.5],
  "sequence_number": 142
}
```

| Field | Type | Description |
|---|---|---|
| `timestamp` | float | Unix epoch seconds (wall clock at time of BLE notification) |
| `heart_rate_bpm` | int | Instantaneous heart rate in BPM |
| `rr_intervals_ms` | list[float] | RR intervals in milliseconds. May contain 0, 1, or 2 values per message depending on the heartbeat timing. |
| `sequence_number` | int | Monotonically increasing counter. Gaps indicate dropped packets. |

---

### `acc` payload

```json
{
  "timestamp": 1740672061.480,
  "sample_rate": 25,
  "samples": [
    [12, -8, 1003],
    [14, -7, 1001],
    [11, -9, 998]
  ],
  "sequence_number": 87
}
```

| Field | Type | Description |
|---|---|---|
| `timestamp` | float | Unix epoch seconds of the BLE notification arrival |
| `sample_rate` | int | Sample rate in Hz (always 25 for Polar H10 default config) |
| `samples` | list[list[int]] | Each inner list is `[x, y, z]` in **milli-g (mg)**. Typically 25 samples per batch (1 second worth). |
| `sequence_number` | int | Monotonically increasing counter. |

**Axis orientation**: x, y, z as reported by the Polar H10 sensor. Gravity at rest ≈ 1000 mg on the dominant axis.

---

### `ecg` payload

```json
{
  "timestamp": 1740672061.510,
  "sample_rate": 130,
  "samples": [142, 138, 145, 3021, 2987, 201, 155, ...],
  "sequence_number": 203
}
```

| Field | Type | Description |
|---|---|---|
| `timestamp` | float | Unix epoch seconds of the BLE notification arrival |
| `sample_rate` | int | Sample rate in Hz (always 130 for Polar H10) |
| `samples` | list[int] | Raw ECG values in **microvolts (µV)**. Typically 73 samples per batch (~0.56 seconds). Values are signed 24-bit integers sign-extended from the Polar 14-bit ADC. |
| `sequence_number` | int | Monotonically increasing counter. |

---

## Subscriber Examples

### Python (pyzmq)

```python
import zmq
import json

ctx = zmq.Context()
sub = ctx.socket(zmq.SUB)
sub.connect("tcp://127.0.0.1:5555")

# Subscribe to specific topics (empty string = all topics)
sub.setsockopt(zmq.SUBSCRIBE, b"hr")
sub.setsockopt(zmq.SUBSCRIBE, b"acc")
# sub.setsockopt(zmq.SUBSCRIBE, b"")  # all topics

while True:
    topic, payload = sub.recv_multipart()
    data = json.loads(payload)
    print(f"[{topic.decode()}] {data}")
```

### Python — non-blocking poll (for use inside a GUI loop)

```python
import zmq
import json

ctx = zmq.Context()
sub = ctx.socket(zmq.SUB)
sub.connect("tcp://127.0.0.1:5555")
sub.setsockopt(zmq.SUBSCRIBE, b"hr")

# In your update loop (e.g. every 20ms):
def poll_sensor():
    while sub.poll(timeout=0):  # non-blocking
        topic, payload = sub.recv_multipart()
        data = json.loads(payload)
        handle(data)
```

### Rust (zmq crate)

```rust
let ctx = zmq::Context::new();
let sub = ctx.socket(zmq::SUB).unwrap();
sub.connect("tcp://127.0.0.1:5555").unwrap();
sub.set_subscribe(b"hr").unwrap();

loop {
    let topic = sub.recv_string(0).unwrap().unwrap();
    let payload = sub.recv_string(0).unwrap().unwrap();
    let data: serde_json::Value = serde_json::from_str(&payload).unwrap();
    println!("{}: {:?}", topic, data);
}
```

### Node.js (zeromq npm package)

```js
const zmq = require("zeromq");

async function run() {
  const sub = new zmq.Subscriber();
  sub.connect("tcp://127.0.0.1:5555");
  sub.subscribe("hr", "acc");

  for await (const [topic, payload] of sub) {
    const data = JSON.parse(payload.toString());
    console.log(topic.toString(), data);
  }
}
run();
```

---

## Important Behaviour Notes

1. **HRVM must be running and the Polar H10 must be connected** before any data is published. The socket is bound at startup, but messages only flow once the device is streaming.

2. **PUB/SUB is lossy by design.** If your subscriber is slow or not yet connected when a message is sent, that message is dropped silently. There is no replay or buffering.

3. **Sequence numbers** let you detect dropped packets. If `sequence_number` jumps by more than 1, packets were lost (either in BLE or in the ZeroMQ send queue).

4. **The endpoint is localhost-only** by default (`127.0.0.1`). To expose it on a network interface, change `DEFAULT_ENDPOINT` in [`src/ble/stream_publisher.py`](../src/ble/stream_publisher.py).

5. **Multiple subscribers** can connect simultaneously — ZeroMQ PUB broadcasts to all of them with no extra cost.

6. **Latency**: ZeroMQ localhost PUB/SUB adds approximately 0.1–0.3 ms overhead on top of the BLE delivery latency (~20–100 ms). This is negligible for all biofeedback use cases.

---

## Changing the Endpoint

Edit [`src/ble/stream_publisher.py`](../src/ble/stream_publisher.py), line:

```python
DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"
```

Examples:
- `"tcp://0.0.0.0:5555"` — listen on all interfaces (LAN accessible)
- `"tcp://127.0.0.1:6000"` — different port
- `"ipc:///tmp/hrvm.ipc"` — Unix domain socket (Linux only, slightly lower latency)
