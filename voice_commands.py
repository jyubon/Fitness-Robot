#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║            EDITABLE VOICE COMMAND REGISTRY  (Fitness Robot)          ║
╚══════════════════════════════════════════════════════════════════════╝

Add, remove or edit voice commands here. No other file needs to change.
Changes are picked up automatically while the robot is running
(the file is hot-reloaded the next time someone speaks).

HOW A COMMAND IS DEFINED
────────────────────────
    {
        "name":        Unique ID in UPPER_SNAKE_CASE.
        "description": Plain-English meaning. Used by the LLM backend to
                       understand what the command does.
        "keywords":    Words / short phrases that strongly signal this
                       command. Multi-word phrases score higher than
                       single words, so "forward left" beats "forward".
        "examples":    Full example sentences people might say. They help
                       both the local matcher and the LLM.
        "action":      What the robot does (see ACTION TYPES below).
        "priority":    (optional) Tie-breaker, higher wins. Default 0.
        "enabled":     (optional) Set False to disable. Default True.
    }

ACTION TYPES
────────────
    {"type": "stop"}
        Stop motors immediately and stop the running session.

    {"type": "motion", "direction": "FORWARD", "duration": 2.5}
        Drive for `duration` seconds then stop. Valid directions:
        FORWARD, BACKWARD, LEFT, RIGHT, FWD_LEFT, FWD_RIGHT,
        BWD_LEFT, BWD_RIGHT, SEARCH.
        A spoken duration overrides it: "go forward for 4 seconds".
        "a little" halves it, "a lot" doubles it.

    {"type": "mode", "mode": "SQUATS"}
        Open an exercise / mode screen. Valid modes:
        BICEP_CURLS, PUSHUPS, SQUATS, PLANK, PERSON_FOLLOW.

    {"type": "start"}     Press the Start button of the current exercise.
    {"type": "pause"}     Pause the current exercise.
    {"type": "resume"}    Resume the current exercise.
    {"type": "home"}      Stop everything and return to the home screen.

    {"type": "serial", "command": "STOP"}
        Send a raw command string to the ESP32.

    {"type": "python", "function": my_function}
        Call your own Python function. It receives (params, text) where
        params = {"duration": .., "count": .., "number": ..} extracted
        from speech, and text = the full recognised sentence.
"""

# ─── Matching behaviour ───────────────────────────────────────────────

# Minimum confidence (0..1) needed before a command is executed.
# Raise it if the robot reacts to random conversation, lower it if it
# misses commands.
MIN_CONFIDENCE = 0.40

# If True, a sentence is only treated as a command when it contains one
# of the WAKE_WORDS (e.g. "robot, go forward").
REQUIRE_WAKE_WORD = False
WAKE_WORDS = ["robot", "fitbot", "fit bot", "fitness robot", "hey robot"]

# Words ignored while matching (politeness, fillers, wake words ...).
FILLER_WORDS = [
    "please", "could", "would", "can", "you", "i", "want", "to", "the", "a",
    "an", "um", "uh", "hmm", "okay", "ok", "hey", "now", "lets", "let",
    "us", "me", "just", "some", "like", "robot", "fitbot", "go", "and",
    "do", "for", "with", "my", "be", "is", "it", "of", "on", "kindly",
]

# Phrases removed before keyword matching because they contain a keyword
# but mean something else ("all right" is not the direction RIGHT).
IGNORE_PHRASES = [
    "all right", "alright", "right now", "right there", "right away",
    "that's right", "thats right", "youre right", "you are right",
    "back then", "come back later",
]

# Safety limit for spoken movement durations (seconds).
MAX_MOTION_DURATION = 10.0


# ─── Command list ─────────────────────────────────────────────────────

COMMANDS = [
    # ── Safety ──────────────────────────────────────────────────────
    {
        "name": "STOP",
        "description": "Immediately stop all robot movement and stop the current activity.",
        "keywords": ["stop", "halt", "freeze", "brake", "cancel", "stay", "hold on",
                     "stop moving", "stand still", "emergency"],
        "examples": ["stop", "stop the robot", "please stop moving", "freeze right there",
                     "hold on stop"],
        "action": {"type": "stop"},
        "priority": 5,
    },

    # ── Exercises / modes ──────────────────────────────────────────
    {
        "name": "BICEP_CURLS",
        "description": "Open the bicep curls exercise.",
        "keywords": ["bicep", "biceps", "curl", "curls", "bicep curls", "arm curls", "arms"],
        "examples": ["start bicep curls", "i want to train my biceps", "let's do some curls",
                     "arm workout"],
        "action": {"type": "mode", "mode": "BICEP_CURLS"},
    },
    {
        "name": "PUSHUPS",
        "description": "Open the push-ups exercise.",
        "keywords": ["push ups", "push up", "pushups", "pushup", "press ups", "chest"],
        "examples": ["start push ups", "let's do pushups", "i want to do some press ups",
                     "chest workout"],
        "action": {"type": "mode", "mode": "PUSHUPS"},
    },
    {
        "name": "SQUATS",
        "description": "Open the squats exercise.",
        "keywords": ["squat", "squats", "legs", "leg day"],
        "examples": ["start squats", "let's do squats", "time for leg day",
                     "i want to train my legs"],
        "action": {"type": "mode", "mode": "SQUATS"},
    },
    {
        "name": "PLANK",
        "description": "Open the plank exercise.",
        "keywords": ["plank", "planks", "core", "planking"],
        "examples": ["start plank", "plank exercise", "let's do a plank",
                     "core workout"],
        "action": {"type": "mode", "mode": "PLANK"},
    },
    {
        "name": "PERSON_FOLLOW",
        "description": "Start person-following mode where the robot follows the user.",
        "keywords": ["follow", "follow me", "track me", "tracking", "come with me",
                     "come along", "walk with me"],
        "examples": ["follow me", "please follow me around", "come with me robot",
                     "start tracking me"],
        "action": {"type": "mode", "mode": "PERSON_FOLLOW"},
    },

    # ── Movement ───────────────────────────────────────────────────
    {
        "name": "FORWARD",
        "description": "Drive the robot forward for a short time.",
        "keywords": ["forward", "forwards", "ahead", "front", "come here",
                     "come closer", "closer", "straight"],
        "examples": ["go forward", "move forward a bit", "come closer to me",
                     "drive ahead for three seconds"],
        "action": {"type": "motion", "direction": "FORWARD", "duration": 2.5},
    },
    {
        "name": "BACKWARD",
        "description": "Drive the robot backward for a short time.",
        "keywords": ["back", "backward", "backwards", "reverse", "retreat",
                     "move away", "go away", "further"],
        "examples": ["go back", "move backward", "reverse a little",
                     "move away from me"],
        "action": {"type": "motion", "direction": "BACKWARD", "duration": 2.5},
    },
    {
        "name": "LEFT",
        "description": "Turn or move the robot to the left.",
        "keywords": ["left", "turn left", "go left", "move left", "rotate left"],
        "examples": ["turn left", "move to the left", "go left a bit"],
        "action": {"type": "motion", "direction": "LEFT", "duration": 1.5},
    },
    {
        "name": "RIGHT",
        "description": "Turn or move the robot to the right.",
        "keywords": ["right", "turn right", "go right", "move right", "rotate right"],
        "examples": ["turn right", "move to the right", "go right a bit"],
        "action": {"type": "motion", "direction": "RIGHT", "duration": 1.5},
    },
    {
        "name": "FWD_LEFT",
        "description": "Drive diagonally forward and to the left.",
        "keywords": ["forward left", "front left", "diagonal left", "ahead left"],
        "examples": ["go forward left", "move diagonally to the front left"],
        "action": {"type": "motion", "direction": "FWD_LEFT", "duration": 2.0},
    },
    {
        "name": "FWD_RIGHT",
        "description": "Drive diagonally forward and to the right.",
        "keywords": ["forward right", "front right", "diagonal right", "ahead right"],
        "examples": ["go forward right", "move diagonally to the front right"],
        "action": {"type": "motion", "direction": "FWD_RIGHT", "duration": 2.0},
    },

    # ── Session / UI control ───────────────────────────────────────
    {
        "name": "START",
        "description": "Begin the currently selected exercise (press the Start button).",
        "keywords": ["start", "begin", "ready", "i am ready", "im ready",
                     "start workout", "start exercise", "start the exercise",
                     "start the workout", "begin workout"],
        "examples": ["start", "i'm ready", "begin the workout", "start the exercise",
                     "okay let's begin"],
        "action": {"type": "start"},
        # Negative priority: "start squats" should open SQUATS, not press Start.
        "priority": -1,
    },
    {
        "name": "PAUSE",
        "description": "Pause the current exercise.",
        "keywords": ["pause", "break", "take a break", "rest", "wait"],
        "examples": ["pause", "i need a break", "pause the workout", "let me rest"],
        "action": {"type": "pause"},
        "priority": 1,
    },
    {
        "name": "RESUME",
        "description": "Resume a paused exercise.",
        "keywords": ["resume", "continue", "unpause", "carry on", "keep going"],
        "examples": ["resume", "continue the workout", "let's keep going"],
        "action": {"type": "resume"},
        "priority": 1,
    },
    {
        "name": "HOME",
        "description": "Stop the current activity and go back to the main menu.",
        "keywords": ["home", "menu", "main menu", "exit", "quit", "finish",
                     "end workout", "i am done", "im done", "done"],
        "examples": ["go home", "back to the main menu", "i'm done",
                     "end the workout", "exit"],
        "action": {"type": "home"},
        "priority": 2,
    },

    # ── Add your own commands below ────────────────────────────────
    # Example of a custom command calling your own Python function:
    #
    # {
    #     "name": "SAY_HELLO",
    #     "description": "Greet the user.",
    #     "keywords": ["hello", "hi", "good morning"],
    #     "examples": ["hello robot", "hi there"],
    #     "action": {"type": "python", "function": lambda params, text: print("👋 Hello!")},
    # },
]
