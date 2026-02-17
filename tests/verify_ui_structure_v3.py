
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
    
    print("Verifying UI Structure (Device Subsections)...")
    all_pass = True
    
    # 1. Check for "GRAPHS" header
    if dpg.does_item_exist("header_graphs"):
        print("[PASS] 'GRAPHS' header found.")
        label = dpg.get_item_label("header_graphs")
        if label == "GRAPHS":
            print("[PASS] 'GRAPHS' header label correct.")
        else:
            print(f"[FAIL] 'GRAPHS' header label is '{label}', expected 'GRAPHS'.")
            all_pass = False
    else:
        print("[FAIL] 'GRAPHS' header NOT found.")
        all_pass = False

    # 2. Check for "Polar H10" device subsection
    if dpg.does_item_exist("header_polar_h10"):
        print("[PASS] 'Polar H10' subsection header found.")
        label = dpg.get_item_label("header_polar_h10")
        if label == "Polar H10":
            print("[PASS] 'Polar H10' label correct.")
        else:
            print(f"[FAIL] 'Polar H10' label is '{label}'.")
            all_pass = False
        
        # Check it starts hidden (show=False)
        shown = dpg.is_item_shown("header_polar_h10")
        if not shown:
            print("[PASS] 'Polar H10' subsection starts hidden (show=False).")
        else:
            print("[FAIL] 'Polar H10' subsection should start hidden.")
            all_pass = False
        
        # Check parent is inside the GRAPHS header
        parent = dpg.get_item_parent("header_polar_h10")
        graphs_header_id = dpg.get_alias_id("header_graphs") if hasattr(dpg, 'get_alias_id') else None
        if graphs_header_id and parent == graphs_header_id:
            print("[PASS] 'Polar H10' is nested inside GRAPHS header.")
        elif parent is not None:
            print(f"[INFO] 'Polar H10' parent: {parent} (GRAPHS header: {graphs_header_id})")
    else:
        print("[FAIL] 'Polar H10' subsection header NOT found.")
        all_pass = False

    # 3. Check for "Genki Wave" device subsection
    if dpg.does_item_exist("header_genki_wave"):
        print("[PASS] 'Genki Wave' subsection header found.")
        label = dpg.get_item_label("header_genki_wave")
        if label == "Genki Wave":
            print("[PASS] 'Genki Wave' label correct.")
        else:
            print(f"[FAIL] 'Genki Wave' label is '{label}'.")
            all_pass = False
        
        # Check it starts hidden
        shown = dpg.is_item_shown("header_genki_wave")
        if not shown:
            print("[PASS] 'Genki Wave' subsection starts hidden (show=False).")
        else:
            print("[FAIL] 'Genki Wave' subsection should start hidden.")
            all_pass = False
    else:
        print("[FAIL] 'Genki Wave' subsection header NOT found.")
        all_pass = False

    # 4. Check Polar H10 graphs container has charts
    if dpg.does_item_exist("polar_h10_graphs_container"):
        children = dpg.get_item_children("polar_h10_graphs_container", 1)
        count = len(children) if children else 0
        print(f"Polar H10 graphs container children count: {count}")
        if count >= 9:  # 9 chart tree nodes
            print("[PASS] Polar H10 graphs container has all 9 charts.")
        else:
            print(f"[FAIL] Expected >= 9 charts, found {count}.")
            all_pass = False
    else:
        print("[FAIL] 'polar_h10_graphs_container' NOT found.")
        all_pass = False

    # 5. Check Genki Wave graphs container exists
    if dpg.does_item_exist("genki_wave_graphs_container"):
        print("[PASS] 'genki_wave_graphs_container' found.")
        children = dpg.get_item_children("genki_wave_graphs_container", 1)
        count = len(children) if children else 0
        print(f"Genki Wave graphs container children count: {count}")
        # Should have at least the placeholder text
        if count >= 1:
            print("[PASS] Genki Wave container has placeholder content.")
        else:
            print("[FAIL] Genki Wave container is empty.")
            all_pass = False
    else:
        print("[FAIL] 'genki_wave_graphs_container' NOT found.")
        all_pass = False

    # 6. Check that old 'graphs_container' no longer exists
    if dpg.does_item_exist("graphs_container"):
        print("[FAIL] Old 'graphs_container' still exists (should be replaced).")
        all_pass = False
    else:
        print("[PASS] Old 'graphs_container' removed (replaced by device subsections).")

    # 7. Check APPS section still works
    if dpg.does_item_exist("header_apps"):
        print("[PASS] 'APPS' header still exists.")
    else:
        print("[FAIL] 'APPS' header missing.")
        all_pass = False

    if dpg.does_item_exist("apps_container"):
        print("[PASS] 'apps_container' still exists.")
    else:
        print("[FAIL] 'apps_container' missing.")
        all_pass = False

    # 8. Verify device subsection themes exist
    if dpg.does_item_exist("device_subsection_theme"):
        print("[PASS] Device subsection theme exists.")
    else:
        print("[FAIL] Device subsection theme missing.")
        all_pass = False

    print()
    if all_pass:
        print("ALL CHECKS PASSED ✓")
    else:
        print("SOME CHECKS FAILED ✗")

    dpg.destroy_context()
    return all_pass

if __name__ == "__main__":
    success = verify_ui_structure()
    sys.exit(0 if success else 1)
