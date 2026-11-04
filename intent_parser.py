#!/usr/bin/env python3
"""
Intent extraction for spoken robot commands.

Turns a free-form spoken sentence (from Vosk) into one command defined in
`voice_commands.py`, plus parameters such as durations and counts.

Wake-word rule:
  • While an exercise is executing (`session_manager.exercise_executing` is set),
    the robot only responds when the keyword "FitBot" is spoken and then the
    command follows (e.g. "FitBot move left", "FitBot pause", "FitBot what's
    the time now", plus "Hello FitBot" or saying "FitBot" and then speaking
    the command within WAKE_WORD_WINDOW seconds).
  • When no exercise is executing (Home screen), commands work both with and
    without the "FitBot" prefix.

Two backends:
  1. LLM backend (optional) – a local Large Language Model served by Ollama
     (https://ollama.com). Enabled automatically when Ollama is running and
     the configured model is pulled (see VOICE_LLM_* in config.py).
  2. Local semantic matcher (always available, instant, offline) –
     keyword/phrase scoring with fuzzy matching, negation handling,
     example-sentence similarity, number-word parsing and parameter extraction.
"""

import os
import re
import sys
import json
import time
import importlib
import urllib.request
from difflib import SequenceMatcher
from dataclasses import dataclass, field

try:
    from config import (
        VOICE_LLM_MODE,
        VOICE_LLM_URL,
        VOICE_LLM_MODEL,
        VOICE_LLM_TIMEOUT,
        DEBUG,
    )
except Exception:  # pragma: no cover - allows standalone use
    VOICE_LLM_MODE, VOICE_LLM_URL = "off", "http://localhost:11434"
    VOICE_LLM_MODEL, VOICE_LLM_TIMEOUT, DEBUG = "qwen2.5:0.5b", 6.0, False

try:
    from session_manager import exercise_executing
except Exception:  # pragma: no cover
    exercise_executing = None

import voice_commands as _registry_module


# ─── Result type ──────────────────────────────────────────────────────
@dataclass
class Intent:
    name: str
    confidence: float
    params: dict = field(default_factory=dict)
    source: str = "local"          # "local" or "llm"
    command: dict = field(default_factory=dict)

    def __repr__(self):
        return (f"Intent({self.name}, conf={self.confidence:.2f}, "
                f"params={self.params}, source={self.source})")


# ─── Registry hot-reload ──────────────────────────────────────────────
_registry_mtime = None


def get_registry():
    """Return the voice_commands module, reloading it if the file changed."""
    global _registry_module, _registry_mtime
    try:
        path = _registry_module.__file__
        mtime = os.path.getmtime(path)
        if _registry_mtime is None:
            _registry_mtime = mtime
        elif mtime != _registry_mtime:
            _registry_module = importlib.reload(_registry_module)
            _registry_mtime = mtime
            print("🔄 [VOICE] voice_commands.py changed - commands reloaded.")
    except Exception as e:
        print(f"⚠️ [VOICE] Could not reload voice_commands.py: {e}", file=sys.stderr)
    return _registry_module


def get_commands():
    reg = get_registry()
    return [c for c in getattr(reg, "COMMANDS", []) if c.get("enabled", True)]


# ─── Text normalisation ───────────────────────────────────────────────
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NEGATIONS = {"dont", "not", "never", "no", "without"}
_GREETING_PREFIXES = {
    "hello", "hi", "hey", "greetings",
    "good morning", "good afternoon", "good evening",
}


def _words_to_numbers(tokens):
    """['go', 'twenty', 'five', 'seconds'] -> ['go', '25', 'seconds']"""
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in _TENS:
            val = _TENS[t]
            if i + 1 < len(tokens) and tokens[i + 1] in _UNITS and _UNITS[tokens[i + 1]] < 10:
                val += _UNITS[tokens[i + 1]]
                i += 1
            out.append(str(val))
        elif t in _UNITS:
            out.append(str(_UNITS[t]))
        elif t == "half" and out and out[-1] in ("a", "an"):
            out[-1] = "0.5"
        else:
            out.append(t)
        i += 1
    return out


def normalize(text):
    """Lower-case, strip punctuation, convert number words to digits."""
    text = (text or "").lower()
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[-_/]", " ", text)
    text = re.sub(r"[^a-z0-9. ]+", " ", text)
    tokens = [t.strip(".") for t in text.split() if t.strip(".")]
    return " ".join(_words_to_numbers(tokens))


def _tokens(text):
    return text.split()


# ─── Wake-word ("FitBot") detection & two-step window ─────────────────
_wake_word_active_until = 0.0


def activate_wake_word_window(duration=None):
    """Open the wake-word window so the next utterance is accepted as a command."""
    global _wake_word_active_until
    reg = get_registry()
    win = float(duration if duration is not None else getattr(reg, "WAKE_WORD_WINDOW", 6.0))
    _wake_word_active_until = time.time() + win


def clear_wake_word_window():
    """Close any active wake-word window."""
    global _wake_word_active_until
    _wake_word_active_until = 0.0


def is_wake_word_window_active():
    """Return True if the user recently said 'FitBot' and we are awaiting the command."""
    return time.time() < _wake_word_active_until


def _is_fuzzy_fitbot(window_tokens):
    """Check if a 1- or 2-word token window is a phonetic match for 'fitbot'."""
    joined = "".join(window_tokens)
    if len(joined) < 5 or len(joined) > 8:
        return False
    if not (joined[0] in ("f", "b", "s") or joined.endswith(("bot", "but", "pot", "box", "bar"))):
        return False
    return SequenceMatcher(None, joined, "fitbot").ratio() >= 0.82


def split_on_wake_word(text):
    """
    Search `text` for the wake word ('FitBot' or its Vosk phonetic aliases).
    Returns tuple: (found: bool, before_wake: str, after_wake: str).
    """
    reg = get_registry()
    norm = normalize(text)
    tokens = _tokens(norm)
    if not tokens:
        return False, "", ""

    wake_phrases = [_tokens(normalize(w)) for w in getattr(reg, "WAKE_WORDS", ["fitbot", "fit bot"])]
    wake_phrases = [p for p in wake_phrases if p]
    # Sort longest first so 2-word "fit bot" matches completely
    wake_phrases.sort(key=len, reverse=True)

    for i in range(len(tokens)):
        # 1. Exact wake-word or alias match
        for wp in wake_phrases:
            n = len(wp)
            if i + n <= len(tokens) and tokens[i:i + n] == wp:
                before = " ".join(tokens[:i]).strip()
                after = " ".join(tokens[i + n:]).strip()
                return True, before, after

        # 2. Fuzzy phonetic match for 1- or 2-token transcriptions of "FitBot"
        for n in (2, 1):
            if i + n <= len(tokens) and _is_fuzzy_fitbot(tokens[i:i + n]):
                before = " ".join(tokens[:i]).strip()
                after = " ".join(tokens[i + n:]).strip()
                return True, before, after

    return False, norm, ""


def has_wake_word(text):
    """Return True if `text` contains the wake word 'FitBot'."""
    found, _, _ = split_on_wake_word(text)
    return found


# ─── Parameter extraction ─────────────────────────────────────────────
def extract_params(norm_text):
    params = {}
    m = re.search(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s)\b", norm_text)
    if m:
        params["duration"] = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?)\b", norm_text)
        if m:
            params["duration"] = float(m.group(1)) * 60.0
    m = re.search(r"(\d+)\s*(reps?|repetitions?|times|sets?)\b", norm_text)
    if m:
        params["count"] = int(m.group(1))
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", norm_text)
    if m:
        params["number"] = float(m.group(1))
    if re.search(r"\b(a (little|bit)|slightly|little bit|tiny)\b", norm_text):
        params["scale"] = 0.5
    elif re.search(r"\b(a lot|far|long way|much more)\b", norm_text):
        params["scale"] = 2.0
    return params


# ─── Local semantic matcher ───────────────────────────────────────────
def _find_phrase(tokens, phrase_tokens):
    """Return (start_index, match_quality) of a phrase in tokens, or (None, 0)."""
    n = len(phrase_tokens)
    if n == 0 or n > len(tokens) + 1:
        return None, 0.0
    target = " ".join(phrase_tokens)
    joined_target = "".join(phrase_tokens)

    # Exact match on n-gram windows
    for i in range(len(tokens) - n + 1):
        if tokens[i:i + n] == phrase_tokens:
            return i, 1.0

    # Compound-word match: "push ups" <-> "pushups"
    for k in (1, 2, 3):
        for i in range(len(tokens) - k + 1):
            if "".join(tokens[i:i + k]) == joined_target:
                return i, 1.0

    # Fuzzy match (tolerates recognition errors like "squads" -> "squats").
    # Only for longer keywords: short words ("right"/"light") are too risky.
    if len(joined_target) >= 6:
        best_i, best_r = None, 0.0
        for i in range(len(tokens) - n + 1):
            window = " ".join(tokens[i:i + n])
            r = SequenceMatcher(None, window, target).ratio()
            if r > best_r:
                best_i, best_r = i, r
        if best_r >= 0.80:
            return best_i, 0.75
    return None, 0.0


def _is_negated(tokens, start):
    """True if the word just before `start` negates it ("don't stop", "do not stop")."""
    if not start:
        return False
    return tokens[start - 1] in _NEGATIONS


def _example_similarity(content_tokens, examples, fillers):
    if not examples or not content_tokens:
        return 0.0
    text = " ".join(content_tokens)
    a = set(content_tokens)
    best = 0.0
    for ex in examples:
        ex_tokens = [t for t in _tokens(normalize(ex)) if t not in fillers]
        if not ex_tokens:
            continue
        b = set(ex_tokens)
        jacc = len(a & b) / len(a | b)
        seq = SequenceMatcher(None, text, " ".join(ex_tokens)).ratio()
        best = max(best, 0.5 * jacc + 0.5 * seq)
    return best


def _score_command(cmd, tokens, content_tokens, fillers):
    hits = []
    for kw in cmd.get("keywords", []):
        kw_tokens = _tokens(normalize(kw))
        start, quality = _find_phrase(tokens, kw_tokens)
        if start is None or _is_negated(tokens, start):
            continue
        hits.append(quality * len(kw_tokens))
    hits.sort(reverse=True)
    kw_score = (hits[0] + 0.3 * sum(hits[1:])) if hits else 0.0
    kw_component = 1.0 - (0.4 ** kw_score)
    ex_sim = _example_similarity(content_tokens, cmd.get("examples", []), fillers)
    confidence = max(0.8 * kw_component + 0.2 * ex_sim, 0.9 * ex_sim if ex_sim >= 0.75 else 0.0)
    return min(1.0, confidence), kw_score


def local_parse(text, return_all=False):
    """Rule-based semantic intent matcher (offline, instant)."""
    reg = get_registry()
    fillers = set(getattr(reg, "FILLER_WORDS", []))
    norm = normalize(text)
    for phrase in getattr(reg, "IGNORE_PHRASES", []):
        norm = re.sub(rf"\b{re.escape(normalize(phrase))}\b", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    tokens = _tokens(norm)
    content_tokens = [t for t in tokens if t not in fillers]

    scored = []
    for cmd in get_commands():
        conf, kw_score = _score_command(cmd, tokens, content_tokens, fillers)
        if conf <= 0:
            continue
        rank = conf + 0.01 * cmd.get("priority", 0)
        scored.append((rank, conf, cmd))
    scored.sort(key=lambda s: s[0], reverse=True)

    if return_all:
        return [(c["name"], round(conf, 3)) for _, conf, c in scored]
    if not scored:
        return None
    _, conf, cmd = scored[0]
    return Intent(cmd["name"], conf, extract_params(norm), "local", cmd)


# ─── LLM backend (Ollama) ─────────────────────────────────────────────
_llm_available = None
_llm_checked_at = 0.0
_LLM_RECHECK_SECONDS = 60.0


def llm_available():
    """Check (cached) whether Ollama is running with the configured model."""
    global _llm_available, _llm_checked_at
    mode = str(VOICE_LLM_MODE).lower()
    if mode == "off":
        return False
    if _llm_available is not None and time.time() - _llm_checked_at < _LLM_RECHECK_SECONDS:
        return _llm_available
    _llm_checked_at = time.time()
    try:
        with urllib.request.urlopen(f"{VOICE_LLM_URL}/api/tags", timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = {m.get("name", "") for m in data.get("models", [])}
        wanted = VOICE_LLM_MODEL
        ok = wanted in names or f"{wanted}:latest" in names or any(
            n.split(":")[0] == wanted.split(":")[0] for n in names)
        if not ok and _llm_available is not False:
            print(f"ℹ️ [VOICE] Ollama running but model '{wanted}' not pulled. "
                  f"Run: ollama pull {wanted}")
        _llm_available = ok
    except Exception:
        if mode == "on" and _llm_available is not False:
            print(f"⚠️ [VOICE] LLM mode 'on' but Ollama not reachable at {VOICE_LLM_URL}")
        _llm_available = False
    return _llm_available


def _build_prompt(text, commands):
    lines = [f"- {c['name']}: {c.get('description', '')}" for c in commands]
    return (
        "You are FitBot, a voice-controlled fitness robot. A user spoke a "
        "sentence (transcribed by speech recognition, it may contain errors). "
        "Decide which ONE command the user wants, or NONE if the sentence is "
        "not a command.\n\n"
        "Available commands:\n" + "\n".join(lines) + "\n- NONE: not a command\n\n"
        "Reply ONLY with JSON in this exact format:\n"
        '{"command": "<COMMAND_NAME or NONE>", "duration": <seconds or null>, '
        '"count": <number or null>, "confidence": <0.0-1.0>}\n\n'
        f'Sentence: "{text}"\nJSON:'
    )


def llm_parse(text):
    """Ask the local LLM to extract the command. Returns Intent, 'NONE' or None on error."""
    commands = get_commands()
    payload = {
        "model": VOICE_LLM_MODEL,
        "prompt": _build_prompt(text, commands),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0, "num_predict": 60},
        "keep_alive": "30m",
    }
    req = urllib.request.Request(
        f"{VOICE_LLM_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=VOICE_LLM_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8")).get("response", "")
        result = json.loads(raw)
        if DEBUG:
            print(f"🧠 [LLM] {result} ({time.time() - t0:.2f}s)")
    except Exception as e:
        print(f"⚠️ [LLM] Request failed ({e}); using local matcher.", file=sys.stderr)
        return None

    name = str(result.get("command", "NONE")).strip().upper().replace(" ", "_")
    if name in ("NONE", "", "NULL"):
        return "NONE"
    cmd = next((c for c in commands if c["name"] == name), None)
    if cmd is None:
        return None

    params = extract_params(normalize(text))
    for key in ("duration", "count"):
        val = result.get(key)
        if isinstance(val, (int, float)) and val > 0 and key not in params:
            params[key] = float(val) if key == "duration" else int(val)
    try:
        conf = float(result.get("confidence", 0.8))
    except (TypeError, ValueError):
        conf = 0.8
    return Intent(name, max(0.0, min(1.0, conf)), params, "llm", cmd)


def _match_text_to_intent(candidate_text, min_conf):
    """Run local + optional LLM matching on `candidate_text`."""
    local = local_parse(candidate_text)

    # Safety fast-path: high-priority commands (STOP) skip the LLM entirely.
    if local and local.command.get("priority", 0) >= 5 and local.confidence >= 0.6:
        return local

    if llm_available():
        llm = llm_parse(candidate_text)
        if isinstance(llm, Intent) and llm.confidence >= min_conf:
            return llm
        if llm == "NONE":
            return local if (local and local.confidence >= 0.75) else None

    if local and local.confidence >= min_conf:
        return local
    return None


def _get_hello_intent():
    """Return the HELLO command Intent if defined in voice_commands.py."""
    for cmd in get_commands():
        if cmd.get("name") == "HELLO":
            return Intent("HELLO", 0.95, {}, "local", cmd)
    return Intent(
        "HELLO",
        0.95,
        {},
        "local",
        {
            "name": "HELLO",
            "action": {
                "type": "reply",
                "response": "Hello! I am FitBot, your fitness assistant. How can I help you today?",
            },
        },
    )


def _get_wakeup_intent():
    """Return a wake-word acknowledgement Intent when the user says only 'FitBot'."""
    return Intent(
        "WAKE_UP",
        1.0,
        {},
        "local",
        {
            "name": "WAKE_UP",
            "action": {
                "type": "reply",
                "response": "Yes? I am listening.",
            },
        },
    )


# ─── Public API ───────────────────────────────────────────────────────
def extract_intent(text, exercising=None):
    """
    Extract the intended command from a spoken sentence.
    When `exercising` is True (or `session_manager.exercise_executing` is set),
    requires the keyword "FitBot" followed by the command.
    Returns an Intent, or None if no command was understood.
    """
    if not text or not text.strip():
        return None

    reg = get_registry()
    min_conf = float(getattr(reg, "MIN_CONFIDENCE", 0.4))

    if exercising is None:
        exercising = bool(exercise_executing is not None and exercise_executing.is_set())

    require_wake = (
        (exercising and getattr(reg, "REQUIRE_WAKE_WORD_DURING_EXERCISE", True))
        or bool(getattr(reg, "REQUIRE_WAKE_WORD", False))
    )

    found_wake, before_wake, after_wake = split_on_wake_word(text)

    if found_wake:
        clear_wake_word_window()
        # 1. Command follows "FitBot": e.g. "FitBot move left", "FitBot what's the time now"
        if after_wake:
            return _match_text_to_intent(after_wake, min_conf)

        # 2. Nothing after "FitBot": check if it was "Hello FitBot" / "Hi FitBot" or bare "FitBot"
        before_clean = before_wake.strip()
        if not before_clean:
            activate_wake_word_window()
            return _get_wakeup_intent() if require_wake else _get_hello_intent()

        if before_clean in _GREETING_PREFIXES or any(
            before_clean.endswith(g) for g in _GREETING_PREFIXES
        ):
            return _get_hello_intent()

        # When wake word is required (during exercise), commands BEFORE "FitBot"
        # (like "move left FitBot") are ignored because the command must follow "FitBot".
        if require_wake:
            if DEBUG:
                print(f"🔇 [VOICE] Command must follow '{getattr(reg, 'WAKE_WORD', 'FitBot')}': \"{text}\"")
            return None

        return _match_text_to_intent(before_clean, min_conf)

    # Wake word was not in this sentence: check if user said "FitBot" just before this
    if is_wake_word_window_active():
        clear_wake_word_window()
        return _match_text_to_intent(text, min_conf)

    if require_wake:
        if DEBUG:
            print(f"🔇 [VOICE] Ignored during exercise (say '{getattr(reg, 'WAKE_WORD', 'FitBot')} <command>'): \"{text}\"")
        return None

    return _match_text_to_intent(text, min_conf)


if __name__ == "__main__":
    # Interactive tester: python intent_parser.py
    print(f"LLM available: {llm_available()} (mode={VOICE_LLM_MODE}, model={VOICE_LLM_MODEL})")
    print("Type a sentence (empty line to quit):")
    while True:
        try:
            s = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not s:
            break
        print("  [Home mode]    ->", extract_intent(s, exercising=False))
        print("  [Exercise mode]->", extract_intent(s, exercising=True))
        print("  ranking:", local_parse(s, return_all=True)[:4])
