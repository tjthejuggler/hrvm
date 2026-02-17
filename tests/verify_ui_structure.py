
import dearpygui.dearpygui as dpg
import sys
import os

# Adjust path to find src
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.gui.ui_manager import UIManager

def verify_ui_structure():
    dpg.create_context()
    
    # Mocking pipes as None since we only check UI structure
    ui = UIManager(None, None, None, "shm_test")
    ui.setup_ui()
    
    # Verification Logic
    print("Verifying UI Structure...")
    
    # 1. Check for "Apps" header
    if dpg.does_item_exist("header_apps"):
        print("[PASS] 'Apps' header found.")
        parent = dpg.get_item_parent("header_apps")
        print(f"      Parent of 'Apps': {dpg.get_item_label(parent) if dpg.does_item_exist(parent) else parent}")
    else:
        print("[FAIL] 'Apps' header NOT found.")

    # 2. Check for "Graphs" header
    if dpg.does_item_exist("header_graphs"):
        print("[PASS] 'Graphs' header found.")
    else:
        print("[FAIL] 'Graphs' header NOT found.")

    # 3. Check for children in Apps
    apps_children = dpg.get_item_children("apps_container", 1)  # 1 is for slot 1 (regular children)
    print(f"Apps container children count: {len(apps_children) if apps_children else 0}")
    
    # 4. Check for children in Graphs
    graphs_children = dpg.get_item_children("graphs_container", 1)
    print(f"Graphs container children count: {len(graphs_children) if graphs_children else 0}")

    # Check if charts are inside graphs_container
    # We can't easily check python object association to dpg tag here without more introspection,
    # but the count suggests if items were added.
    if graphs_children and len(graphs_children) >= 8: # Biofeedback, Heartbeat, ACC, ECG, Tachogram, Poincare, RMSSD, SDNN, Coherence
         print("[PASS] Graphs container seems populated.")
    else:
         print(f"[FAIL] Graphs container under-populated (found {len(graphs_children) if graphs_children else 0}).")

    dpg.destroy_context()

if __name__ == "__main__":
    verify_ui_structure()
