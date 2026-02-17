import time
from src.gui.counting_game import CountingGameController, CountingGameWidget

def test_counting_game_widget_structure():
    """Verify the widget can be instantiated and has expected tags."""
    print("Testing CountingGameWidget structure...")
    widget = CountingGameWidget()
    assert widget.TAG_GROUP == "counting_game_group"
    assert widget.TAG_BTN == "counting_game_btn"
    print("Widget structure OK.")

def test_counting_game_logic():
    """Verify the core logic of the counting game (controller level)."""
    print("Testing CountingGameController logic...")
    controller = CountingGameController()
    assert controller.state == "idle"
    
    # Start round
    duration = controller.start_round()
    assert 20.0 <= duration <= 80.0
    assert controller.state == "counting"
    
    # Simulate time passing (mocking time isn't easy here without patching, 
    # so we'll just check state transitions manually if possible or trust the unit)
    # Ideally we'd inject a time source. For now, we test the logic flows we can control.
    
    # Add RR
    controller.add_rr(800)
    assert len(controller._rr_intervals) == 1
    
    # Force state to input (simulating timer expiry)
    controller.state = "input"
    
    # Submit guess
    result = controller.submit_guess(10)
    assert result is not None
    assert result["guessed_count"] == 10
    assert controller.state == "idle"
    print("Controller logic OK.")

if __name__ == "__main__":
    try:
        test_counting_game_widget_structure()
        test_counting_game_logic()
        print("All CountingGame verifications passed.")
    except AssertionError as e:
        print(f"Verification failed: {e}")
        exit(1)
    except Exception as e:
        print(f"An error occurred: {e}")
        exit(1)
