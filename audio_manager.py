#!/usr/bin/env python3
"""
Audio Manager for Fitness Robot.
Handles non-blocking background MP3 playback for voice guidance, alerts,
and ElevenLabs / Local XTTS-v2 Voice Cloning with permanent SD-card caching.
"""

import os
import re
import sys
import json
import time
import hashlib
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path
from config import (
    PROJECT_DIR,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_SPEED,
    ELEVENLABS_STABILITY,
    ELEVENLABS_SIMILARITY_BOOST,
    ELEVENLABS_STYLE,
    ELEVENLABS_SPEAKER_BOOST,
    TTS_CACHE_DIR,
)

VOICE_FILE_MAP = {
    "align horizontally":    "001_align_horizontally_GET_IN_POSITION.mp3",
    "too deep":              "002_don't_go_too_deep_in_squats.mp3",
    "equalize both hands":   "003_equalize_both_hands.mp3",
    "extend arms straight":  "004_extendarms_straight_and_directly_in_front_of_you.mp3",
    "straight line":         "005_form_a_straight_line_with_body!.mp3",
    "feet at shoulder":      "006_keep_feet_at_shoulder_distance.mp3",
    "look in front":         "007_look_in_front_of_you!.mp3",
    "curling too fast":      "008_slow_down!_curling_too_fast.mp3",
    "stand at shoulder width": "009_stand_at_shoulder_width_and_keep_knees_in_front_of_body.mp3",
    "tuck elbow":            "010_tuck_elbow_closer_to_your_body.mp3",
    "swing":                 "011_do_not_swing_arm,keep_hands_in_straight_position_without_swinging.mp3",
}

# Reference ElevenLabs clips in voice/ used to clone Voice ID 6psAnGNeDguzLyTxKYvI locally
REFERENCE_VOICE_FILES = [
    "004_extendarms_straight_and_directly_in_front_of_you.mp3",
    "009_stand_at_shoulder_width_and_keep_knees_in_front_of_body.mp3",
    "011_do_not_swing_arm,keep_hands_in_straight_position_without_swinging.mp3",
    "013_please_rest_for_a_while.mp3",
    "014_lets_start_the_workout.mp3",
    "015_lets_resume_the_workout.mp3",
    "016_an_obstacle_is_detected.mp3",
]

_current_audio_process = None
_process_lock = threading.Lock()
_audio_lock = threading.Semaphore(1)
_cache_lock = threading.Lock()
_tts_speaking = threading.Event()
_xtts_model = None
_xtts_lock = threading.Lock()


def is_tts_speaking():
    """Return True while FitBot is speaking a synthesized voice response."""
    return _tts_speaking.is_set()


def _resolve_elevenlabs_api_key():
    """Resolve ELEVENLABS_API_KEY from environment, config, or .env file."""
    key = os.getenv("ELEVENLABS_API_KEY", "").strip() or ELEVENLABS_API_KEY
    if key:
        return key
    env_path = PROJECT_DIR / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ELEVENLABS_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            os.environ["ELEVENLABS_API_KEY"] = val
                            return val
        except OSError:
            pass
    return ""


def _normalize_phrase_key(text):
    """Canonical lowercase key for phrase lookup."""
    return re.sub(r"\s+", " ", str(text).strip()).lower()


def _slugify_phrase(text, max_len=42):
    """Human-readable filename prefix from spoken phrase."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")
    return (slug[:max_len].rstrip("_") or "tts")


def _compute_cache_filename(text, voice_id=None, model_id=None, settings=None):
    """Compute deterministic MP3 filename for a phrase and voice configuration."""
    voice_id = voice_id or ELEVENLABS_VOICE_ID
    model_id = model_id or ELEVENLABS_MODEL_ID
    settings = settings or get_elevenlabs_voice_settings()
    norm = _normalize_phrase_key(text)
    signature = json.dumps(
        {
            "text": norm,
            "voice_id": voice_id,
            "model_id": model_id,
            "settings": settings,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10]
    slug = _slugify_phrase(norm)
    return f"{slug}_{digest}.mp3"


def get_elevenlabs_voice_settings():
    """Return the configured ElevenLabs voice_settings dictionary."""
    return {
        "stability": float(ELEVENLABS_STABILITY),
        "similarity_boost": float(ELEVENLABS_SIMILARITY_BOOST),
        "style": float(ELEVENLABS_STYLE),
        "use_speaker_boost": bool(ELEVENLABS_SPEAKER_BOOST),
        "speed": float(ELEVENLABS_SPEED),
    }


def _load_cache_manifest(cache_dir):
    manifest_path = cache_dir / "manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _save_cache_manifest(cache_dir, manifest):
    manifest_path = cache_dir / "manifest.json"
    tmp_path = cache_dir / "manifest.json.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, manifest_path)
    except Exception as exc:
        print(f"⚠️ Failed to update TTS cache manifest: {exc}")


def _get_ffmpeg_exe():
    """Resolve ffmpeg binary from system PATH or imageio_ffmpeg."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def build_reference_voice_wav(cache_dir=None):
    """
    Build a combined 22050 Hz mono reference WAV from the existing ElevenLabs MP3
    clips in `voice/` so XTTS-v2 can clone the exact voice (`6psAnGNeDguzLyTxKYvI`).
    """
    cache_dir = Path(cache_dir or TTS_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ref_wav = cache_dir / f"_reference_voice_{ELEVENLABS_VOICE_ID}.wav"
    if ref_wav.exists() and ref_wav.stat().st_size > 4096:
        return ref_wav

    voice_dir = PROJECT_DIR / "voice"
    input_mp3s = [voice_dir / name for name in REFERENCE_VOICE_FILES if (voice_dir / name).exists()]
    if not input_mp3s:
        input_mp3s = sorted(voice_dir.glob("*.mp3"))[:6]
    if not input_mp3s:
        return None

    ffmpeg_exe = _get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-y"]
    for mp3 in input_mp3s:
        cmd.extend(["-i", str(mp3)])
    n = len(input_mp3s)
    filter_inputs = "".join(f"[{i}:a]" for i in range(n))
    filter_complex = f"{filter_inputs}concat=n={n}:v=0:a=1[outa]"
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[outa]",
        "-ar", "22050",
        "-ac", "1",
        str(ref_wav),
    ])
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if res.returncode == 0 and ref_wav.exists() and ref_wav.stat().st_size > 4096:
        return ref_wav
    return None


def _generate_via_local_xtts(clean_text, target_path, voice_settings=None):
    """
    Synthesize `clean_text` using local Coqui XTTS-v2 voice cloning from
    the existing ElevenLabs MP3 clips in `voice/`, applying the configured
    speed (`0.9`) and stability/temperature derived from the ElevenLabs settings.
    """
    global _xtts_model
    voice_settings = voice_settings or get_elevenlabs_voice_settings()
    cache_dir = Path(TTS_CACHE_DIR)
    ref_wav = build_reference_voice_wav(cache_dir)
    if ref_wav is None or not ref_wav.exists():
        print("⚠️ No reference MP3 clips found in voice/ for local voice cloning.")
        return None

    # Allow automatic Coqui TOS agreement and keep model cache inside workspace .venv
    os.environ["COQUI_TOS_AGREED"] = "1"
    os.environ.setdefault("TTS_HOME", str(PROJECT_DIR / ".venv" / "tts_models_cache"))
    os.environ.setdefault("HF_HOME", str(PROJECT_DIR / ".venv" / "hf_cache"))

    try:
        import types
        import importlib.machinery
        import torch
        import torchaudio
        import soundfile as sf

        if "torchcodec" not in sys.modules:
            _tc = types.ModuleType("torchcodec")
            _tc.__spec__ = importlib.machinery.ModuleSpec("torchcodec", None)
            sys.modules["torchcodec"] = _tc

        if "six" not in sys.modules:
            _six = types.ModuleType("six")
            _six.string_types = (str,)
            _six.integer_types = (int,)
            _six.text_type = str
            _six.binary_type = bytes
            _six.PY3 = True
            _six.__spec__ = importlib.machinery.ModuleSpec("six", None)
            sys.modules["six"] = _six

        def _sf_torchaudio_load(filepath, *args, **kwargs):
            data, sr = sf.read(str(filepath), dtype="float32", always_2d=True)
            return torch.from_numpy(data.T.copy()), sr

        torchaudio.load = _sf_torchaudio_load
        from TTS.api import TTS
    except ImportError:
        # If running from global Python while coqui-tts is installed in .venv, invoke .venv Python
        venv_py = PROJECT_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if venv_py.exists() and Path(sys.executable).resolve() != venv_py.resolve():
            helper_code = (
                "import sys; from pathlib import Path; "
                "from audio_manager import _generate_via_local_xtts; "
                "res = _generate_via_local_xtts(sys.argv[1], Path(sys.argv[2])); "
                "sys.exit(0 if res else 1)"
            )
            proc = subprocess.run([str(venv_py), "-c", helper_code, clean_text, str(target_path)])
            if proc.returncode == 0 and target_path.exists() and target_path.stat().st_size > 256:
                return target_path
        return None

    with _xtts_lock:
        if _xtts_model is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            print(f"🎙️ Loading XTTS-v2 voice cloning model on {device.upper()}...")
            _xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)

        tmp_wav = target_path.with_suffix(".tmp.wav")
        speed = float(voice_settings.get("speed", 0.9))
        stability = float(voice_settings.get("stability", 0.50))
        # Map stability (0..1) -> XTTS temperature (higher stability = lower temperature)
        temperature = max(0.15, min(0.85, round(1.0 - 0.6 * stability, 2)))

        _xtts_model.tts_to_file(
            text=clean_text,
            speaker_wav=str(ref_wav),
            language="en",
            file_path=str(tmp_wav),
            speed=speed,
            temperature=temperature,
            split_sentences=True,
        )

    if not tmp_wav.exists() or tmp_wav.stat().st_size <= 256:
        return None

    # Convert WAV -> 44.1 kHz 128 kbps MP3 for compact permanent SD card storage
    ffmpeg_exe = _get_ffmpeg_exe()
    tmp_mp3 = target_path.with_suffix(".tmp.mp3")
    conv = subprocess.run(
        [
            ffmpeg_exe, "-y",
            "-i", str(tmp_wav),
            "-ar", "44100",
            "-ac", "1",
            "-b:a", "128k",
            str(tmp_mp3),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        tmp_wav.unlink(missing_ok=True)
    except Exception:
        pass

    if conv.returncode == 0 and tmp_mp3.exists() and tmp_mp3.stat().st_size > 256:
        os.replace(tmp_mp3, target_path)
        return target_path
    return None


def get_or_create_tts_mp3(text, output_path=None, force_regenerate=False, prefer_local_clone=False):
    """
    Return Path to an MP3 file for `text` using the FitBot voice (`6psAnGNeDguzLyTxKYvI`).
    1. Checks permanent SD card cache (`voice/tts_cache/`) first.
    2. If missing (or `force_regenerate=True`):
       - Tries ElevenLabs API (unless `prefer_local_clone=True` or free-tier 402 on library voice)
       - Falls back to local XTTS-v2 voice cloning trained from `voice/001`–`016.mp3`.
    3. Saves the MP3 permanently in `voice/tts_cache/` and updates `manifest.json`.
    """
    if not text or not str(text).strip():
        return None

    clean_text = re.sub(r"\s+", " ", str(text).strip())
    norm_key = _normalize_phrase_key(clean_text)
    cache_dir = Path(TTS_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)

    voice_id = ELEVENLABS_VOICE_ID
    model_id = ELEVENLABS_MODEL_ID
    voice_settings = get_elevenlabs_voice_settings()

    if output_path is not None:
        target_path = Path(output_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        filename = _compute_cache_filename(clean_text, voice_id, model_id, voice_settings)
        target_path = cache_dir / filename

    # 1. Check permanent disk cache first (unless force_regenerate is requested)
    if not force_regenerate:
        if target_path.exists() and target_path.stat().st_size > 256:
            return target_path

        with _cache_lock:
            manifest = _load_cache_manifest(cache_dir)
            entry = manifest.get(norm_key)
            if isinstance(entry, dict) and entry.get("file"):
                existing = cache_dir / entry["file"]
                if existing.exists() and existing.stat().st_size > 256:
                    return existing

    # 2. Try ElevenLabs API if configured and not preferring local clone
    api_key = _resolve_elevenlabs_api_key()
    if api_key and not prefer_local_clone:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
        payload = {
            "text": clean_text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        }
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                audio_bytes = resp.read()

            if audio_bytes and len(audio_bytes) > 256:
                with _cache_lock:
                    tmp_file = target_path.with_suffix(".mp3.tmp")
                    with open(tmp_file, "wb") as f:
                        f.write(audio_bytes)
                    os.replace(tmp_file, target_path)

                    if target_path.parent == cache_dir:
                        manifest = _load_cache_manifest(cache_dir)
                        manifest[norm_key] = {
                            "text": clean_text,
                            "file": target_path.name,
                            "voice_id": voice_id,
                            "model_id": model_id,
                            "engine": "elevenlabs_api",
                            "voice_settings": voice_settings,
                            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        }
                        _save_cache_manifest(cache_dir, manifest)
                return target_path
        except urllib.error.HTTPError as http_err:
            # HTTP 402 occurs when using a Community Library voice on a free API plan;
            # fall through to local XTTS-v2 voice cloning from voice/*.mp3
            if http_err.code != 402:
                err_body = ""
                try:
                    err_body = http_err.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                print(f"⚠️ ElevenLabs API HTTP {http_err.code}: {err_body}")
        except Exception:
            pass

    # 3. Generate via local XTTS-v2 voice cloning using `voice/001`–`016.mp3`
    cloned_path = _generate_via_local_xtts(clean_text, target_path, voice_settings)
    if cloned_path is not None and cloned_path.exists():
        with _cache_lock:
            if cloned_path.parent == cache_dir:
                manifest = _load_cache_manifest(cache_dir)
                manifest[norm_key] = {
                    "text": clean_text,
                    "file": cloned_path.name,
                    "voice_id": voice_id,
                    "model_id": "xtts_v2_clone",
                    "engine": "local_xtts_v2",
                    "voice_settings": voice_settings,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                _save_cache_manifest(cache_dir, manifest)
        return cloned_path

    return None


def _fallback_system_tts(clean_text):
    """Fallback OS synthesizer if offline and phrase is not yet cached on SD card."""
    if os.name == "nt":
        escaped = clean_text.replace("'", "''")
        ps_cmd = (
            f"try {{ (New-Object -ComObject SAPI.SpVoice).Speak('{escaped}') }} "
            f"catch {{ Add-Type -AssemblyName System.Speech; "
            f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{escaped}') }}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-c", ps_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
        )
    else:
        for cmd in (
            ["espeak-ng", "-s", "155", clean_text],
            ["espeak", "-s", "155", clean_text],
            ["spd-say", "--wait", clean_text],
            ["flite", "-t", clean_text],
        ):
            try:
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=12,
                )
                if res.returncode == 0:
                    break
            except FileNotFoundError:
                continue
            except Exception:
                break


def speak_text(text):
    """
    Speak a conversational response out loud in a background thread.
    Plays the permanently cached MP3 from `voice/tts_cache/` (or generates
    and caches it on first use). Falls back to system TTS only if uncached.
    """
    if not text or not str(text).strip():
        return
    clean_text = str(text).strip()

    def _run_tts():
        _tts_speaking.set()
        try:
            mp3_path = get_or_create_tts_mp3(clean_text)
            if mp3_path is not None and mp3_path.exists():
                _audio_lock.acquire(blocking=True)
                try:
                    if os.name == "nt":
                        played = _play_mp3_windows(str(mp3_path))
                    else:
                        played = _play_mp3_linux(str(mp3_path))
                finally:
                    _audio_lock.release()
                if played:
                    return

            _fallback_system_tts(clean_text)
        except Exception:
            pass
        finally:
            time.sleep(0.15)
            _tts_speaking.clear()

    threading.Thread(target=_run_tts, daemon=True).start()


def terminate_current_audio():
    """Terminate the currently active audio playback process immediately."""
    global _current_audio_process
    with _process_lock:
        if _current_audio_process is not None:
            try:
                _current_audio_process.terminate()
                _current_audio_process.wait(timeout=1)
            except Exception:
                pass
            _current_audio_process = None


def _play_mp3_windows(file_path_str):
    """Play MP3 on Windows using PowerShell Media.MediaPlayer."""
    global _current_audio_process
    uri = "file:///" + file_path_str.replace("\\", "/").replace(" ", "%20").replace("'", "''")
    ps_cmd = (
        "Add-Type -AssemblyName PresentationCore;"
        "$m=New-Object System.Windows.Media.MediaPlayer;"
        f"$m.Open([System.Uri]'{uri}');"
        "$sw=[Diagnostics.Stopwatch]::StartNew();"
        "while(-not $m.NaturalDuration.HasTimeSpan -and $sw.ElapsedMilliseconds -lt 2000){Start-Sleep -Milliseconds 50};"
        "$m.Play();"
        "$dur=if($m.NaturalDuration.HasTimeSpan){[int][Math]::Ceiling($m.NaturalDuration.TimeSpan.TotalMilliseconds)+250}else{5000};"
        "Start-Sleep -Milliseconds $dur;"
        "$m.Close()"
    )
    try:
        terminate_current_audio()
        with _process_lock:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-c", ps_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )
            _current_audio_process = proc

        try:
            stdout, stderr = proc.communicate(timeout=25)
            if proc.returncode != 0 and proc.returncode is not None:
                err = stderr.decode(errors="replace").strip()
                print(f"⚠️ PowerShell audio failed (rc={proc.returncode}): {err[:200]}")
                return False
        except subprocess.TimeoutExpired:
            terminate_current_audio()
            return False

        with _process_lock:
            if _current_audio_process == proc:
                _current_audio_process = None
        return True
    except Exception as exc:
        print(f"⚠️ _play_mp3_windows exception: {exc}")
        return False


def _play_mp3_linux(file_path_str):
    """Play MP3 on Linux / Raspberry Pi using mpg123, mpg321, or play."""
    global _current_audio_process
    for cmd in [
        ["mpg123", "-q", file_path_str],
        ["mpg321", "-q", file_path_str],
        ["play",   "-q", file_path_str],
    ]:
        try:
            terminate_current_audio()
            with _process_lock:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE
                )
                _current_audio_process = proc

            try:
                stdout, stderr = proc.communicate(timeout=25)
                if proc.returncode == 0:
                    with _process_lock:
                        if _current_audio_process == proc:
                            _current_audio_process = None
                    return True
                err_text = stderr.decode(errors="replace").strip()
                if err_text:
                    print(f"⚠️ {cmd[0]} returned {proc.returncode}: {err_text[:120]}")
            except subprocess.TimeoutExpired:
                terminate_current_audio()
            except FileNotFoundError:
                continue
        except Exception as exc:
            print(f"⚠️ {cmd[0]} error: {exc}")
            continue
    print("⚠️ No working MP3 player found. On RPi run: sudo apt-get install -y mpg123")
    return False


def _play_mp3_background(file_path_str, force=False):
    """Play audio file in non-blocking background thread."""
    def run():
        if force:
            terminate_current_audio()
            _audio_lock.acquire(blocking=True)
        else:
            if not _audio_lock.acquire(blocking=False):
                return
        try:
            if os.name == 'nt':
                _play_mp3_windows(file_path_str)
            else:
                _play_mp3_linux(file_path_str)
        except Exception as exc:
            print(f"⚠️ Audio playback error: {exc}")
        finally:
            _audio_lock.release()

    t = threading.Thread(target=run, daemon=False)
    t.start()


def play_audio_track(track_number):
    """Play state transition audio tracks (13=rest, 14=start, 15=resume, 16=obstacle)."""
    track_files = {
        13: "013_please_rest_for_a_while.mp3",
        14: "014_lets_start_the_workout.mp3",
        15: "015_lets_resume_the_workout.mp3",
        16: "016_an_obstacle_is_detected.mp3"
    }
    filename = track_files.get(track_number)
    if filename:
        file_path = PROJECT_DIR / "voice" / filename
        if not file_path.exists():
            # Try alternate naming
            if track_number == 14:
                alt = PROJECT_DIR / "voice" / "014_let's_start_the_workout.mp3"
                if alt.exists(): file_path = alt
            elif track_number == 15:
                alt = PROJECT_DIR / "voice" / "015_lets_resume_the_workout.mp3"
                if alt.exists(): file_path = alt

        if file_path.exists():
            _play_mp3_background(str(file_path), force=True)
        else:
            print(f"⚠️ Voice file not found: {file_path}")


def play_voice_command(err_msg, is_resting=False, person_missing=False, lost_person_since=None):
    """Play matching MP3 feedback based on error message."""
    if is_resting or person_missing:
        return
    if lost_person_since is not None and (time.time() - lost_person_since) > 2.0:
        return

    msg = err_msg.lower()
    matched_file = None
    for key, filename in VOICE_FILE_MAP.items():
        if key in msg:
            matched_file = filename
            break

    if not matched_file:
        return

    file_path = PROJECT_DIR / "voice" / matched_file
    if file_path.exists():
        _play_mp3_background(str(file_path))
    else:
        print(f"⚠️ Voice file not found: {file_path}")
