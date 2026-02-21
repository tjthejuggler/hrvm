from enum import Enum

class ActionType(Enum):
    SET_COLOR = "Set Color"
    NEXT_IN_CYCLE = "Cycle: Next Color"
    PREV_IN_CYCLE = "Cycle: Prev Color"
    START_RAINBOW = "Rainbow: Start (Fade)"
    STOP_RAINBOW = "Rainbow: Stop (Select)"
    TOGGLE_RAINBOW = "Rainbow: Toggle (Start/Stop)"

class GestureType(Enum):
    INSTANT = "Instant (Hold)"
    PRESS = "Press (Edge)"
    SINGLE_CLICK = "1x Click"
    DOUBLE_CLICK = "2x Click"
    TRIPLE_CLICK = "3x Click"

# Device Constants
DEV_H10 = "Polar H10"
DEV_PVS = "Polar Verity Sense"
DEV_GENKI = "Genki Wave"

METRIC_DEFS = {
    DEV_H10: {
        "HR": None, "RR": None, 
        "Accelerometer": ["Acc X", "Acc Y", "Acc Z"], "ECG": None
    },
    DEV_PVS: {
        "HR": None,
        "Accelerometer": ["Acc X", "Acc Y", "Acc Z"],
        "Gyroscope": ["Gyro X", "Gyro Y", "Gyro Z"],
        "Magnetometer": ["Mag X", "Mag Y", "Mag Z"]
    },
    DEV_GENKI: {
        "Accelerometer": ["Acc X", "Acc Y", "Acc Z"],
        "Gyroscope": ["Gyro X", "Gyro Y", "Gyro Z"],
        "Magnetometer": ["Mag X", "Mag Y", "Mag Z"],
        "Pitch": None, "Roll": None
    }
}