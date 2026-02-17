
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
    print("Verifying UI Structure (With Custom Themes)...")
    
    # 0. Check for deleted "Charts Area" label
    # This is tricky because labels aren't tags. We scan all items in charts_area
    children = dpg.get_item_children("charts_area", 1)
    charts_area_labels = []
    if children:
        for child in children:
            if dpg.get_item_type(child) == "mvAppItemType::mvText":
                charts_area_labels.append(dpg.get_item_label(child))
    
    if "Charts Area" in charts_area_labels:
         print("[FAIL] 'Charts Area' label STILL EXISTS.")
    else:
         print("[PASS] 'Charts Area' label removed.")


    # 1. Check for "Apps" header
    if dpg.does_item_exist("header_apps"):
        print("[PASS] 'Apps' header found.")
        label = dpg.get_item_label("header_apps")
        print(f"      Label: {label}")
        if label == "APPS":
            print("[PASS] 'Apps' header label updated to uppercase.")
        else:
            print(f"[WARN] 'Apps' header label is '{label}', expected 'APPS'.")
            
        # Check theme binding
        theme = dpg.get_item_theme("header_apps")
        # In a headless test, tag alias resolution for themes might be internal IDs.
        # We just want to check if it has A theme.
        if theme:
             print(f"[PASS] 'Apps' header has theme bound: {theme}")
        else:
             print(f"[FAIL] 'Apps' header theme missing.")

    else:
        print("[FAIL] 'Apps' header NOT found.")

    # 2. Check for "Graphs" header
    if dpg.does_item_exist("header_graphs"):
        print("[PASS] 'Graphs' header found.")
        label = dpg.get_item_label("header_graphs")
        print(f"      Label: {label}")
        if label == "GRAPHS":
            print("[PASS] 'Graphs' header label updated to uppercase.")
        else:
            print(f"[WARN] 'Graphs' header label is '{label}', expected 'GRAPHS'.")
            
         # Check theme binding
        theme = dpg.get_item_theme("header_graphs")
        if theme:
             print(f"[PASS] 'Graphs' header has theme bound: {theme}")
        else:
             print(f"[FAIL] 'Graphs' header theme missing.")

    else:
        print("[FAIL] 'Graphs' header NOT found.")

    # 3. Check for children in Apps
    apps_children = dpg.get_item_children("apps_container", 1)
    print(f"Apps container children count: {len(apps_children) if apps_children else 0}")
    
    # 4. Check for children in Graphs
    graphs_children = dpg.get_item_children("graphs_container", 1)
    print(f"Graphs container children count: {len(graphs_children) if graphs_children else 0}")

    dpg.destroy_context()

if __name__ == "__main__":
    verify_ui_structure()
