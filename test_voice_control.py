#!/usr/bin/env python3
"""
Unit and integration tests for Vosk voice control + intent extraction.
Run:  python test_voice_control.py
"""

import os
import sys
from pathlib import Path

# Ensure Fitness-Robot directory is on sys.path; force local matcher for deterministic tests
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.environ["VOICE_LLM_MODE"] = "off"

from intent_parser import extract_intent, normalize, extract_params
from voice_control import (
    parse_voice_command,
    execute_voice_command,
    get_voice_state,
    VoiceState,
)
from session_manager import (
    remote_mode_queue, start_exercise, force_stop, pause_request, resume_request,
)


def test_command_parsing():
    test_phrases = {
        # Simple commands
        'stop': 'STOP',
        'freeze robot': 'STOP',
        'halt': 'STOP',
        'go forward': 'FORWARD',
        'turn left now': 'LEFT',
        'turn right': 'RIGHT',
        'resume': 'RESUME',
        # Commands inside natural sentences
        'robot could you please move forward a little bit': 'FORWARD',
        'please drive backward': 'BACKWARD',
        'hey robot i think i want to start squats now': 'SQUATS',
        'let us do pushups': 'PUSHUPS',
        'okay lets do some push ups': 'PUSHUPS',
        'i would like to start bicep curls please': 'BICEP_CURLS',
        'plank exercise': 'PLANK',
        'can you follow me please': 'PERSON_FOLLOW',
        'start workout': 'START',
        'okay i am ready': 'START',
        'pause workout': 'PAUSE',
        'i need a break': 'PAUSE',
        'go to main menu': 'HOME',
        'take me back to the main menu': 'HOME',
        'i am done for today': 'HOME',
        'go forward left': 'FWD_LEFT',
        'that is all right stop moving': 'STOP',
        # Negation: "don't stop" must not stop
        'dont stop keep going': 'RESUME',
    }
    failures = []
    for phrase, expected in test_phrases.items():
        actual = parse_voice_command(phrase)
        if actual != expected:
            failures.append(f"'{phrase}': expected {expected}, got {actual}")
    assert not failures, "Parsing failures:\n  " + "\n  ".join(failures)
    print(f"✅ All {len(test_phrases)} sentence -> command tests passed!")


def test_non_commands():
    for phrase in ["what a lovely day", "i went to the store yesterday", "hmm", ""]:
        assert parse_voice_command(phrase) is None, f"'{phrase}' should not be a command"
    print("✅ Non-command conversation correctly ignored!")


def test_param_extraction():
    assert normalize("Go forward for twenty five seconds!") == "go forward for 25 seconds"
    assert extract_params(normalize("move back for three seconds"))["duration"] == 3.0
    assert extract_params(normalize("do ten reps"))["count"] == 10
    assert extract_params(normalize("move forward a little"))["scale"] == 0.5
    intent = extract_intent("robot go forward for four seconds")
    assert intent.name == "FORWARD" and intent.params["duration"] == 4.0
    print("✅ Parameter extraction tests passed!")


def test_command_execution():
    assert execute_voice_command("robot stop")
    _, _, _, last_cmd = get_voice_state()
    assert last_cmd == "STOP"

    assert execute_voice_command("hey can we start squats")
    cmd_type, mode = remote_mode_queue.get_nowait()
    assert (cmd_type, mode) == ("switch", "SQUATS")

    start_exercise.clear()
    assert execute_voice_command("start exercise")
    assert start_exercise.is_set()

    pause_request.clear()
    assert execute_voice_command("pause the workout")
    assert pause_request.is_set()
    assert execute_voice_command("continue")
    assert resume_request.is_set() and not pause_request.is_set()

    force_stop.clear()
    assert execute_voice_command("go home")
    assert force_stop.is_set()

    assert not execute_voice_command("what a lovely day")
    print("✅ All command execution tests passed!")


if __name__ == "__main__":
    test_command_parsing()
    test_non_commands()
    test_param_extraction()
    test_command_execution()
    print("🎉 All voice control tests completed successfully!")
