#!/usr/bin/env python3
"""
Generate and permanently cache FitBot voice clips on the SD card (`voice/tts_cache/`).

Supports:
  • Local XTTS-v2 Voice Cloning trained from the existing `voice/001`–`016.mp3`
    ElevenLabs recordings of Voice ID `6psAnGNeDguzLyTxKYvI` (100% free & offline).
  • ElevenLabs API (if a paid API plan is configured).

Voice configuration (matches ElevenLabs studio settings):
  • Voice ID:         6psAnGNeDguzLyTxKYvI
  • Model:            Eleven Multilingual v2 / XTTS-v2 Local Voice Clone
  • Speed:            0.9
  • Stability:        50% (0.50)
  • Similarity boost: 75% (0.75)
  • Style:            6%  (0.06)
  • Speaker boost:    Enabled (True)

Usage:
  1. Generate & play a single example audio clip to verify voice settings:
       .\.venv\Scripts\python.exe generate_tts_audio.py --sample

  2. Pre-generate & permanently cache all static voice responses on the SD card:
       .\.venv\Scripts\python.exe generate_tts_audio.py --all

  3. Generate & cache a specific custom phrase:
       .\.venv\Scripts\python.exe generate_tts_audio.py --text "Let's begin squats!"
"""

import os
import sys
import argparse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import (
    ELEVENLABS_VOICE_ID,
    ELEVENLABS_MODEL_ID,
    TTS_CACHE_DIR,
)
from audio_manager import (
    get_or_create_tts_mp3,
    get_elevenlabs_voice_settings,
    build_reference_voice_wav,
    _play_mp3_windows,
    _play_mp3_linux,
)
import voice_commands

SAMPLE_TEXT = (
    "Hello! I am FitBot, your fitness assistant. "
    "Let's start the workout and keep your form steady!"
)


def collect_static_phrases():
    """Collect static spoken replies from voice_commands.py for permanent SD caching."""
    phrases = []
    seen = set()

    for cmd in getattr(voice_commands, "COMMANDS", []):
        action = cmd.get("action") or {}
        if str(action.get("type", "")).lower() == "reply":
            resp = action.get("response")
            if isinstance(resp, str) and "{" not in resp:
                clean = resp.strip()
                if clean and clean.lower() not in seen:
                    seen.add(clean.lower())
                    phrases.append((cmd.get("name", "REPLY"), clean))

    return phrases


def play_clip_sync(mp3_path):
    """Play a generated MP3 clip synchronously for previewing."""
    if os.name == "nt":
        return _play_mp3_windows(str(mp3_path))
    return _play_mp3_linux(str(mp3_path))


def main():
    parser = argparse.ArgumentParser(
        description="Generate FitBot TTS audio clips and store them permanently in voice/tts_cache/."
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Generate an example audio clip to verify the voice settings.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Pre-generate and permanently cache all static conversational replies in voice_commands.py.",
    )
    parser.add_argument(
        "--text",
        type=str,
        default="",
        help="Custom text phrase to generate and cache.",
    )
    parser.add_argument(
        "--local-clone",
        action="store_true",
        default=True,
        help="Clone Voice ID 6psAnGNeDguzLyTxKYvI locally from voice/001-016.mp3 using XTTS-v2.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate audio even if already present in voice/tts_cache/.",
    )
    parser.add_argument(
        "--no-play",
        action="store_true",
        help="Do not play the generated sample clip out loud.",
    )
    args = parser.parse_args()

    # Default to --sample if no mode flag was specified
    if not args.sample and not args.all and not args.text:
        args.sample = True

    settings = get_elevenlabs_voice_settings()
    ref_wav = build_reference_voice_wav(TTS_CACHE_DIR)
    print("============================================================")
    print("  FitBot TTS Generator & Permanent SD Cache")
    print(f"  • Voice ID         : {ELEVENLABS_VOICE_ID} (cloned from voice/*.mp3)")
    print(f"  • Reference audio  : {ref_wav.name if ref_wav else 'N/A'}")
    print(f"  • Model            : XTTS-v2 / {ELEVENLABS_MODEL_ID}")
    print(f"  • Speed            : {settings['speed']}")
    print(f"  • Stability        : {int(settings['stability'] * 100)}% ({settings['stability']})")
    print(f"  • Similarity boost : {int(settings['similarity_boost'] * 100)}% ({settings['similarity_boost']})")
    print(f"  • Style            : {int(settings['style'] * 100)}% ({settings['style']})")
    print(f"  • Speaker boost    : {'Enabled' if settings['use_speaker_boost'] else 'Disabled'}")
    print(f"  • Cache directory  : {TTS_CACHE_DIR}")
    print("============================================================")

    if args.sample:
        sample_out = Path(TTS_CACHE_DIR) / "example_voice_test.mp3"
        print(f"\n🎙️ Generating example audio:\n   \"{SAMPLE_TEXT}\"")
        path = get_or_create_tts_mp3(
            SAMPLE_TEXT,
            output_path=sample_out,
            force_regenerate=True,
            prefer_local_clone=args.local_clone,
        )
        if path and path.exists():
            print(f"✅ Example audio saved to: {path} ({path.stat().st_size:,} bytes)")
            if not args.no_play:
                print("🔊 Playing example audio...")
                play_clip_sync(path)
        else:
            print("❌ Failed to generate example audio.")
            return 1

    if args.text:
        print(f"\n🎙️ Generating custom phrase:\n   \"{args.text}\"")
        path = get_or_create_tts_mp3(
            args.text,
            force_regenerate=args.force,
            prefer_local_clone=args.local_clone,
        )
        if path and path.exists():
            print(f"✅ Cached at: {path} ({path.stat().st_size:,} bytes)")
            if not args.no_play:
                print("🔊 Playing audio...")
                play_clip_sync(path)
        else:
            print("❌ Failed to generate custom phrase.")
            return 1

    if args.all:
        phrases = collect_static_phrases()
        print(f"\n📦 Caching {len(phrases)} static voice replies to {TTS_CACHE_DIR} ...")
        ok = 0
        for name, phrase in phrases:
            path = get_or_create_tts_mp3(
                phrase,
                force_regenerate=args.force,
                prefer_local_clone=args.local_clone,
            )
            if path and path.exists():
                print(f"  ✅ [{name}] -> {path.name}")
                ok += 1
            else:
                print(f"  ❌ [{name}] Failed: \"{phrase}\"")
        print(f"\n🎉 Cached {ok}/{len(phrases)} voice clips permanently in {TTS_CACHE_DIR}")
        if ok < len(phrases):
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
