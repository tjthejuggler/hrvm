import socket
import struct
import time
import colorsys
import math
from typing import List, Dict, Optional
from .ltx_defs import *

class LTXSender:
    def __init__(self, ip: str, port: int = 41412):
        self.ip = ip
        self.port = port

    def send_color(self, r: int, g: int, b: int):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # 8-byte Header: 66, 0, 0, 0
                udp_header = struct.pack("!bIBH", 66, 0, 0, 0)
                # Command: 0x0a + R + G + B
                color_data = struct.pack("!BBBB", 0x0a, r, g, b)
                sock.sendto(udp_header + color_data, (self.ip, self.port))
            finally:
                sock.close()
        except:
            pass

class GestureDetector:
    def __init__(self, click_speed=0.4):
        self.active = False
        self.last_release_time = 0.0
        self.click_count = 0
        self.click_speed = click_speed
        self.processed = False

    def update(self, is_triggered: bool, now: float) -> Optional[GestureType]:
        result = None
        if is_triggered:
            if not self.active:
                self.active = True
                self.processed = False
                return GestureType.PRESS
        else:
            if self.active:
                self.active = False
                self.click_count += 1
                self.last_release_time = now
        
        if self.click_count > 0 and not self.active:
            if (now - self.last_release_time) > self.click_speed:
                if self.click_count == 1: result = GestureType.SINGLE_CLICK
                elif self.click_count == 2: result = GestureType.DOUBLE_CLICK
                elif self.click_count >= 3: result = GestureType.TRIPLE_CLICK
                self.click_count = 0
        return result

class BallState:
    def __init__(self):
        self.current_color = [0, 0, 0] # [R, G, B]
        self.mode = "solid" # 'solid' or 'rainbow'
        self.cycle_index = 0
        self.rainbow_phase = 0.0

    def set_color(self, r, g, b):
        self.mode = "solid"
        self.current_color = [r, g, b]

class LTXEngine:
    def __init__(self):
        self.ips = []
        self.senders = {}
        self.ball_states: Dict[int, BallState] = {}
        self.gesture_detectors: Dict[int, GestureDetector] = {} 
        
        self.cycles: Dict[str, List[List[int]]] = {
            "RGB": [[255,0,0], [0,255,0], [0,0,255]],
            "Traffic": [[255, 0, 0], [255, 255, 0], [0, 255, 0]],
            "Police": [[255, 0, 0], [0, 0, 255]],
            "Pastel": [[255, 179, 186], [255, 223, 186], [255, 255, 186], [186, 255, 201], [186, 225, 255]]
        }

    def update_ips(self, ips):
        self.ips = ips
        self.senders = {ip: LTXSender(ip) for ip in ips}
        for i in range(3):
            if i not in self.ball_states:
                self.ball_states[i] = BallState()

    def tick(self, triggers: List[Dict], data_state: Dict):
        now = time.time()
        
        # 1. Evaluate Triggers
        for idx, trig in enumerate(triggers):
            if idx not in self.gesture_detectors:
                self.gesture_detectors[idx] = GestureDetector()
            
            is_over = self._evaluate_threshold(trig, data_state)
            gesture = self.gesture_detectors[idx].update(is_over, now)
            
            req_gesture = GestureType(trig.get('gesture', GestureType.INSTANT.value))
            should_fire = False

            if req_gesture == GestureType.INSTANT and is_over:
                last_t = trig.get('_last_fire', 0)
                if now - last_t > 0.1:
                    should_fire = True
                    trig['_last_fire'] = now
            elif gesture == req_gesture:
                should_fire = True

            if should_fire:
                self._execute_action(trig)

        # 2. Update Animations & Send
        self._update_and_send_balls(now)

    def _evaluate_threshold(self, trig, data_state) -> bool:
        dev, metric = trig['device'], trig['metric']
        if not dev or not metric: return False
        
        data_map = data_state.get(dev, {})
        val = 0.0
        
        axes = trig.get('axes', [])
        metric_def = METRIC_DEFS.get(dev, {}).get(metric)
        
        if metric_def is None: # Scalar
            val = float(data_map.get(metric, 0.0))
        else: # Vector
            temp_sum = 0.0
            if 'x' in axes and len(metric_def) > 0: temp_sum += abs(data_map.get(metric_def[0], 0))
            if 'y' in axes and len(metric_def) > 1: temp_sum += abs(data_map.get(metric_def[1], 0))
            if 'z' in axes and len(metric_def) > 2: temp_sum += abs(data_map.get(metric_def[2], 0))
            val = temp_sum

        op, thresh = trig.get('operator', '>'), float(trig.get('threshold', 0))
        return val > thresh if op == '>' else val < thresh

    def _execute_action(self, trig):
        targets = trig.get('targets', [])
        action_type = ActionType(trig.get('action_type', ActionType.SET_COLOR.value))
        
        for t_idx in targets:
            state = self.ball_states[t_idx]
            
            if action_type == ActionType.SET_COLOR:
                c = trig.get('color', [255, 255, 255])
                state.set_color(int(c[0]), int(c[1]), int(c[2]))
                
            elif action_type in [ActionType.NEXT_IN_CYCLE, ActionType.PREV_IN_CYCLE]:
                cycle_name = trig.get('cycle_name', "Traffic")
                cycle = self.cycles.get(cycle_name, [[255,255,255]])
                
                direction = 1 if action_type == ActionType.NEXT_IN_CYCLE else -1
                state.cycle_index = (state.cycle_index + direction) % len(cycle)
                
                next_c = cycle[state.cycle_index]
                state.set_color(next_c[0], next_c[1], next_c[2])
                
            elif action_type == ActionType.START_RAINBOW:
                state.mode = "rainbow"
                
            elif action_type == ActionType.STOP_RAINBOW:
                state.mode = "solid"
                
            elif action_type == ActionType.TOGGLE_RAINBOW:
                if state.mode == "rainbow":
                    state.mode = "solid"
                else:
                    state.mode = "rainbow"

    def _update_and_send_balls(self, now):
        for i, state in self.ball_states.items():
            r, g, b = 0, 0, 0
            
            if state.mode == "rainbow":
                # Cycle speed: 3 seconds for full spectrum
                speed = 0.33 
                state.rainbow_phase = (now * speed) % 1.0
                rgb = colorsys.hsv_to_rgb(state.rainbow_phase, 1.0, 1.0)
                r, g, b = int(rgb[0]*255), int(rgb[1]*255), int(rgb[2]*255)
                state.current_color = [r, g, b]
            else:
                r, g, b = state.current_color
                
            if i < len(self.ips):
                ip = self.ips[i]
                if ip in self.senders:
                    self.senders[ip].send_color(r, g, b)

    def get_ball_color(self, idx):
        if idx in self.ball_states:
            return self.ball_states[idx].current_color
        return [30, 30, 30]