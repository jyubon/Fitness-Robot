#!/usr/bin/env python3
"""
Unit and integration tests for Vosk voice control, wake-word ("FitBot")
enforcement during exercises, conversational responses, and robot commands.
Run:  python test_voice_control.py
"""

import os
import sys
from pathlib import Path

# Ensure Fitness-Robot directory is on sys.path; force local matcher for deterministic tests
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.environ["VOICE_LLM_MODE"] = "off"

import audio_manager
# Disable external PowerShell/espeak subprocess spawning during unit tests for speed
audio_manager.speak_text = lambda text: None

from intent_parser import (
    extract_intent,
    normalize,
    extract_params,
    split_on_wake_word,
    clear_wake_word_window,
)
from voice_control import (
    parse_voice_command,
    execute_voice_command,
    get_voice_state,
    get_last_voice_reply,
    _execute_stop_motion,
)
from session_manager import (
    remote_mode_queue,
    start_exercise,
    force_stop,
    pause_request,
    resume_request,
    exercise_executing,
)


def test_home_screen_commands():
    """On the Home screen (exercising=False), commands work with or without 'FitBot'."""
    exercise_executing.clear()
    clear_wake_word_window()

    test_phrases = {
        # Safety & stationary controls
        "stop": "STOP",
        "freeze robot": "STOP",
        "halt": "STOP",
        # ── EXHIBITION STATIONARY MODE: Movement tests commented out for reuse later ──
        # "rotate": "ROTATE",
        # "fitbot rotate": "ROTATE",
        # "spin around": "ROTATE",
        # "rotate left": "ROTATE_LEFT",
        # "turn left now": "ROTATE_LEFT",
        # "rotate right": "ROTATE_RIGHT",
        # "turn right": "ROTATE_RIGHT",
        # "move left": "MOVE_LEFT",
        # "fitbot move left": "MOVE_LEFT",
        # "slide left": "MOVE_LEFT",
        # "move right": "MOVE_RIGHT",
        # "fitbot move right": "MOVE_RIGHT",
        # "go right a bit": "MOVE_RIGHT",
        # "go forward": "FORWARD",
        # "robot could you please move forward a little bit": "FORWARD",
        # "please drive backward": "BACKWARD",
        # "go forward left": "FWD_LEFT",
        # Follow me & exercise modes
        # "follow me": "FOLLOW_ME",
        # "can you follow me please": "FOLLOW_ME",
        # "fitbot follow me": "FOLLOW_ME",
        "hey robot i think i want to start squats now": "SQUATS",
        "let us do pushups": "PUSHUPS",
        "okay lets do some push ups": "PUSHUPS",
        "i would like to start bicep curls please": "BICEP_CURLS",
        "plank exercise": "PLANK",
        # Session controls
        "start workout": "START",
        "okay i am ready": "START",
        "pause workout": "PAUSE",
        "i need a break": "PAUSE",
        "resume": "RESUME",
        "go to main menu": "HOME",
        "take me back to the main menu": "HOME",
        "i am done for today": "HOME",
        "that is all right stop moving": "STOP",
        "dont stop keep going": "RESUME",
        # Normal / conversational commands
        "hello": "HELLO",
        "hello fitbot": "HELLO",
        "fitbot hello": "HELLO",
        "good morning fitbot": "HELLO",
        "what's the time now": "TIME_NOW",
        "fitbot what is the time now": "TIME_NOW",
        "what time is it": "TIME_NOW",
        "what is the date today": "DATE_TODAY",
        "how are you": "HOW_ARE_YOU",
        "who are you": "WHO_ARE_YOU",
        "thank you fitbot": "THANK_YOU",
        "goodbye": "GOODBYE",
    }

    failures = []
    for phrase, expected in test_phrases.items():
        clear_wake_word_window()
        actual = parse_voice_command(phrase, exercising=False)
        if actual != expected:
            failures.append(f"'{phrase}': expected {expected}, got {actual}")
    assert not failures, "Home screen parsing failures:\n  " + "\n  ".join(failures)
    print(f"✅ All {len(test_phrases)} Home screen command & conversation tests passed!")


def test_exercise_mode_wake_word():
    """
    When an exercise is executing (exercise_executing is set), ONLY respond when
    'FitBot' is spoken and then the command follows (plus 'Hello FitBot').
    """
    exercise_executing.set()
    clear_wake_word_window()
    try:
        # 1. Commands WITHOUT "FitBot" must be ignored during exercise
        ignored_during_exercise = [
            "stop",
            "pause",
            "resume",
            "move left",
            "move right",
            "rotate",
            "follow me",
            "what's the time now",
            "hello",
            "go home",
            "start squats",
            # Command BEFORE FitBot (with nothing following FitBot) must also be ignored
            "move left fitbot",
            "rotate fitbot",
        ]
        for phrase in ignored_during_exercise:
            clear_wake_word_window()
            res = parse_voice_command(phrase, exercising=True)
            assert res is None, f"During exercise, '{phrase}' should be ignored, but got {res}"

        # 2. Commands WITH "FitBot" followed by the command MUST be recognized
        accepted_during_exercise = {
            "fitbot stop": "STOP",
            "fitbot pause": "PAUSE",
            "fitbot pause the workout": "PAUSE",
            "fitbot resume": "RESUME",
            "fitbot start": "START",
            # ── EXHIBITION STATIONARY MODE: Exercise motion tests commented out for reuse later ──
            # "fitbot rotate": "ROTATE",
            # "fitbot rotate left": "ROTATE_LEFT",
            # "fitbot rotate right": "ROTATE_RIGHT",
            # "fitbot move left": "MOVE_LEFT",
            # "fitbot move right": "MOVE_RIGHT",
            # "fitbot follow me": "FOLLOW_ME",
            "fitbot go home": "HOME",
            "fitbot what's the time now": "TIME_NOW",
            "fitbot what is the date today": "DATE_TODAY",
            "fitbot how are you": "HOW_ARE_YOU",
            "fitbot hello": "HELLO",
            "hello fitbot": "HELLO",
            # Common Vosk 2-word phonetic transcriptions of "FitBot"
            # "fit bot move left": "MOVE_LEFT",
            # "fit but rotate": "ROTATE",
            # "feet bot follow me": "FOLLOW_ME",
            "fit bot pause": "PAUSE",
        }
        failures = []
        for phrase, expected in accepted_during_exercise.items():
            clear_wake_word_window()
            actual = parse_voice_command(phrase, exercising=True)
            if actual != expected:
                failures.append(f"'{phrase}': expected {expected}, got {actual}")
        assert not failures, "Exercise mode wake-word failures:\n  " + "\n  ".join(failures)

        # 3. Two-step wake word: saying "FitBot" alone wakes FitBot, then next command executes
        clear_wake_word_window()
        assert parse_voice_command("FitBot", exercising=True) == "WAKE_UP"
        assert parse_voice_command("pause", exercising=True) == "PAUSE"
        # Window is consumed after one command
        assert parse_voice_command("resume", exercising=True) is None

        print("✅ Exercise mode 'FitBot' wake-word enforcement tests passed!")
    finally:
        exercise_executing.clear()
        clear_wake_word_window()


def test_non_commands():
    for phrase in ["what a lovely day", "i went to the store yesterday", "hmm", ""]:
        assert parse_voice_command(phrase, exercising=False) is None, f"'{phrase}' should not be a command"
    print("✅ Non-command conversation correctly ignored!")


def test_param_extraction():
    assert normalize("Go forward for twenty five seconds!") == "go forward for 25 seconds"
    assert extract_params(normalize("move back for three seconds"))["duration"] == 3.0
    assert extract_params(normalize("do ten reps"))["count"] == 10
    assert extract_params(normalize("move forward a little"))["scale"] == 0.5
    # ── EXHIBITION STATIONARY MODE: FORWARD command disabled, testing parameter extraction helper directly
    # intent = extract_intent("FitBot go forward for four seconds", exercising=True)
    # assert intent.name == "FORWARD" and intent.params["duration"] == 4.0
    print("✅ Parameter extraction tests passed!")


def test_command_execution():
    exercise_executing.clear()
    while not remote_mode_queue.empty():
        remote_mode_queue.get_nowait()

    # 1. Conversational commands ("Hello", "Hello FitBot", "What's the time now")
    assert execute_voice_command("Hello FitBot")
    assert "FitBot" in get_last_voice_reply()

    assert execute_voice_command("What's the time now")
    assert "time now is" in get_last_voice_reply().lower()

    # 2. Stationary Stop command
    assert execute_voice_command("stop", exercising=False)
    assert get_voice_state()[3] == "STOP"
    _execute_stop_motion()

    # ── EXHIBITION STATIONARY MODE: Movement execution tests commented out for reuse later ──
    # assert execute_voice_command("rotate", exercising=False)
    # assert get_voice_state()[3] == "ROTATE"
    # _execute_stop_motion()
    # assert execute_voice_command("move left", exercising=False)
    # assert get_voice_state()[3] == "MOVE_LEFT"
    # _execute_stop_motion()
    # assert execute_voice_command("follow me", exercising=False)
    # cmd_type, mode = remote_mode_queue.get_nowait()
    # assert (cmd_type, mode) == ("switch", "PERSON_FOLLOW")

    # 3. During exercise execution: require "FitBot"
    exercise_executing.set()
    try:
        pause_request.clear()
        assert not execute_voice_command("pause")  # rejected without FitBot
        assert execute_voice_command("FitBot pause")  # accepted with FitBot
        assert pause_request.is_set()

        assert execute_voice_command("FitBot resume")
        assert resume_request.is_set() and not pause_request.is_set()

        force_stop.clear()
        assert execute_voice_command("FitBot stop")
        assert force_stop.is_set()
    finally:
        exercise_executing.clear()

    print("✅ All command & conversation execution tests passed!")


if __name__ == "__main__":
    test_home_screen_commands()
    test_exercise_mode_wake_word()
    test_non_commands()
    test_param_extraction()
    test_command_execution()
    print("🎉 All voice control tests completed successfully!")
