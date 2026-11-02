#!/usr/bin/env python3
"""
Unit and integration tests for Vosk voice control module.
"""

import sys
from pathlib import Path

# Ensure Fitness-Robot directory is on sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from voice_control import (
    parse_voice_command,
    execute_voice_command,
    get_voice_state,
    VoiceState,
    _execute_timed_motion,
    _execute_stop_motion,
)
from session_manager import remote_mode_queue, start_exercise, force_stop

def test_command_parsing():
    test_phrases = {
        'stop': 'STOP',
        'freeze robot': 'STOP',
        'halt': 'STOP',
        'go forward': 'FORWARD',
        'robot move forward': 'FORWARD',
        'please drive backward': 'BACKWARD',
        'turn left now': 'LEFT',
        'turn right': 'RIGHT',
        'start squats': 'SQUATS',
        'let us do pushups': 'PUSHUPS',
        'start bicep curls': 'BICEP_CURLS',
        'plank exercise': 'PLANK',
        'follow me please': 'PERSON_FOLLOW',
        'start workout': 'START',
        'pause workout': 'PAUSE',
        'resume': 'RESUME',
        'go to main menu': 'HOME',
    }

    for phrase, expected in test_phrases.items():
        actual = parse_voice_command(phrase)
        assert actual == expected, f"Failed for '{phrase}': expected {expected}, got {actual}"
    print(f"✅ All {len(test_phrases)} voice command parsing tests passed!")


def test_command_execution():
    # Test STOP
    assert execute_voice_command("robot stop")
    state, msg, last_txt, last_cmd = get_voice_state()
    assert last_cmd == "STOP"

    # Test mode queueing
    assert execute_voice_command("start squats")
    assert not remote_mode_queue.empty()
    cmd_type, mode = remote_mode_queue.get()
    assert cmd_type == "switch"
    assert mode == "SQUATS"

    # Test start exercise
    start_exercise.clear()
    assert execute_voice_command("start exercise")
    assert start_exercise.is_set()

    # Test home / force stop
    force_stop.clear()
    assert execute_voice_command("go home")
    assert force_stop.is_set()

    print("✅ All command execution tests passed!")


if __name__ == "__main__":
    test_command_parsing()
    test_command_execution()
    print("🎉 All voice control tests completed successfully!")

