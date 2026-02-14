import dearpygui.dearpygui as dpg
import math
import time

class PacerEngine:
    """
    Handles the visual breathing pacer logic and rendering.
    Supports 4-stage breathing: Inhale -> Hold -> Exhale -> Hold.
    """
    def __init__(self):
        # Default to 4-4-4-4 box breathing (16s cycle = 3.75 BPM)
        self.inhale_time = 4.0
        self.inhale_hold_time = 4.0
        self.exhale_time = 4.0
        self.exhale_hold_time = 4.0
        
        self.start_time = time.time()
        
        # Visual settings
        self.min_radius = 50
        self.max_radius = 250
        self.center_x = 0
        self.center_y = 0
        
        # Bar settings
        self.bar_width = 30
        self.bar_height = 400
        
        # DPG tags
        self.draw_layer = None
        self.circle_tag = None
        self.guide_text_tag = None
        self.bar_bg_tag = None
        self.bar_fill_tag = None
        
    def set_timing(self, inhale: float, inhale_hold: float, exhale: float, exhale_hold: float):
        """Updates the duration of each breathing stage."""
        self.inhale_time = max(0.1, inhale)
        self.inhale_hold_time = max(0.0, inhale_hold)
        self.exhale_time = max(0.1, exhale)
        self.exhale_hold_time = max(0.0, exhale_hold)

    def get_cycle_duration(self) -> float:
        return self.inhale_time + self.inhale_hold_time + self.exhale_time + self.exhale_hold_time

    def get_bpm(self) -> float:
        duration = self.get_cycle_duration()
        return 60.0 / duration if duration > 0 else 0.0

    def reset(self):
        self.start_time = time.time()

    def setup_draw_layer(self, parent_tag):
        """Creates the draw node and initial shapes within a DPG container."""
        with dpg.draw_node(parent=parent_tag) as self.draw_layer:
            # Expanding Circle
            self.circle_tag = dpg.draw_circle(
                center=(0, 0), 
                radius=self.min_radius, 
                color=(255, 255, 255, 255), 
                fill=(0, 191, 255, 150),
                thickness=2
            )
            
            # Text Guide
            self.guide_text_tag = dpg.draw_text(
                pos=(0, 0), 
                text="Breathe", 
                size=30, 
                color=(255, 255, 255, 255)
            )
            
            # Vertical Bar Background
            self.bar_bg_tag = dpg.draw_rectangle(
                pmin=(0, 0), pmax=(0, 0),
                color=(100, 100, 100, 100),
                fill=(50, 50, 50, 100),
                thickness=1
            )
            
            # Vertical Bar Fill
            self.bar_fill_tag = dpg.draw_rectangle(
                pmin=(0, 0), pmax=(0, 0),
                color=(0, 255, 255, 200),
                fill=(0, 255, 255, 150),
                thickness=0
            )

    def update(self, viewport_width, viewport_height):
        """
        Updates the pacer visuals based on time.
        Returns the current phase (0.0 to 1.0).
        """
        if not self.draw_layer:
            return 0.0

        # Calculate cycle progress
        now = time.time()
        elapsed = now - self.start_time
        cycle_duration = self.get_cycle_duration()
        
        if cycle_duration <= 0:
            return 0.0
            
        cycle_time = elapsed % cycle_duration
        
        # Determine Stage
        # 1. Inhale
        # 2. Inhale Hold
        # 3. Exhale
        # 4. Exhale Hold
        
        expansion = 0.0
        state_text = ""
        fill_color = (0, 0, 0, 0)
        
        t1 = self.inhale_time
        t2 = t1 + self.inhale_hold_time
        t3 = t2 + self.exhale_time
        t4 = t3 + self.exhale_hold_time
        
        if cycle_time < t1:
            # Inhale Stage
            progress = cycle_time / self.inhale_time
            expansion = progress # 0 to 1
            state_text = "Inhale"
            fill_color = (0, 191, 255, 150) # Deep Sky Blue
            
        elif cycle_time < t2:
            # Inhale Hold Stage
            expansion = 1.0 # Stay full
            state_text = "Hold"
            fill_color = (255, 215, 0, 150) # Gold
            
        elif cycle_time < t3:
            # Exhale Stage
            progress = (cycle_time - t2) / self.exhale_time
            expansion = 1.0 - progress # 1 to 0
            state_text = "Exhale"
            fill_color = (0, 0, 139, 150) # Dark Blue
            
        else:
            # Exhale Hold Stage
            expansion = 0.0 # Stay empty
            state_text = "Hold"
            fill_color = (100, 100, 100, 150) # Grey

        # --- Update Circle ---
        self.center_x = viewport_width / 2
        self.center_y = viewport_height / 2
        
        current_radius = self.min_radius + (self.max_radius - self.min_radius) * expansion
        
        dpg.configure_item(self.circle_tag, center=(self.center_x, self.center_y), radius=current_radius, fill=fill_color)
        
        # Center text
        # Approx width: 15px per char
        text_width = len(state_text) * 12 
        dpg.configure_item(self.guide_text_tag, pos=(self.center_x - text_width/2, self.center_y - 15), text=state_text)

        # --- Update Vertical Bar ---
        # Position: Right side of the screen
        bar_x_start = viewport_width - 80
        bar_y_start = (viewport_height - self.bar_height) / 2
        
        dpg.configure_item(self.bar_bg_tag, 
                           pmin=(bar_x_start, bar_y_start), 
                           pmax=(bar_x_start + self.bar_width, bar_y_start + self.bar_height))
        
        # Fill height based on expansion
        fill_height = self.bar_height * expansion
        dpg.configure_item(self.bar_fill_tag,
                           pmin=(bar_x_start, bar_y_start + self.bar_height - fill_height),
                           pmax=(bar_x_start + self.bar_width, bar_y_start + self.bar_height),
                           fill=fill_color)

        return cycle_time / cycle_duration
