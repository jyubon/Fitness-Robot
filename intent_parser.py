#!/usr/bin/env python3
"""
Intent extraction for spoken robot commands.

Turns a free-form spoken sentence (from Vosk) into one command defined in
`voice_commands.py`, plus parameters such as durations and counts.

Two backends:
  1. LLM backend (optional) – a local Large Language Model served by Ollama
     (https://ollama.com). Understands paraphrases ("I'm tired, give me a
     minute" -> PAUSE). Enabled automatically when Ollama is running and
     the configured model is pulled (see VOICE_LLM_* in config.py).
  2. Local semantic matcher (always available, instant, offline) –
     keyword/phrase scoring with fuzzy matching (tolerates Vosk
     mis-hearings), negation handling ("don't stop"), example-sentence
     similarity, number-word parsing and parameter extraction.

The LLM is tried first when available; the local matcher is the fallback.
Safety-critical commands (STOP) bypass the LLM for zero latency.
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
        "You are the command interpreter of a fitness robot. A user spoke a "
        "sentence (transcribed by speech recognition, it may contain errors). "
        "Decide which ONE command the user wants, or NONE if the sentence is "
        "not a command (for example normal conversation).\n\n"
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


# ─── Public API ───────────────────────────────────────────────────────
def has_wake_word(text):
    reg = get_registry()
    norm = normalize(text)
    return any(re.search(rf"\b{re.escape(normalize(w))}\b", norm)
               for w in getattr(reg, "WAKE_WORDS", []))


def extract_intent(text):
    """
    Extract the intended command from a spoken sentence.
    Returns an Intent, or None if no command was understood.
    """
    if not text or not text.strip():
        return None
    reg = get_registry()
    min_conf = float(getattr(reg, "MIN_CONFIDENCE", 0.4))

    if getattr(reg, "REQUIRE_WAKE_WORD", False) and not has_wake_word(text):
        if DEBUG:
            print(f"🔇 [VOICE] No wake word in: \"{text}\"")
        return None

    local = local_parse(text)

    # Safety fast-path: high-priority commands (STOP) skip the LLM entirely.
    if local and local.command.get("priority", 0) >= 5 and local.confidence >= 0.6:
        return local

    if llm_available():
        llm = llm_parse(text)
        if isinstance(llm, Intent) and llm.confidence >= min_conf:
            return llm
        if llm == "NONE":
            # LLM says "not a command": only trust the local matcher if it is very sure.
            return local if (local and local.confidence >= 0.75) else None

    if local and local.confidence >= min_conf:
        return local
    return None


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
        print("  ->", extract_intent(s))
        print("  ranking:", local_parse(s, return_all=True)[:4])
