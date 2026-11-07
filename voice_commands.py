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
                       single words, so "rotate left" beats "rotate".
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
        FORWARD, BACKWARD, LEFT (or MOVE_LEFT), RIGHT (or MOVE_RIGHT),
        ROTATE, ROTATE_LEFT, ROTATE_RIGHT, FWD_LEFT, FWD_RIGHT,
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

    {"type": "reply", "response": "Hello! The time now is {time}."}
        Speak a conversational response out loud (and show it on screen).
        Supports placeholders {time}, {date}, {day}, or a callable
        function `lambda params, text: "..."`.

    {"type": "serial", "command": "STOP"}
        Send a raw command string to the ESP32.

    {"type": "python", "function": my_function}
        Call your own Python function. It receives (params, text) where
        params = {"duration": .., "count": .., "number": ..} extracted
        from speech, and text = the full recognised sentence.
"""

# ─── Wake-word & Matching Behaviour ───────────────────────────────────

# Primary wake word ("FitBot").
WAKE_WORD = "FitBot"

# When an exercise is actively executing, require the user to say "FitBot"
# followed by the command (e.g., "FitBot move left", "FitBot pause").
REQUIRE_WAKE_WORD_DURING_EXERCISE = True

# If True, ALSO require "FitBot" on the Home screen / main menu.
# (Default False so on the Home screen you can speak with or without "FitBot".)
REQUIRE_WAKE_WORD = False

# Seconds the robot stays awake waiting for the command if the user says
# only "FitBot" and pauses before speaking the command.
WAKE_WORD_WINDOW = 6.0

# Accepted wake-word transcriptions. Because Vosk's compact model splits
# "FitBot" into two words ("fit bot") or close phonetic pairs depending
# on accent and microphone, these aliases are all recognised as "FitBot".
WAKE_WORDS = [
    "fitbot",
    "fit bot",
    "fit but",
    "fit boat",
    "fit bought",
    "fit pot",
    "fit part",
    "fit box",
    "fit bar",
    "fit board",
    "fit ball",
    "feet bot",
    "feed bot",
    "foot bot",
    "fifth bot",
    "bit bot",
    "sit bot",
    "fib bot",
]

# Minimum confidence (0..1) needed before a command is executed.
MIN_CONFIDENCE = 0.40

# Words ignored while matching (politeness, fillers, wake words ...).
FILLER_WORDS = [
    "please", "could", "would", "can", "you", "i", "want", "to", "the", "a",
    "an", "um", "uh", "hmm", "okay", "ok", "hey", "now", "lets", "let",
    "us", "me", "just", "some", "like", "robot", "fitbot", "go", "and",
    "do", "for", "with", "my", "be", "is", "it", "of", "on", "kindly",
]

# Phrases removed before keyword matching because they contain a direction
# word ("right" / "back") in a non-directional sense.
IGNORE_PHRASES = [
    "all right", "alright", "right now", "right there", "right away",
    "that's right", "thats right", "youre right", "you are right",
    "back then", "come back later",
]

# Safety limit for spoken movement durations (seconds).
MAX_MOTION_DURATION = 10.0


# ─── Command List ─────────────────────────────────────────────────────

COMMANDS = [
    # ── 1. Safety ───────────────────────────────────────────────────
    {
        "name": "STOP",
        "description": "Immediately stop all robot movement and stop the current activity.",
        "keywords": ["stop", "halt", "freeze", "brake", "cancel", "stay", "hold on",
                     "stop moving", "stand still", "emergency"],
        "examples": ["stop", "fitbot stop", "stop the robot", "please stop moving",
                     "freeze right there", "hold on stop"],
        "action": {"type": "stop"},
        "priority": 5,
    },

    # ── 2. Movement & Rotation Commands ────────────────────────────
    # ── EXHIBITION STATIONARY MODE: Movement commands commented out for reuse later ──
    # {
    # "name": "ROTATE",
    # "description": "Rotate or spin the robot in place.",
    # "keywords": ["rotate", "spin", "turn around", "rotate around", "spin around",
    # "do a spin", "rotate robot", "turn in place", "circle"],
    # "examples": ["rotate", "fitbot rotate", "spin around", "turn around",
    # "rotate for three seconds"],
    # "action": {"type": "motion", "direction": "ROTATE", "duration": 2.5},
    # "priority": 1,
    # },
    # {
    # "name": "ROTATE_LEFT",
    # "description": "Rotate or turn the robot counter-clockwise (to the left) in place.",
    # "keywords": ["rotate left", "spin left", "turn left", "rotate to the left",
    # "turn to the left", "rotate counter clockwise"],
    # "examples": ["rotate left", "fitbot rotate left", "turn left", "spin to the left"],
    # "action": {"type": "motion", "direction": "ROTATE_LEFT", "duration": 1.8},
    # "priority": 2,
    # },
    # {
    # "name": "ROTATE_RIGHT",
    # "description": "Rotate or turn the robot clockwise (to the right) in place.",
    # "keywords": ["rotate right", "spin right", "turn right", "rotate to the right",
    # "turn to the right", "rotate clockwise"],
    # "examples": ["rotate right", "fitbot rotate right", "turn right", "spin to the right"],
    # "action": {"type": "motion", "direction": "ROTATE_RIGHT", "duration": 1.8},
    # "priority": 2,
    # },
    # {
    # "name": "MOVE_LEFT",
    # "description": "Move / strafe the robot sideways to the left.",
    # "keywords": ["move left", "go left", "step left", "slide left", "strafe left",
    # "shift left", "move to the left", "left"],
    # "examples": ["move left", "fitbot move left", "move to the left",
    # "go left a bit", "slide left"],
    # "action": {"type": "motion", "direction": "MOVE_LEFT", "duration": 1.5},
    # "priority": 1,
    # },
    # {
    # "name": "MOVE_RIGHT",
    # "description": "Move / strafe the robot sideways to the right.",
    # "keywords": ["move right", "go right", "step right", "slide right", "strafe right",
    # "shift right", "move to the right", "right"],
    # "examples": ["move right", "fitbot move right", "move to the right",
    # "go right a bit", "slide right"],
    # "action": {"type": "motion", "direction": "MOVE_RIGHT", "duration": 1.5},
    # "priority": 1,
    # },
    # {
    # "name": "FORWARD",
    # "description": "Drive the robot forward for a short time.",
    # "keywords": ["forward", "forwards", "move forward", "go forward", "ahead",
    # "front", "come here", "come closer", "closer", "straight"],
    # "examples": ["go forward", "fitbot move forward", "move forward a bit",
    # "come closer to me", "drive ahead for three seconds"],
    # "action": {"type": "motion", "direction": "FORWARD", "duration": 2.5},
    # },
    # {
    # "name": "BACKWARD",
    # "description": "Drive the robot backward for a short time.",
    # "keywords": ["back", "backward", "backwards", "move backward", "go backward",
    # "move back", "go back", "reverse", "retreat", "move away", "go away"],
    # "examples": ["go back", "fitbot move backward", "move backward",
    # "reverse a little", "move away from me"],
    # "action": {"type": "motion", "direction": "BACKWARD", "duration": 2.5},
    # },
    # {
    # "name": "FWD_LEFT",
    # "description": "Drive diagonally forward and to the left.",
    # "keywords": ["forward left", "front left", "diagonal left", "ahead left"],
    # "examples": ["go forward left", "move diagonally to the front left"],
    # "action": {"type": "motion", "direction": "FWD_LEFT", "duration": 2.0},
    # "priority": 2,
    # },
    # {
    # "name": "FWD_RIGHT",
    # "description": "Drive diagonally forward and to the right.",
    # "keywords": ["forward right", "front right", "diagonal right", "ahead right"],
    # "examples": ["go forward right", "move diagonally to the front right"],
    # "action": {"type": "motion", "direction": "FWD_RIGHT", "duration": 2.0},
    # "priority": 2,
    # },

    # ── 3. Follow Me & Workout Modes ───────────────────────────────
    # ── EXHIBITION STATIONARY MODE: Follow Me commented out for reuse later ──
    # {
    # "name": "FOLLOW_ME",
    # "description": "Start person-following mode where the robot follows the user.",
    # "keywords": ["follow me", "follow", "track me", "tracking", "start following",
    # "come with me", "come along", "walk with me"],
    # "examples": ["follow me", "fitbot follow me", "please follow me around",
    # "come with me", "start tracking me"],
    # "action": {"type": "mode", "mode": "PERSON_FOLLOW"},
    # "priority": 2,
    # },
    {
        "name": "BICEP_CURLS",
        "description": "Open the bicep curls exercise.",
        "keywords": ["bicep", "biceps", "curl", "curls", "bicep curls", "arm curls", "arms"],
        "examples": ["start bicep curls", "fitbot start bicep curls",
                     "i want to train my biceps", "let's do some curls"],
        "action": {"type": "mode", "mode": "BICEP_CURLS"},
    },
    {
        "name": "PUSHUPS",
        "description": "Open the push-ups exercise.",
        "keywords": ["push ups", "push up", "pushups", "pushup", "press ups", "chest"],
        "examples": ["start push ups", "fitbot start pushups",
                     "let's do pushups", "i want to do some press ups"],
        "action": {"type": "mode", "mode": "PUSHUPS"},
    },
    {
        "name": "SQUATS",
        "description": "Open the squats exercise.",
        "keywords": ["squat", "squats", "legs", "leg day"],
        "examples": ["start squats", "fitbot start squats",
                     "let's do squats", "time for leg day"],
        "action": {"type": "mode", "mode": "SQUATS"},
    },
    {
        "name": "PLANK",
        "description": "Open the plank exercise.",
        "keywords": ["plank", "planks", "core", "planking"],
        "examples": ["start plank", "fitbot start plank",
                     "plank exercise", "let's do a plank"],
        "action": {"type": "mode", "mode": "PLANK"},
    },

    # ── 4. Session / UI Control ────────────────────────────────────
    {
        "name": "START",
        "description": "Begin the currently selected exercise (press the Start button).",
        "keywords": ["start", "begin", "ready", "i am ready", "im ready",
                     "start workout", "start exercise", "start the exercise",
                     "start the workout", "begin workout"],
        "examples": ["start", "fitbot start", "i'm ready",
                     "begin the workout", "start the exercise"],
        "action": {"type": "start"},
        # Negative priority so "start squats" opens SQUATS rather than pressing Start.
        "priority": -1,
    },
    {
        "name": "PAUSE",
        "description": "Pause the current exercise.",
        "keywords": ["pause", "break", "take a break", "rest", "wait", "pause workout"],
        "examples": ["pause", "fitbot pause", "i need a break",
                     "pause the workout", "let me rest"],
        "action": {"type": "pause"},
        "priority": 1,
    },
    {
        "name": "RESUME",
        "description": "Resume a paused exercise.",
        "keywords": ["resume", "continue", "unpause", "carry on", "keep going"],
        "examples": ["resume", "fitbot resume", "continue the workout",
                     "let's keep going"],
        "action": {"type": "resume"},
        "priority": 1,
    },
    {
        "name": "HOME",
        "description": "Stop the current activity and go back to the main menu.",
        "keywords": ["home", "menu", "main menu", "go home", "exit", "quit", "finish",
                     "end workout", "i am done", "im done", "done"],
        "examples": ["go home", "fitbot go home", "back to the main menu",
                     "i'm done", "end the workout"],
        "action": {"type": "home"},
        "priority": 2,
    },
    {
        "name": "MUTE",
        "description": "Mute robot audio and voice feedbacks.",
        "keywords": ["mute", "be quiet", "silence", "mute voice", "turn off sound", "shut up"],
        "examples": ["mute", "fitbot mute", "be quiet", "mute feedback", "turn off voice"],
        "action": {"type": "mute"},
        "priority": 2,
    },
    {
        "name": "UNMUTE",
        "description": "Unmute robot audio and voice feedbacks.",
        "keywords": ["unmute", "turn on sound", "enable voice", "speak up", "unmute voice"],
        "examples": ["unmute", "fitbot unmute", "turn on sound", "enable feedback"],
        "action": {"type": "unmute"},
        "priority": 2,
    },

    # ── 5. Normal / Conversational Voice Commands ──────────────────
    {
        "name": "HELLO",
        "description": "Greet the user when they say hello or hi to FitBot.",
        "keywords": ["hello", "hello fitbot", "hi", "hi fitbot", "hey fitbot",
                     "greetings", "good morning", "good afternoon", "good evening"],
        "examples": ["hello", "hello fitbot", "hi fitbot",
                     "good morning fitbot", "hey there"],
        "action": {
            "type": "reply",
            "response": "Hello! I am FitBot, your fitness assistant. How can I help you today?",
            "audio_file": "017_hello_fitbot.mp3",
        },
        "priority": 1,
    },
    {
        "name": "TIME_NOW",
        "description": "Tell the user the current time.",
        "keywords": ["whats the time now", "what is the time now", "whats the time",
                     "what is the time", "what time is it", "what time", "time now",
                     "current time", "tell me the time", "the time now", "what the time"],
        "examples": ["what's the time now", "fitbot what's the time now",
                     "what time is it", "tell me the time"],
        "action": {
            "type": "reply",
            "response": "The time now is {time}.",
        },
        "priority": 3,
    },
    {
        "name": "DATE_TODAY",
        "description": "Tell the user today's day and date.",
        "keywords": ["what is the date", "whats the date", "what day is it",
                     "what is today", "todays date", "date today", "what day is today"],
        "examples": ["what is the date today", "what day is it",
                     "fitbot what is today's date"],
        "action": {
            "type": "reply",
            "response": "Today is {day}, {date}.",
        },
        "priority": 2,
    },
    {
        "name": "HOW_ARE_YOU",
        "description": "Respond when the user asks how FitBot is doing.",
        "keywords": ["how are you", "how are you doing", "how is it going",
                     "hows it going", "how do you feel", "are you okay"],
        "examples": ["how are you", "hello fitbot how are you",
                     "how are you doing today"],
        "action": {
            "type": "reply",
            "response": "I am doing great and ready for a workout! How are you feeling?",
            "audio_file": "018_how_are_you.mp3",
        },
        "priority": 2,
    },
    {
        "name": "WHO_ARE_YOU",
        "description": "Explain who FitBot is and what it can do.",
        "keywords": ["who are you", "what is your name", "whats your name",
                     "your name", "what can you do", "introduce yourself"],
        "examples": ["who are you", "what is your name", "fitbot what can you do"],
        "action": {
            "type": "reply",
            "response": "I am FitBot, your smart fitness robot. I can track your exercises, follow you, and move on your voice command.",
            "audio_file": "019_who_are_you.mp3",
        },
        "priority": 2,
    },
    {
        "name": "THANK_YOU",
        "description": "Respond politely when the user says thank you.",
        "keywords": ["thank you", "thanks", "thanks a lot", "thank you fitbot",
                     "appreciate it"],
        "examples": ["thank you", "thank you fitbot", "thanks a lot"],
        "action": {
            "type": "reply",
            "response": "You are welcome! Keep up the great workout!",
            "audio_file": "020_thank_you.mp3",
        },
        "priority": 1,
    },
    {
        "name": "GOODBYE",
        "description": "Say goodbye to the user.",
        "keywords": ["goodbye", "bye", "bye bye", "see you", "see you later", "good night"],
        "examples": ["goodbye", "bye fitbot", "see you later"],
        "action": {
            "type": "reply",
            "response": "Goodbye! Stay healthy and see you next workout!",
            "audio_file": "021_goodbye.mp3",
        },
        "priority": 1,
    },
]
