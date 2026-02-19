"""Polar Verity Sense PMD data packet parser.

Parses raw BLE notification data from the Polar Measurement Data (PMD)
service on the Polar Verity Sense. Supports both raw frames (0x00) and
delta-compressed frames (0x80).

Measurement types supported:
  - ACC  (0x02): 3-axis accelerometer, units: mg (milli-g)
  - PPI  (0x03): Pulse-to-Pulse Interval (HRV data)
  - GYR  (0x05): 3-axis gyroscope, units: dps (degrees per second)
  - MAG  (0x06): 3-axis magnetometer, units: Gauss/10
  - PPG  (0x15): Raw optical heart rate (PPG channels)
"""

import struct
import logging
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# PMD Measurement type constants
PMD_TYPE_ACC = 0x02
PMD_TYPE_PPI = 0x03
PMD_TYPE_GYR = 0x05
PMD_TYPE_MAG = 0x06
PMD_TYPE_SDK = 0x09
PMD_TYPE_PPG = 0x15

# Frame types
FRAME_TYPE_RAW = 0x00
FRAME_TYPE_DELTA = 0x80  # High bit set = delta frame; lower bits may encode delta size

# Setting type keys (for building start commands)
SETTING_SAMPLE_RATE = 0x00
SETTING_RESOLUTION = 0x01
SETTING_RANGE = 0x02
SETTING_CHANNELS = 0x04


@dataclass
class PVSAccSample:
    """A single 3-axis accelerometer sample (mg)."""
    x: int
    y: int
    z: int


@dataclass
class PVSGyroSample:
    """A single 3-axis gyroscope sample (dps)."""
    x: int
    y: int
    z: int


@dataclass
class PVSMagSample:
    """A single 3-axis magnetometer sample (Gauss/10)."""
    x: int
    y: int
    z: int


@dataclass
class PVSPPISample:
    """A single PPI (Pulse-to-Pulse Interval) sample."""
    ppi_ms: int          # Pulse-to-pulse interval in ms
    error_estimate: int  # Error estimate in ms
    hr: int              # Heart rate in BPM
    blocker: bool        # True if blocker detected
    skin_contact: bool   # True if skin contact detected


@dataclass
class PVSPPGSample:
    """A single PPG (optical) sample with multiple channels."""
    channels: List[int] = field(default_factory=list)


@dataclass
class PVSDataPacket:
    """Parsed PMD data packet from the Polar Verity Sense."""
    measurement_type: int
    timestamp_ns: int
    frame_type: int
    acc_samples: List[PVSAccSample] = field(default_factory=list)
    gyro_samples: List[PVSGyroSample] = field(default_factory=list)
    mag_samples: List[PVSMagSample] = field(default_factory=list)
    ppi_samples: List[PVSPPISample] = field(default_factory=list)
    ppg_samples: List[PVSPPGSample] = field(default_factory=list)


def parse_pmd_header(data: bytearray) -> Tuple[int, int, int]:
    """Parse the 10-byte PMD packet header.

    Returns: (measurement_type, timestamp_ns, frame_type)
    """
    if len(data) < 10:
        raise ValueError(f"PMD packet too short: {len(data)} bytes (need >= 10)")

    measurement_type = data[0]
    timestamp_ns = struct.unpack_from('<Q', data, 1)[0]
    frame_type = data[9]
    return measurement_type, timestamp_ns, frame_type


def _read_signed(data: bytearray, offset: int, num_bytes: int) -> int:
    """Read a signed little-endian integer of arbitrary byte width."""
    if num_bytes == 2:
        return struct.unpack_from('<h', data, offset)[0]
    elif num_bytes == 3:
        raw = data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)
        if raw & 0x800000:
            raw -= 0x1000000
        return raw
    elif num_bytes == 4:
        return struct.unpack_from('<i', data, offset)[0]
    else:
        # Generic: read unsigned then sign-extend
        raw = int.from_bytes(data[offset:offset + num_bytes], byteorder='little', signed=False)
        sign_bit = 1 << (num_bytes * 8 - 1)
        if raw & sign_bit:
            raw -= (1 << (num_bytes * 8))
        return raw


def _read_delta_bits(data: bytearray, bit_offset: int, num_bits: int) -> int:
    """Read a signed value of num_bits from a bit stream.

    bit_offset is the starting bit position in the data bytearray.
    Returns the signed integer value.
    """
    byte_offset = bit_offset // 8
    bit_in_byte = bit_offset % 8

    # Accumulate enough bytes
    total_bits_needed = bit_in_byte + num_bits
    num_bytes_needed = (total_bits_needed + 7) // 8

    raw = 0
    for i in range(num_bytes_needed):
        if byte_offset + i < len(data):
            raw |= data[byte_offset + i] << (8 * i)

    # Shift out the lower bits we don't need
    raw >>= bit_in_byte

    # Mask to num_bits
    mask = (1 << num_bits) - 1
    raw &= mask

    # Sign extend
    sign_bit = 1 << (num_bits - 1)
    if raw & sign_bit:
        raw -= (1 << num_bits)

    return raw


def parse_acc_raw(data: bytearray, offset: int, resolution_bytes: int = 2) -> List[PVSAccSample]:
    """Parse raw ACC frame data (3-axis, signed 16-bit LE by default)."""
    samples = []
    step = resolution_bytes * 3
    while offset + step <= len(data):
        x = _read_signed(data, offset, resolution_bytes)
        y = _read_signed(data, offset + resolution_bytes, resolution_bytes)
        z = _read_signed(data, offset + 2 * resolution_bytes, resolution_bytes)
        samples.append(PVSAccSample(x=x, y=y, z=z))
        offset += step
    return samples


def parse_gyro_raw(data: bytearray, offset: int, resolution_bytes: int = 2) -> List[PVSGyroSample]:
    """Parse raw GYR frame data (3-axis, signed 16-bit LE by default)."""
    samples = []
    step = resolution_bytes * 3
    while offset + step <= len(data):
        x = _read_signed(data, offset, resolution_bytes)
        y = _read_signed(data, offset + resolution_bytes, resolution_bytes)
        z = _read_signed(data, offset + 2 * resolution_bytes, resolution_bytes)
        samples.append(PVSGyroSample(x=x, y=y, z=z))
        offset += step
    return samples


def parse_mag_raw(data: bytearray, offset: int, resolution_bytes: int = 2) -> List[PVSMagSample]:
    """Parse raw MAG frame data (3-axis, signed 16-bit LE by default)."""
    samples = []
    step = resolution_bytes * 3
    while offset + step <= len(data):
        x = _read_signed(data, offset, resolution_bytes)
        y = _read_signed(data, offset + resolution_bytes, resolution_bytes)
        z = _read_signed(data, offset + 2 * resolution_bytes, resolution_bytes)
        samples.append(PVSMagSample(x=x, y=y, z=z))
        offset += step
    return samples


def parse_ppi_data(data: bytearray, offset: int) -> List[PVSPPISample]:
    """Parse PPI frame data.

    PPI samples are 6 bytes each:
      - Bytes 0-1: PPI value (uint16 LE, ms)
      - Bytes 2-3: Error estimate (uint16 LE, ms)
      - Byte 4: HR (uint8, BPM)
      - Byte 5: Flags (bit 0 = blocker, bit 1 = skin contact)
    """
    samples = []
    while offset + 6 <= len(data):
        ppi_ms = struct.unpack_from('<H', data, offset)[0]
        error_est = struct.unpack_from('<H', data, offset + 2)[0]
        hr = data[offset + 4]
        flags = data[offset + 5]
        blocker = bool(flags & 0x01)
        skin_contact = bool(flags & 0x02)
        samples.append(PVSPPISample(
            ppi_ms=ppi_ms,
            error_estimate=error_est,
            hr=hr,
            blocker=blocker,
            skin_contact=skin_contact,
        ))
        offset += 6
    return samples


def parse_ppg_raw(data: bytearray, offset: int, num_channels: int = 4,
                  resolution_bytes: int = 3) -> List[PVSPPGSample]:
    """Parse raw PPG frame data.

    Each sample has `num_channels` values, each `resolution_bytes` wide (signed LE).
    Default: 4 channels (ambient + 3 LEDs), 3 bytes (22-bit) each.
    """
    samples = []
    step = resolution_bytes * num_channels
    while offset + step <= len(data):
        channels = []
        for ch in range(num_channels):
            val = _read_signed(data, offset + ch * resolution_bytes, resolution_bytes)
            channels.append(val)
        samples.append(PVSPPGSample(channels=channels))
        offset += step
    return samples


def parse_ppg_delta(data: bytearray, offset: int, num_channels: int = 4,
                    resolution_bytes: int = 3) -> List[PVSPPGSample]:
    """Parse a delta-compressed PPG frame.

    Delta frame structure after the 10-byte header:
      - Reference sample: num_channels values of resolution_bytes each
      - Delta size: 1 byte (bits per delta value)
      - Sample count: 1 byte
      - Delta data: bit-packed stream

    Returns list of PVSPPGSample objects.
    """
    if offset >= len(data):
        return []

    # Read reference sample
    ref_channels = []
    for ch in range(num_channels):
        if offset + resolution_bytes > len(data):
            break
        val = _read_signed(data, offset, resolution_bytes)
        ref_channels.append(val)
        offset += resolution_bytes

    if len(ref_channels) < num_channels:
        return [PVSPPGSample(channels=ref_channels)] if ref_channels else []

    if offset + 2 > len(data):
        return [PVSPPGSample(channels=ref_channels)]

    delta_size = data[offset]
    offset += 1
    sample_count = data[offset]
    offset += 1

    samples = [PVSPPGSample(channels=list(ref_channels))]

    prev_channels = list(ref_channels)
    bit_offset = offset * 8

    for _ in range(sample_count):
        bits_needed = bit_offset + delta_size * num_channels
        if bits_needed > len(data) * 8:
            break
        new_channels = []
        for ch in range(num_channels):
            delta = _read_delta_bits(data, bit_offset, delta_size)
            bit_offset += delta_size
            new_channels.append(prev_channels[ch] + delta)
        prev_channels = new_channels
        samples.append(PVSPPGSample(channels=new_channels))

    return samples


def parse_delta_3axis(data: bytearray, offset: int, resolution_bytes: int = 2):
    """Parse a delta-compressed 3-axis frame (ACC, GYR, MAG).

    Delta frame structure after the 10-byte header:
      - Reference sample: 3 values of `resolution_bytes` each (signed LE)
      - Delta size: 1 byte (bits per delta value)
      - Sample count: 1 byte
      - Delta data: bit-packed stream

    Returns list of (x, y, z) tuples.
    """
    if offset >= len(data):
        return []

    # Read reference sample
    ref_x = _read_signed(data, offset, resolution_bytes)
    offset += resolution_bytes
    ref_y = _read_signed(data, offset, resolution_bytes)
    offset += resolution_bytes
    ref_z = _read_signed(data, offset, resolution_bytes)
    offset += resolution_bytes

    if offset + 2 > len(data):
        return [(ref_x, ref_y, ref_z)]

    delta_size = data[offset]
    offset += 1
    sample_count = data[offset]
    offset += 1

    samples = [(ref_x, ref_y, ref_z)]

    prev_x, prev_y, prev_z = ref_x, ref_y, ref_z
    bit_offset = offset * 8

    for _ in range(sample_count):
        bits_needed = bit_offset + delta_size * 3
        if bits_needed > len(data) * 8:
            break
        dx = _read_delta_bits(data, bit_offset, delta_size)
        bit_offset += delta_size
        dy = _read_delta_bits(data, bit_offset, delta_size)
        bit_offset += delta_size
        dz = _read_delta_bits(data, bit_offset, delta_size)
        bit_offset += delta_size

        prev_x += dx
        prev_y += dy
        prev_z += dz
        samples.append((prev_x, prev_y, prev_z))

    return samples


def parse_pmd_data(data: bytearray) -> Optional[PVSDataPacket]:
    """Parse a complete PMD data notification packet.

    Handles both raw (0x00) and delta (0x80) frame types for all
    supported measurement types.

    Returns a PVSDataPacket or None if parsing fails.
    """
    try:
        meas_type, timestamp_ns, frame_type = parse_pmd_header(data)
    except ValueError as e:
        logger.warning(f"PVS parse error: {e}")
        return None

    packet = PVSDataPacket(
        measurement_type=meas_type,
        timestamp_ns=timestamp_ns,
        frame_type=frame_type,
    )

    payload_offset = 10  # After header

    # The high bit of frame_type indicates delta compression; lower bits may encode
    # additional metadata (e.g. delta size hint). Use bitmask, not exact equality.
    is_delta = (frame_type & 0x80) == 0x80
    is_raw = frame_type == FRAME_TYPE_RAW

    if meas_type == PMD_TYPE_ACC:
        if is_raw:
            packet.acc_samples = parse_acc_raw(data, payload_offset)
        elif is_delta:
            tuples = parse_delta_3axis(data, payload_offset)
            packet.acc_samples = [PVSAccSample(x=t[0], y=t[1], z=t[2]) for t in tuples]
        else:
            logger.warning(f"PVS ACC: unknown frame type 0x{frame_type:02x}")

    elif meas_type == PMD_TYPE_GYR:
        if is_raw:
            packet.gyro_samples = parse_gyro_raw(data, payload_offset)
        elif is_delta:
            tuples = parse_delta_3axis(data, payload_offset)
            packet.gyro_samples = [PVSGyroSample(x=t[0], y=t[1], z=t[2]) for t in tuples]
        else:
            logger.warning(f"PVS GYR: unknown frame type 0x{frame_type:02x}")

    elif meas_type == PMD_TYPE_MAG:
        if is_raw:
            packet.mag_samples = parse_mag_raw(data, payload_offset)
        elif is_delta:
            tuples = parse_delta_3axis(data, payload_offset)
            packet.mag_samples = [PVSMagSample(x=t[0], y=t[1], z=t[2]) for t in tuples]
        else:
            logger.warning(f"PVS MAG: unknown frame type 0x{frame_type:02x}")

    elif meas_type == PMD_TYPE_PPI:
        # PPI doesn't use delta frames
        packet.ppi_samples = parse_ppi_data(data, payload_offset)

    elif meas_type == PMD_TYPE_PPG:
        if is_raw:
            packet.ppg_samples = parse_ppg_raw(data, payload_offset)
        elif is_delta:
            packet.ppg_samples = parse_ppg_delta(data, payload_offset)
        else:
            logger.warning(f"PVS PPG: unknown frame type 0x{frame_type:02x}")

    else:
        logger.warning(f"PVS: unknown measurement type 0x{meas_type:02x}")

    return packet


def parse_settings_response(data: bytearray) -> dict:
    """Parse a PMD Control Point settings response.

    Response format (confirmed from Polar SDK / bleakheart):
      Byte 0: Response indicator (0xF0)
      Byte 1: Original op code (0x01 = get settings, 0x02 = start, etc.)
      Byte 2: Measurement type (0x02 = ACC, 0x05 = GYR, etc.)
      Byte 3: Status (0x00 = success, non-zero = error)
      Byte 4: Frame type byte (0x00 = raw, 0x80 = delta, etc.)
      Remaining (from byte 5): Setting entries [type(1), count(1), values(count * 2)]

    Returns dict like:
      {
        'measurement_type': 0x02,
        'error': 0,
        'frame_type': 0x00,
        'sample_rates': [25, 50, 100, 200],
        'resolutions': [16],
        'ranges': [2, 4, 8],
        'channels': [4],
      }
    """
    if len(data) < 4:
        logger.warning(f"PVS settings response too short: {len(data)} bytes, data={data.hex()}")
        return {'error': -1}

    result = {
        'response_indicator': data[0],
        'op_code': data[1],
        'measurement_type': data[2],
        'error': data[3],
        'frame_type': None,
        'sample_rates': [],
        'resolutions': [],
        'ranges': [],
        'channels': [],
    }

    logger.info(f"PVS CP response: indicator=0x{data[0]:02x} op=0x{data[1]:02x} "
                f"type=0x{data[2]:02x} status=0x{data[3]:02x} full={data.hex()}")

    if data[3] != 0x00:
        logger.warning(f"PVS settings error for type 0x{data[2]:02x}: "
                       f"status=0x{data[3]:02x}")
        return result

    # For GET_SETTINGS responses (op=0x01), byte 4 is the frame type byte
    # Settings data starts at byte 5
    if len(data) < 5:
        return result

    if data[1] == 0x01:
        # GET_SETTINGS response: byte 4 is frame type, settings start at 5
        result['frame_type'] = data[4]
        offset = 5
    else:
        # START/STOP responses: no frame type byte, settings start at 4
        offset = 4

    while offset + 2 <= len(data):
        setting_type = data[offset]
        count = data[offset + 1]
        offset += 2

        values = []
        for _ in range(count):
            if offset + 2 <= len(data):
                val = struct.unpack_from('<H', data, offset)[0]
                values.append(val)
                offset += 2
            elif offset + 1 <= len(data):
                # Handle trailing single byte (some firmware versions
                # send 1-byte values for certain settings like CHANNELS)
                val = data[offset]
                values.append(val)
                offset += 1
                logger.debug(f"PVS settings: read 1-byte value {val} for "
                           f"type 0x{setting_type:02x}")
            else:
                break

        if setting_type == SETTING_SAMPLE_RATE:
            result['sample_rates'] = values
        elif setting_type == SETTING_RESOLUTION:
            result['resolutions'] = values
        elif setting_type == SETTING_RANGE:
            result['ranges'] = values
        elif setting_type == SETTING_CHANNELS:
            result['channels'] = values
        else:
            logger.debug(f"PVS settings: unknown type 0x{setting_type:02x} = {values}")

    logger.info(f"PVS parsed settings for type 0x{result['measurement_type']:02x}: "
                f"rates={result['sample_rates']}, res={result['resolutions']}, "
                f"ranges={result['ranges']}, channels={result['channels']}")

    return result


def build_get_settings_command(measurement_type: int) -> bytearray:
    """Build a PMD 'Get Settings' command.

    Format: [0x01, measurement_type]
    """
    return bytearray([0x01, measurement_type])


def build_start_command(measurement_type: int, sample_rate: int = 0,
                        resolution: int = 0, range_val: int = 0,
                        channels: int = 0) -> bytearray:
    """Build a PMD 'Start Measurement' command.

    Format: [0x02, measurement_type, setting_type, array_len, value_LE16, ...]
    Only includes settings with non-zero values.
    """
    cmd = bytearray([0x02, measurement_type])

    if sample_rate > 0:
        cmd.append(SETTING_SAMPLE_RATE)
        cmd.append(0x01)
        cmd.extend(struct.pack('<H', sample_rate))

    if resolution > 0:
        cmd.append(SETTING_RESOLUTION)
        cmd.append(0x01)
        cmd.extend(struct.pack('<H', resolution))

    if range_val > 0:
        cmd.append(SETTING_RANGE)
        cmd.append(0x01)
        cmd.extend(struct.pack('<H', range_val))

    if channels > 0:
        cmd.append(SETTING_CHANNELS)
        cmd.append(0x01)
        cmd.extend(struct.pack('<H', channels))

    return cmd


def build_start_command_simple(measurement_type: int, sample_rate: int = 0,
                                resolution: int = 0, range_val: int = 0,
                                channels: int = 0) -> bytearray:
    """Build a PMD 'Start Measurement' command using simplified format.

    Some Polar devices (e.g., Verity Sense) use a simpler format where
    each setting is [key, value_byte] instead of [key, count, value_LE16].

    Format: [0x02, measurement_type, key, value, key, value, ...]
    Only includes settings with non-zero values.
    """
    cmd = bytearray([0x02, measurement_type])

    if sample_rate > 0:
        cmd.append(SETTING_SAMPLE_RATE)
        # Use single byte if value fits, otherwise LE16
        if sample_rate <= 255:
            cmd.append(sample_rate & 0xFF)
        else:
            cmd.extend(struct.pack('<H', sample_rate))

    if resolution > 0:
        cmd.append(SETTING_RESOLUTION)
        cmd.append(resolution & 0xFF)

    if range_val > 0:
        cmd.append(SETTING_RANGE)
        if range_val <= 255:
            cmd.append(range_val & 0xFF)
        else:
            cmd.extend(struct.pack('<H', range_val))

    if channels > 0:
        cmd.append(SETTING_CHANNELS)
        cmd.append(channels & 0xFF)

    return cmd


def build_sdk_cmd(mtype: int, rate: int, res: int, range_val: int,
                  channels: int) -> bytearray:
    """Build a PMD start command using the proven SDK mode format.

    This matches the exact format used in the working test_pvs_sdk_stream.py.
    All settings are always included (even if zero) with the format:
      [0x02, mtype, key, 0x01, value_LE16, key, 0x01, value_LE16, ...]

    The channels setting uses a single byte value instead of LE16.
    """
    cmd = bytearray([0x02, mtype])
    # Rate (2 bytes)
    cmd.extend([SETTING_SAMPLE_RATE, 0x01])
    cmd.extend(struct.pack("<H", rate))
    # Resolution (2 bytes)
    cmd.extend([SETTING_RESOLUTION, 0x01])
    cmd.extend(struct.pack("<H", res))
    # Range (2 bytes)
    cmd.extend([SETTING_RANGE, 0x01])
    cmd.extend(struct.pack("<H", range_val))
    # Channels (1 byte!)
    cmd.extend([SETTING_CHANNELS, 0x01])
    cmd.extend([channels])
    return cmd


def build_stop_command(measurement_type: int) -> bytearray:
    """Build a PMD 'Stop Measurement' command.

    Format: [0x03, measurement_type]
    """
    return bytearray([0x03, measurement_type])
