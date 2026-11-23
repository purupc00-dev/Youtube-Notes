#!/usr/bin/env python3
"""Generate story-matched music and SFX with AI, falling back to distinct local scores.

v10 Upgrades:
- Three genuinely distinct musical identities (different keys, tempos, chord progressions,
  opening hooks, instrumentation, and rhythmic pulses — NEVER renamed/duplicated files):
  1. `light_cinematic_motivation` (alias `hopeful`): bright D-major piano/plucked hook,
     gentle brushed percussion, uplifting strings (108 BPM).
  2. `warm_acoustic_storytelling` (alias `warm_emotional`): G-major fingerpicked acoustic
     guitar/mandolin + felt piano, soft organic pulse, warm strings (96 BPM).
  3. `emotional_uplifting_score` (alias `moody`): A-minor to C-major lifting melodic piano,
     rich restrained strings, soft bass, warm emotional cadence (88 BPM).
- Music audition mode (`--auditions`) generates 3 distinct audition tracks and validates
  distinctness using SHA-256 hashes + spectral/RMS audio fingerprints.
- Logs prompt, provider, model, source (`ai` vs `library_fallback`), duration, and SHA-256
  into `music_manifest.json` and `audio_manifest.json`. Never claims AI music when fallback
  library audio was used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

SR = 22050
MUSIC_SR = 44100

STYLE_ALIASES = {
    "hopeful": "light_cinematic_motivation",
    "warm_emotional": "warm_acoustic_storytelling",
    "moody": "emotional_uplifting_score",
    "light_cinematic_motivation": "light_cinematic_motivation",
    "warm_acoustic_storytelling": "warm_acoustic_storytelling",
    "emotional_uplifting_score": "emotional_uplifting_score",
}

CANONICAL_TO_FILENAME = {
    "light_cinematic_motivation": "hopeful.mp3",
    "warm_acoustic_storytelling": "warm_emotional.mp3",
    "emotional_uplifting_score": "moody.mp3",
}

MUSIC_IDENTITIES = {
    "light_cinematic_motivation": {
        "label": "Light Cinematic Motivation",
        "alias": "hopeful",
        "bpm": 108,
        "key": "D major",
        "seed": 1081,
        "prompt_style": (
            "bright uplifting modern Hindi storytelling instrumental with a catchy opening plucked acoustic "
            "and bright piano motif, gentle brushed shaker and soft rhythmic pulse, warm rising strings "
            "entering in the second half, gradual emotional build, and a warm satisfying major resolution"
        ),
    },
    "warm_acoustic_storytelling": {
        "label": "Warm Acoustic Storytelling",
        "alias": "warm_emotional",
        "bpm": 96,
        "key": "G major",
        "seed": 2096,
        "prompt_style": (
            "warm acoustic Hindi short-film underscore led by fingerpicked acoustic guitar and mandolin-like "
            "plucked hook, gentle felt piano chords, soft organic hand-percussion pulse, tender cello and "
            "warm strings lifting at the turning point, intimate and heartfelt with a warm ending"
        ),
    },
    "emotional_uplifting_score": {
        "label": "Emotional Uplifting Score",
        "alias": "moody",
        "bpm": 88,
        "key": "A minor to C major",
        "seed": 3088,
        "prompt_style": (
            "delicate melodic piano opening hook with rich restrained string ensemble harmonies, soft warm "
            "upright bass, gentle acoustic texture, and a gradual cinematic emotional lift from tender "
            "reflection into a bright hopeful major resolution"
        ),
    },
}

SFX_LIBRARY = {
    "room_tone": "quiet_apartment_room_tone.mp3",
    "wind": "soft_hallway_wind.mp3",
    "door_creak": "wooden_door_creak.mp3",
    "footsteps": "soft_indoor_footsteps.mp3",
    "candle_flame": "candle_flame_flicker.mp3",
    "match_strike": "match_strike.mp3",
    "warm_chime": "warm_glass_chime.mp3",
    "soft_whoosh": "gentle_transition_whoosh.mp3",
    "cloth": "soft_cloth_rustle.mp3",
    "morning_birds": "morning_birds_ambience.mp3",
    "kitchen_ambience": "warm_kitchen_tiffin_ambience.mp3",
    "paper_note": "paper_note_rustle.mp3",
    "train_station": "distant_train_station_ambience.mp3",
    "clock_tick": "gentle_clock_ticking.mp3",
    "night_crickets": "soft_night_crickets_breeze.mp3",
    "cut_whoosh": "cinema_cut_whoosh.mp3",
    "sparkle": "warm_reveal_sparkle.mp3",
    "heartbeat": "emotional_soft_heartbeat.mp3",
    "thud": "cinematic_soft_thud.mp3",
    "pop": "soft_reveal_pop.mp3",
}

SFX_PROMPTS = {
    "room_tone": "Very subtle natural room tone inside a modest Indian street or home at night, quiet air, distant gentle ambience, no voices, no music.",
    "wind": "A soft cool night breeze moving gently through a quiet street or corridor, intimate cinematic ambience, no music.",
    "door_creak": "A wooden shop shutter or door slowly closing with a quiet realistic creak at night, close perspective, subtle and natural.",
    "footsteps": "Two or three gentle footsteps on a pavement or tiled floor, slow careful walking, intimate perspective, no voices.",
    "candle_flame": "A very subtle close-up lantern or candle flame flutter with faint warm crackle, quiet night ambience, delicate and realistic.",
    "match_strike": "A single match striking and catching with a crisp scratch followed by a tiny flame ignition, close and intimate, no speech.",
    "warm_chime": "A soft delicate glass chime with a warm resonant tail, tender emotional accent, not a musical melody.",
    "soft_whoosh": "A very gentle cinematic air whoosh for a quiet scene transition, warm and restrained, no impact boom.",
    "cloth": "Soft cotton clothing fabric rustling or book pages turning gently, close natural foley.",
    "morning_birds": "Gentle early morning Indian neighborhood birds chirping softly outside a sunlit window with light warm breeze, peaceful cinematic ambience, no voices, no music.",
    "kitchen_ambience": "Warm home kitchen morning ambience with a subtle metallic tiffin box lid closing gently and soft distant domestic air, intimate realistic foley, no voices.",
    "paper_note": "Crisp delicate sound of a small handwritten paper note being folded or gently unfolded in quiet hands, close intimate foley.",
    "train_station": "Soft distant Indian railway platform ambience with gentle air movement and far-off muffled station hum, restrained cinematic background.",
    "clock_tick": "Warm intimate mechanical watch and wall clock ticking softly in a quiet wooden workshop, steady comforting rhythm.",
    "night_crickets": "Gentle warm Indian night street ambience with distant soft crickets and light evening breeze, peaceful cinematic background.",
    "cut_whoosh": "Soft warm cinematic air whoosh for a smooth scene cut, low-passed below 2.5 kHz, gentle and non-harsh.",
    "sparkle": "Delicate warm golden bell shimmer accent for an uplifting emotional reveal, rounded and restrained.",
    "heartbeat": "Deep soft cinematic double heartbeat pulse for a quiet emotional turning point, low-passed below 300 Hz.",
    "thud": "Restrained low warm cinematic impact thud, rounded and soft without trailer boom.",
    "pop": "Gentle soft rounded acoustic pop accent for a subtle visual cue.",
}


def load_env(project_root: Path) -> None:
    for candidate in (project_root / ".env", Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def canonical_style(style: str) -> str:
    return STYLE_ALIASES.get(style, "warm_acoustic_storytelling")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_audio_fingerprint(path: Path) -> list[float]:
    """Compute a 16-band normalized spectral + temporal energy fingerprint via FFmpeg + NumPy."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path.is_file():
        return []
    try:
        import numpy as np
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-t", "20", "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0 or len(proc.stdout) < 3200:
            return []
        sig = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        # 12 log-spaced spectral bands + 4 temporal envelope quartiles
        spec = np.abs(np.fft.rfft(sig[:min(len(sig), 16000 * 12)]))
        edges = np.geomspace(10, len(spec), 13).astype(int)
        bands = [float(np.mean(spec[edges[i]: max(edges[i] + 1, edges[i + 1])])) for i in range(12)]
        b_norm = math.sqrt(sum(v * v for v in bands)) or 1.0
        bands = [round(v / b_norm, 5) for v in bands]
        chunks = np.array_split(np.abs(sig), 4)
        env = [float(np.mean(c)) for c in chunks]
        e_norm = math.sqrt(sum(v * v for v in env)) or 1.0
        env = [round(v / e_norm, 5) for v in env]
        return bands + env
    except Exception:
        return []


def fingerprint_similarity(fp_a: list[float], fp_b: list[float]) -> float:
    if not fp_a or not fp_b or len(fp_a) != len(fp_b):
        return 0.0
    dot = sum(a * b for a, b in zip(fp_a, fp_b))
    na = math.sqrt(sum(a * a for a in fp_a)) or 1.0
    nb = math.sqrt(sum(b * b for b in fp_b)) or 1.0
    return round(dot / (na * nb), 5)


def verify_distinct_tracks(track_records: list[dict], max_similarity: float = 0.985) -> dict:
    """Verify that generated/fallback music options are genuinely distinct (hash + fingerprint)."""
    hashes: dict[str, str] = {}
    duplicate_hashes: list[tuple[str, str]] = []
    similar_pairs: list[dict] = []
    for rec in track_records:
        h = rec.get("sha256", "")
        name = rec.get("style", rec.get("file", ""))
        if h and h in hashes:
            duplicate_hashes.append((hashes[h], name))
        elif h:
            hashes[h] = name

    for i in range(len(track_records)):
        for j in range(i + 1, len(track_records)):
            sim = fingerprint_similarity(
                track_records[i].get("spectral_fingerprint", []),
                track_records[j].get("spectral_fingerprint", []),
            )
            if sim >= max_similarity:
                similar_pairs.append({
                    "a": track_records[i].get("style"),
                    "b": track_records[j].get("style"),
                    "similarity": sim,
                })
    ok = (not duplicate_hashes) and (not similar_pairs)
    return {
        "distinct": ok,
        "duplicate_hashes": duplicate_hashes,
        "high_similarity_pairs": similar_pairs,
        "threshold": max_similarity,
    }


def write_stereo_wav(path: Path, left, right, sample_rate: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import numpy as np
        l_arr = np.clip(np.asarray(left, dtype=np.float32), -1.0, 1.0)
        r_arr = np.clip(np.asarray(right, dtype=np.float32), -1.0, 1.0)
        interleaved = np.empty(l_arr.size * 2, dtype=np.int16)
        interleaved[0::2] = (l_arr * 32767.0).astype(np.int16)
        interleaved[1::2] = (r_arr * 32767.0).astype(np.int16)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(interleaved.tobytes())
    except ImportError:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            frames = bytearray()
            for l, r in zip(left, right):
                frames += struct.pack("<hh", int(max(-1, min(1, l)) * 32767), int(max(-1, min(1, r)) * 32767))
            wf.writeframes(frames)


def write_mono_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32767)) for x in samples))


def _midi_to_hz(midi: float) -> float:
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


def _add_note(buf_l, buf_r, start_idx: int, dur_s: float, freq: float, amp: float, pan: float, timbre: str, sr: int) -> None:
    import numpy as np
    n = int(dur_s * sr)
    if n <= 8 or start_idx >= len(buf_l):
        return
    end_idx = min(len(buf_l), start_idx + n)
    length = end_idx - start_idx
    t = np.arange(length, dtype=np.float32) / sr

    if timbre == "bright_piano":
        env = np.minimum(1.0, t / 0.008) * np.exp(-t * 3.1)
        sig = (
            0.62 * np.sin(2 * np.pi * freq * t)
            + 0.24 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t * 2.0)
            + 0.10 * np.sin(2 * np.pi * freq * 3.0 * t) * np.exp(-t * 3.8)
            + 0.04 * np.sin(2 * np.pi * freq * 4.01 * t) * np.exp(-t * 5.5)
        ) * env
    elif timbre == "acoustic_pluck":
        env = np.minimum(1.0, t / 0.004) * np.exp(-t * 4.8)
        sig = (
            0.55 * np.sin(2 * np.pi * freq * t)
            + 0.28 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t * 3.2)
            + 0.14 * np.sin(2 * np.pi * freq * 3.0 * t) * np.exp(-t * 5.5)
            + 0.06 * np.sin(2 * np.pi * freq * 5.0 * t) * np.exp(-t * 9.0)
        ) * env
    elif timbre == "felt_piano":
        env = np.minimum(1.0, t / 0.016) * np.exp(-t * 2.1)
        sig = (
            0.78 * np.sin(2 * np.pi * freq * t)
            + 0.18 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t * 2.8)
            + 0.04 * np.sin(2 * np.pi * freq * 3.0 * t) * np.exp(-t * 5.0)
        ) * env
    elif timbre == "warm_strings":
        attack = min(0.45, dur_s * 0.35)
        release = min(0.55, dur_s * 0.35)
        env = np.minimum(1.0, t / max(0.01, attack)) * np.minimum(1.0, np.maximum(0.0, (dur_s - t) / max(0.01, release)))
        vibrato = 0.003 * np.sin(2 * np.pi * 5.1 * t)
        sig = (
            0.50 * np.sin(2 * np.pi * freq * (1.0 + vibrato) * t)
            + 0.25 * np.sin(2 * np.pi * freq * 1.003 * t)
            + 0.15 * np.sin(2 * np.pi * freq * 2.0 * t)
            + 0.06 * np.sin(2 * np.pi * freq * 3.0 * t)
        ) * env
    elif timbre == "soft_bass":
        env = np.minimum(1.0, t / 0.03) * np.exp(-t * 1.4)
        sig = (0.85 * np.sin(2 * np.pi * freq * t) + 0.15 * np.sin(2 * np.pi * freq * 2.0 * t)) * env
    else:
        env = np.minimum(1.0, t / 0.01) * np.exp(-t * 3.0)
        sig = np.sin(2 * np.pi * freq * t) * env

    g_l = amp * math.cos((pan + 1.0) * math.pi / 4.0)
    g_r = amp * math.sin((pan + 1.0) * math.pi / 4.0)
    buf_l[start_idx:end_idx] += sig * g_l
    buf_r[start_idx:end_idx] += sig * g_r


def _add_percussion(buf_l, buf_r, start_idx: int, kind: str, amp: float, pan: float, sr: int, rng) -> None:
    import numpy as np
    if kind == "shaker":
        n = int(0.055 * sr)
        end_idx = min(len(buf_l), start_idx + n)
        length = end_idx - start_idx
        if length <= 4:
            return
        t = np.arange(length, dtype=np.float32) / sr
        noise = rng.uniform(-1.0, 1.0, length).astype(np.float32)
        # Simple high-pass difference for airy brushed shaker
        hp = np.diff(noise, prepend=0.0)
        sig = hp * np.minimum(1.0, t / 0.005) * np.exp(-t * 52.0)
    elif kind == "soft_kick":
        n = int(0.16 * sr)
        end_idx = min(len(buf_l), start_idx + n)
        length = end_idx - start_idx
        if length <= 4:
            return
        t = np.arange(length, dtype=np.float32) / sr
        f = 95.0 * np.exp(-t * 22.0) + 46.0
        phase = np.cumsum(2 * np.pi * f / sr)
        sig = np.sin(phase) * np.exp(-t * 18.0)
    else:  # woodblock / rim
        n = int(0.06 * sr)
        end_idx = min(len(buf_l), start_idx + n)
        length = end_idx - start_idx
        if length <= 4:
            return
        t = np.arange(length, dtype=np.float32) / sr
        sig = (np.sin(2 * np.pi * 820.0 * t) + 0.4 * np.sin(2 * np.pi * 1240.0 * t)) * np.exp(-t * 65.0)

    g_l = amp * math.cos((pan + 1.0) * math.pi / 4.0)
    g_r = amp * math.sin((pan + 1.0) * math.pi / 4.0)
    buf_l[start_idx:end_idx] += sig * g_l
    buf_r[start_idx:end_idx] += sig * g_r


def make_music(duration: float, out: Path, mood: str = "warm_emotional") -> None:
    """Synthesize one of three genuinely distinct, catchy, modern uplifting instrumental scores."""
    import numpy as np

    style = canonical_style(mood)
    sr = MUSIC_SR
    n = max(1, int(duration * sr))
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)

    if style == "light_cinematic_motivation":
        # D major | 108 BPM | Bright catchy piano/plucked hook + brushed pulse + rising strings
        rng = np.random.default_rng(1081)
        bpm = 108.0
        beat = 60.0 / bpm
        bar = beat * 4.0
        # I - V - vi - IV in D major (D, A, Bm, G)
        chords = [
            [50, 57, 62, 66],  # D
            [45, 52, 57, 61],  # A
            [47, 54, 59, 62],  # Bm
            [43, 50, 55, 59],  # G
        ]
        # Catchy bright opening hook (eighth-note syncopated motif)
        hook_a = [(0.0, 74), (0.5, 78), (1.0, 81), (1.5, 78), (2.0, 76), (2.5, 74), (3.0, 73), (3.5, 74)]
        hook_b = [(0.0, 78), (0.5, 81), (1.0, 86), (1.5, 81), (2.0, 78), (2.5, 76), (3.0, 74), (3.5, 76)]
        num_bars = int(math.ceil(duration / bar))
        for b in range(num_bars):
            t_bar = b * bar
            prog = min(1.0, t_bar / max(1.0, duration * 0.72))
            ch = chords[b % len(chords)]
            # Warm bass root on beat 1 & 3
            _add_note(left, right, int(t_bar * sr), beat * 1.4, _midi_to_hz(ch[0]), 0.14, -0.05, "acoustic_pluck", sr)
            _add_note(left, right, int((t_bar + 2 * beat) * sr), beat * 1.2, _midi_to_hz(ch[1]), 0.12, 0.05, "acoustic_pluck", sr)
            # Bright piano chord arpeggio
            for step in range(8):
                t_n = t_bar + step * (beat * 0.5)
                note_midi = ch[step % len(ch)] + (12 if step >= 4 else 0)
                _add_note(left, right, int(t_n * sr), beat * 0.9, _midi_to_hz(note_midi), 0.14 + 0.04 * prog, -0.28 + 0.08 * (step % 4), "bright_piano", sr)
            # Memorable opening hook (develops in section B)
            motif = hook_a if (b % 4) < 2 else hook_b
            for off, midi in motif:
                _add_note(left, right, int((t_bar + off * beat) * sr), beat * 0.65, _midi_to_hz(midi + 12), 0.19 + 0.05 * prog, 0.24, "acoustic_pluck", sr)
            # Gentle uplifting rhythmic pulse (enters from bar 1)
            for step in range(8):
                t_p = t_bar + step * (beat * 0.5)
                _add_percussion(left, right, int(t_p * sr), "shaker", (0.045 if step % 2 == 0 else 0.025) * (0.6 + 0.5 * prog), 0.35 if step % 2 == 0 else -0.35, sr, rng)
                if step in (0, 5) and b >= 1:
                    _add_percussion(left, right, int(t_p * sr), "soft_kick", 0.14 * (0.65 + 0.4 * prog), 0.0, sr, rng)
                if step == 4 and b >= 2:
                    _add_percussion(left, right, int(t_p * sr), "woodblock", 0.04 * (0.6 + 0.4 * prog), -0.2, sr, rng)
            # Rising warm strings enter from bar 2 onward
            if b >= 2:
                str_amp = 0.09 + 0.09 * prog
                for k, m in enumerate(ch[1:]):
                    _add_note(left, right, int(t_bar * sr), bar * 1.05, _midi_to_hz(m + 12), str_amp, -0.45 + 0.45 * k, "warm_strings", sr)

    elif style == "warm_acoustic_storytelling":
        # G major | 96 BPM | Fingerpicked acoustic guitar/mandolin + felt piano + soft organic pulse + warm strings
        rng = np.random.default_rng(2096)
        bpm = 96.0
        beat = 60.0 / bpm
        bar = beat * 4.0
        # G - Em7 - Cadd9 - Dsus4/D
        chords = [
            [43, 50, 55, 59, 62],  # G
            [40, 47, 52, 55, 59],  # Em7
            [48, 52, 55, 60, 62],  # Cadd9
            [50, 54, 57, 62, 66],  # D
        ]
        guitar_pattern = [0, 2, 3, 4, 2, 3, 1, 3]
        melody_bars = [
            [(0.0, 71), (1.0, 74), (1.5, 76), (2.5, 74), (3.0, 71)],
            [(0.0, 71), (1.0, 69), (2.0, 67), (3.0, 71)],
            [(0.0, 72), (1.0, 74), (2.0, 76), (3.0, 79)],
            [(0.0, 78), (1.5, 76), (2.5, 74), (3.5, 71)],
        ]
        num_bars = int(math.ceil(duration / bar))
        for b in range(num_bars):
            t_bar = b * bar
            prog = min(1.0, t_bar / max(1.0, duration * 0.70))
            ch = chords[b % len(chords)]
            # Fingerpicked acoustic guitar
            for step, ci in enumerate(guitar_pattern):
                t_n = t_bar + step * (beat * 0.5)
                _add_note(left, right, int(t_n * sr), beat * 1.1, _midi_to_hz(ch[ci]), 0.16, -0.35 + 0.1 * (ci % 3), "acoustic_pluck", sr)
            # Felt piano warm chords on beat 1 and 3
            for m in ch[1:4]:
                _add_note(left, right, int(t_bar * sr), beat * 2.4, _midi_to_hz(m), 0.10 + 0.04 * prog, 0.25, "felt_piano", sr)
            # Gentle melodic mandolin/piano storytelling hook
            for off, midi in melody_bars[b % len(melody_bars)]:
                _add_note(left, right, int((t_bar + off * beat) * sr), beat * 1.2, _midi_to_hz(midi), 0.15 + 0.04 * prog, 0.15, "felt_piano" if b % 2 else "acoustic_pluck", sr)
            # Organic soft pulse
            if b >= 1:
                _add_percussion(left, right, int(t_bar * sr), "soft_kick", 0.12 * (0.6 + 0.4 * prog), 0.0, sr, rng)
                _add_percussion(left, right, int((t_bar + 2.5 * beat) * sr), "soft_kick", 0.09 * (0.6 + 0.4 * prog), 0.0, sr, rng)
                for off in (1.0, 3.0):
                    _add_percussion(left, right, int((t_bar + off * beat) * sr), "shaker", 0.035 * (0.5 + 0.5 * prog), 0.3, sr, rng)
            # Warm cello & strings swell at emotional turning point
            if b >= 2:
                _add_note(left, right, int(t_bar * sr), bar * 1.05, _midi_to_hz(ch[0]), 0.13 * (0.6 + 0.5 * prog), -0.2, "warm_strings", sr)
                _add_note(left, right, int(t_bar * sr), bar * 1.05, _midi_to_hz(ch[3] + 12), 0.09 * (0.5 + 0.6 * prog), 0.35, "warm_strings", sr)

    else:
        # emotional_uplifting_score | A minor -> C major -> F -> G | 88 BPM | Delicate melodic piano + rich restrained strings
        rng = np.random.default_rng(3088)
        bpm = 88.0
        beat = 60.0 / bpm
        bar = beat * 4.0
        chords = [
            [45, 52, 57, 60],  # Am
            [48, 55, 60, 64],  # C
            [41, 48, 53, 57],  # F
            [43, 50, 55, 59],  # G
        ]
        piano_melody = [
            [(0.0, 72), (1.0, 71), (1.5, 69), (2.5, 72), (3.5, 76)],
            [(0.0, 76), (1.5, 74), (2.5, 72), (3.5, 74)],
            [(0.0, 77), (1.0, 76), (2.0, 72), (3.0, 69)],
            [(0.0, 74), (1.0, 76), (2.0, 79), (3.0, 76)],
        ]
        num_bars = int(math.ceil(duration / bar))
        for b in range(num_bars):
            t_bar = b * bar
            prog = min(1.0, t_bar / max(1.0, duration * 0.68))
            ch = chords[b % len(chords)]
            # Deep warm bass foundation
            _add_note(left, right, int(t_bar * sr), bar * 1.05, _midi_to_hz(ch[0] - 12), 0.34 + 0.08 * prog, 0.0, "soft_bass", sr)
            _add_note(left, right, int(t_bar * sr), bar * 1.05, _midi_to_hz(ch[0]), 0.18 + 0.06 * prog, -0.25, "warm_strings", sr)
            # Delicate rolling felt piano accompaniment
            for step in range(4):
                _add_note(left, right, int((t_bar + step * beat) * sr), beat * 1.6, _midi_to_hz(ch[step]), 0.13, -0.25 + 0.15 * step, "felt_piano", sr)
            # Expressive upper piano melody
            for off, midi in piano_melody[b % len(piano_melody)]:
                _add_note(left, right, int((t_bar + off * beat) * sr), beat * 1.5, _midi_to_hz(midi), 0.17 + 0.05 * prog, 0.18, "bright_piano", sr)
            # Rich restrained string ensemble growing smoothly into climax
            str_gain = 0.07 + 0.13 * prog
            for idx_m, m in enumerate(ch[1:]):
                _add_note(left, right, int(t_bar * sr), bar * 1.08, _midi_to_hz(m + 12), str_gain, -0.5 + 0.5 * idx_m, "warm_strings", sr)
            # Subtle cinematic heartbeat pulse in second half
            if prog > 0.35:
                _add_percussion(left, right, int(t_bar * sr), "soft_kick", 0.11 * prog, 0.0, sr, rng)
                _add_percussion(left, right, int((t_bar + 2.0 * beat) * sr), "shaker", 0.028 * prog, 0.25, sr, rng)

    # Warm stereo micro-reverb (two short delay taps for cinematic room air)
    d1, d2 = int(0.037 * sr), int(0.071 * sr)
    if n > d2 + 10:
        wet_l = np.zeros_like(left)
        wet_r = np.zeros_like(right)
        wet_l[d1:] += right[:-d1] * 0.18
        wet_r[d1:] += left[:-d1] * 0.18
        wet_l[d2:] += left[:-d2] * 0.11
        wet_r[d2:] += right[:-d2] * 0.11
        left = left + wet_l
        right = right + wet_r

    # Smooth fade-in (0.8s) and warm fade-out (2.4s)
    t_arr = np.arange(n, dtype=np.float32) / sr
    fade_in = np.minimum(1.0, t_arr / 0.8)
    fade_out = np.minimum(1.0, np.maximum(0.0, (duration - t_arr) / 2.4))
    env = fade_in * fade_out
    left *= env
    right *= env

    peak = max(float(np.max(np.abs(left))), float(np.max(np.abs(right))), 1e-6)
    gain = min(1.0, 0.72 / peak)
    write_stereo_wav(out, left * gain, right * gain, sample_rate=sr)


def sfx_samples(kind: str, seconds: float, seed: int = 11) -> list[float]:
    rng = random.Random(seed)
    n = int(seconds * SR)
    out = [0.0] * n
    lp = 0.0
    phase = 0.0
    for i in range(n):
        t = i / SR
        p = t / max(seconds, 0.001)
        noise = rng.uniform(-1, 1)
        if kind == "room_tone":
            lp += 0.018 * (noise - lp)
            v = lp * 0.12 + 0.008 * math.sin(2 * math.pi * 50 * t)
        elif kind == "wind":
            lp += 0.006 * (noise - lp)
            v = lp * 1.7 * (0.6 + 0.4 * math.sin(2 * math.pi * t * 0.23)) * math.sin(math.pi * min(1, p))
        elif kind == "door_creak":
            f = 250 - 170 * p + 12 * math.sin(2 * math.pi * 3.2 * t)
            phase += 2 * math.pi * f / SR
            lp += 0.04 * (noise - lp)
            v = (math.sin(phase) * 0.22 + lp * 0.22) * math.sin(math.pi * min(1, p)) ** 1.5
        elif kind == "footsteps":
            v = 0.0
            for center in (0.18, 0.62, 1.05, 1.48):
                dt = t - center
                if 0 <= dt < 0.22:
                    lp += 0.12 * (noise - lp)
                    v += (math.sin(2 * math.pi * 62 * dt) * 0.30 + lp * 0.18) * math.exp(-dt * 18)
        elif kind == "candle_flame":
            lp += 0.08 * (noise - lp)
            v = (lp * 0.08 + math.sin(2 * math.pi * (190 + 18 * math.sin(t * 5.3)) * t) * 0.018) * (0.5 + 0.5 * math.sin(t * 2.1))
        elif kind == "match_strike":
            v = noise * math.exp(-t * 35) * 0.45 + math.sin(2 * math.pi * (850 - 700 * p) * t) * math.exp(-t * 16) * 0.10
        elif kind == "warm_chime":
            v = sum(math.sin(2 * math.pi * f * t) * math.exp(-t * (2 + j * 0.4)) * (0.13 / (j + 1)) for j, f in enumerate((659.25, 880.0, 1318.51, 1760.0)))
        elif kind == "soft_whoosh":
            lp += 0.035 * (noise - lp)
            v = lp * math.sin(math.pi * p) * 0.65
        elif kind == "cloth":
            lp += 0.09 * (noise - lp)
            v = lp * math.exp(-t * 2.4) * 0.30
        elif kind in ("morning_birds", "kitchen_ambience", "paper_note", "train_station", "clock_tick", "night_crickets"):
            # Silky, warm, low-passed acoustic ambience (zero harsh static, zero synthetic beeps!)
            import numpy as np
            from scipy import signal
            nrng = np.random.default_rng(seed)
            nn = max(64, int(seconds * SR))
            t_arr = np.arange(nn, dtype=np.float64) / SR
            def _lp(x, fc):
                b, a = signal.butter(2, min(fc, SR / 2 - 100) / (SR / 2), "low")
                return signal.lfilter(b, a, x)
            fade = np.minimum(1.0, t_arr / 0.15) * np.minimum(1.0, np.maximum(0.0, (seconds - t_arr) / 0.20))
            if kind == "morning_birds":
                # Warm soft morning breeze air (strictly gentle low-pass < 750 Hz, no harsh beeps)
                breeze = _lp(nrng.standard_normal(nn), 520) * (0.65 + 0.35 * np.sin(2 * np.pi * t_arr * 0.35))
                sig = breeze * 0.28 * fade
            elif kind == "kitchen_ambience":
                # Gentle warm indoor room air (< 450 Hz)
                air = _lp(nrng.standard_normal(nn), 380) * (0.75 + 0.25 * np.sin(2 * np.pi * t_arr * 0.25))
                sig = air * 0.22 * fade
            elif kind == "paper_note":
                # Ultra-soft warm cloth/paper brush (band-limited 250-1100 Hz, zero white-noise static)
                b_bp, a_bp = signal.butter(2, [220 / (SR / 2), 1100 / (SR / 2)], "band")
                soft = signal.lfilter(b_bp, a_bp, nrng.standard_normal(nn))
                sig = soft * np.sin(np.pi * np.linspace(0, 1, nn)) ** 2 * 0.25
            elif kind == "train_station":
                # Soft warm distant low hum (< 320 Hz)
                air = _lp(nrng.standard_normal(nn), 280) * (0.8 + 0.2 * np.sin(2 * np.pi * t_arr * 0.3))
                sig = air * 0.28 * fade
            elif kind == "clock_tick":
                # Soft wooden muffled clock tick (< 950 Hz)
                sig = np.zeros(nn)
                for sec_i in range(int(seconds) + 1):
                    st = int(sec_i * SR)
                    seg_n = min(nn - st, int(0.035 * SR))
                    if seg_n > 8:
                        ts = np.arange(seg_n) / SR
                        sig[st:st + seg_n] += np.sin(2 * np.pi * 620.0 * ts) * np.exp(-ts * 110.0) * 0.25
                sig = _lp(sig, 950)
            else:  # night_crickets -> soft warm night breeze (< 480 Hz, zero harsh cricket hiss)
                air = _lp(nrng.standard_normal(nn), 420) * (0.7 + 0.3 * np.sin(2 * np.pi * t_arr * 0.28))
                sig = air * 0.24 * fade
            peak_s = float(np.max(np.abs(sig))) or 1.0
            return (sig * min(1.0, 0.38 / peak_s)).tolist()
        elif kind in ("cut_whoosh", "sparkle", "heartbeat", "thud", "pop"):
            # Warm Butterworth-filtered cinema foley (inspired by faceless-shorts; nothing harsh > 4.5 kHz)
            import numpy as np
            from scipy import signal
            nrng = np.random.default_rng(seed)
            nn = max(64, int(seconds * SR))
            t_arr = np.arange(nn, dtype=np.float64) / SR
            def _lp(x, fc):
                b, a = signal.butter(2, min(fc, SR / 2 - 100) / (SR / 2), "low")
                return signal.lfilter(b, a, x)
            if kind == "cut_whoosh":
                raw_n = nrng.standard_normal(nn)
                lo, hi = _lp(raw_n, 480), _lp(raw_n, 2100)
                k = np.sin(np.pi * np.linspace(0, 1, nn))
                sig = (lo * (1.0 - k) + hi * k) * (k ** 1.4)
            elif kind == "sparkle":
                sig = np.zeros(nn)
                for idx_f, freq in enumerate((587.3, 739.99, 880.0, 1174.66, 1318.51)):
                    st = int(idx_f * 0.14 * SR)
                    seg_n = min(nn - st, int(0.45 * SR))
                    if seg_n > 10:
                        ts = np.arange(seg_n) / SR
                        e = np.minimum(1.0, ts / 0.008) * np.exp(-ts * 6.0)
                        sig[st:st + seg_n] += (np.sin(2 * np.pi * freq * ts) + 0.28 * np.sin(4 * np.pi * freq * ts)) * e * 0.25
                sig = _lp(sig, 4200)
            elif kind == "heartbeat":
                sig = np.zeros(nn)
                for offset, freq, amp in ((0.05, 58.0, 0.9), (0.28, 50.0, 0.65)):
                    st = int(offset * SR)
                    seg_n = min(nn - st, int(0.22 * SR))
                    if seg_n > 10:
                        ts = np.arange(seg_n) / SR
                        sig[st:st + seg_n] += np.sin(2 * np.pi * freq * ts) * np.minimum(1.0, ts / 0.006) * np.exp(-ts * 12.0) * amp
                sig = _lp(sig, 280)
            elif kind == "thud":
                sig = np.sin(2 * np.pi * (68.0 + 38.0 * np.exp(-t_arr * 28.0)) * t_arr) * np.exp(-t_arr * 11.0)
                sig = _lp(sig + _lp(nrng.standard_normal(nn), 220) * np.exp(-t_arr * 18.0) * 0.25, 650)
            else:  # pop
                f_sw = 520.0 * np.exp(-t_arr * 12.0) + 180.0
                sig = _lp(np.sin(2 * np.pi * np.cumsum(f_sw) / SR) * np.minimum(1.0, t_arr / 0.003) * np.exp(-t_arr * 24.0), 2600)
            peak_s = float(np.max(np.abs(sig))) or 1.0
            return (sig * min(1.0, 0.78 / peak_s)).tolist()
        else:
            lp += 0.02 * (noise - lp)
            v = lp * 0.1
        out[i] = v
    peak = max(map(abs, out), default=1.0) or 1.0
    return [x * min(1.0, 0.75 / peak) for x in out]


def classify_sfx(description: str, action: str, shot: str) -> list[str]:
    text = f"{description} {action} {shot}".lower()
    cues = []
    checks = [
        ("morning_birds", ("morning", "sunrise", "dawn", "bird", "सुबह", "सवेरा", "चिड़िया", "sunlight")),
        ("kitchen_ambience", ("kitchen", "tiffin", "रसोई", "टिफ़िन", "बक्सा", "counter", "kettle", "chai", "tea")),
        ("paper_note", ("note", "paper", "letter", "नोट", "कागज़", "कागज", "लिखावट", "write", "notebook")),
        ("train_station", ("train", "station", "platform", "रेल", "स्टेशन")),
        ("clock_tick", ("clock", "watch", "घड़ी", "टिक")),
        ("door_creak", ("door", "shutter", "दरवाज़", "दरवाज", "दुकान बंद", "खुला", "चरमर")),
        ("footsteps", ("step", "walk", "foot", "चल", "कदम", "सीढ़", "लौटा")),
        ("candle_flame", ("candle", "lantern", "lamp", "लालटेन", "मोमबत्ती", "लौ", "flame")),
        ("match_strike", ("match", "माचिस", "strike")),
        ("night_crickets", ("night", "street", "evening", "रात", "फुटपाथ", "सड़क")),
        ("wind", ("wind", "breeze", "हवा", "अंधेरा", "अँधेरा")),
        ("warm_chime", ("hope", "smile", "school", "मुस्कुर", "उम्मीद", "पाठशाला", "रोशनी", "गर्व")),
        ("cloth", ("cloth", "book", "page", "कपड़ा", "किताब", "दुपट्टा", "रोटी")),
    ]
    for kind, words in checks:
        if any(word in text for word in words):
            cues.append(kind)
    return cues[:2] or ["room_tone"]


def api_request(url: str, key: str, payload: dict, timeout: int = 180) -> tuple[bytes, dict]:
    """Make one non-idempotent generation request; never auto-retry billable POSTs."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            headers = dict(response.headers.items())
            if not raw:
                raise RuntimeError("Audio API returned an empty response.")
            return raw, headers
    except urllib.error.HTTPError as exc:
        detail = exc.read(1600).decode("utf-8", "replace")
        raise RuntimeError(f"ElevenLabs API HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs generation request failed without retry to avoid duplicate billing: {exc}") from exc


def make_music_prompt(plan: dict, mood: str) -> str:
    style = canonical_style(mood)
    identity = MUSIC_IDENTITIES[style]
    title = plan.get("title", "emotional Hindi short film")
    arc = plan.get("emotional_arc") or (
        "memorable light opening motif, gentle rhythmic storytelling flow, "
        "a tasteful rise at the emotional turning point, and a warm hopeful ending"
    )
    dur = float(plan.get("duration_seconds") or 40.0)
    return (
        f"Create an original instrumental score for a {dur:.0f}-second modern Hindi emotional storytelling short titled '{title}'. "
        f"Musical identity: {identity['label']} in {identity['key']} around {identity['bpm']} BPM. "
        f"Arrangement: {identity['prompt_style']}. Emotional arc: {arc}. "
        "Include a memorable opening hook and real melodic development rather than a static ambient pad or repeating loop. "
        "Keep midrange clear for spoken Hindi narration. No vocals, no lyrics, no spoken words, no trailer booms, no EDM drops."
    )[:4000]


def build_cues(plan: dict, max_sfx: int) -> list[dict]:
    """Build at least one motivated SFX/ambience cue per scene, unless explicitly capped."""
    cues: list[dict] = []
    dedupe: set[tuple[str, str, float]] = set()
    for idx, scene in enumerate(plan.get("scenes", [])):
        scene_id = str(scene.get("id") or f"scene_{idx + 1:02d}")
        scene_start = float(scene.get("start", 0))
        scene_end = float(scene.get("end", scene_start + 1.0))
        if scene_end <= scene_start:
            raise ValueError(f"Scene {scene_id} has invalid start/end times for SFX planning.")
        added_scene_event = False
        raw = scene.get("sfx_events")
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                prompt = str(entry.get("prompt") or entry.get("text") or "").strip()
                if not prompt:
                    continue
                requested_start = float(entry.get("start", scene_start + 0.35))
                start = min(max(requested_start, scene_start), max(scene_start, scene_end - 0.02))
                available = max(0.10, scene_end - start)
                requested_duration = float(entry.get("duration", 1.8))
                duration = min(4.0, max(0.10, requested_duration), available)
                volume = max(0.03, min(0.5, float(entry.get("volume", 0.18))))
                kind = str(entry.get("kind") or "custom")
                key = (scene_id, prompt.lower(), round(start, 2))
                if key in dedupe:
                    continue
                dedupe.add(key)
                cues.append({"scene": scene_id, "kind": kind, "prompt": prompt,
                             "start": round(start, 3), "duration": round(duration, 3), "volume": volume})
                added_scene_event = True
        if added_scene_event:
            continue

        desc = str(scene.get("sfx", ""))
        kinds = classify_sfx(desc, str(scene.get("action", "")), str(scene.get("shot", "")))
        kind = kinds[0]
        if desc.strip() and len(desc.strip()) > 45:
            prompt = desc.strip()
        elif desc.strip():
            location = str(scene.get("location") or scene.get("setting") or "the scene's environment")
            prompt = f"{desc.strip()} in {location}. Natural, close-perspective cinematic foley, restrained and realistic, no dialogue and no music."
        else:
            prompt = SFX_PROMPTS.get(kind, SFX_PROMPTS["room_tone"])
        is_ambience = kind in ("morning_birds", "kitchen_ambience", "train_station", "night_crickets", "room_tone", "wind", "clock_tick", "candle_flame")
        offset = 0.05 if is_ambience else min(0.35, max(0.05, (scene_end - scene_start) * 0.22))
        start = min(scene_end - 0.02, scene_start + offset)
        duration = max(0.10, scene_end - start) if is_ambience else min(2.4, max(0.10, scene_end - start))
        key = (scene_id, prompt.lower(), round(start, 2))
        if key not in dedupe:
            dedupe.add(key)
            cues.append({"scene": scene_id, "kind": kind, "prompt": prompt,
                         "start": round(start, 3), "duration": round(duration, 3),
                         "volume": 0.015 if kind == "room_tone" else (0.075 if is_ambience else 0.10)})

    primary: list[dict] = []
    secondary: list[dict] = []
    seen_scenes: set[str] = set()
    for cue in cues:
        if cue["scene"] not in seen_scenes:
            primary.append(cue)
            seen_scenes.add(cue["scene"])
        else:
            secondary.append(cue)
    # Optional micro-foley (disabled by default so piano & narration stay 100% clean)
    if os.getenv("CINESTORY_ENABLE_MICRO_FOLEY", "0") == "1":
        for idx, scene in enumerate(plan.get("scenes", [])):
            sid = str(scene.get("id") or f"scene_{idx + 1:02d}")
            s_start = float(scene.get("start", 0))
            s_end = float(scene.get("end", s_start + 1.0))
            if idx > 0 and (s_end - s_start) > 0.5:
                secondary.append({
                    "scene": sid, "kind": "cut_whoosh", "prompt": SFX_PROMPTS["cut_whoosh"],
                    "start": round(s_start, 3), "duration": min(0.42, s_end - s_start), "volume": 0.09,
                })
            for ev in (scene.get("word_events") or []):
                t_ev = float(ev.get("time", -1))
                sfx_k = str(ev.get("sfx_kind") or "sparkle")
                if s_start <= t_ev < s_end - 0.12 and sfx_k in SFX_LIBRARY:
                    secondary.append({
                        "scene": sid, "kind": sfx_k, "prompt": SFX_PROMPTS.get(sfx_k, SFX_PROMPTS["sparkle"]),
                        "start": round(t_ev, 3), "duration": min(1.0, s_end - t_ev), "volume": 0.12,
                    })
    return (primary + secondary)[:max_sfx]


def library_root(project_root: Path) -> Path:
    override = os.getenv("CINESTORY_AUDIO_LIBRARY")
    if override:
        return Path(override).expanduser().resolve()
    project = project_root / "audio_library"
    bundled = Path(__file__).resolve().parents[1] / "assets" / "audio-library"
    return project if project.exists() else bundled


def ensure_library_assets(root: Path, force_rebuild_music: bool = False) -> None:
    """Create genuinely distinct original fallback library files if absent (or rebuilding)."""
    music_dir = root / "music"
    sfx_dir = root / "sfx"
    music_dir.mkdir(parents=True, exist_ok=True)
    sfx_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    for style_key, filename in CANONICAL_TO_FILENAME.items():
        target = music_dir / filename
        if target.exists() and not force_rebuild_music:
            continue
        temp = music_dir / f".{style_key}.wav"
        make_music(60.0, temp, style_key)
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(temp),
                 "-codec:a", "libmp3lame", "-b:a", "192k", str(target)],
                check=True,
            )
            temp.unlink(missing_ok=True)
        else:
            temp.replace(music_dir / f"{style_key}.wav")
    for idx, kind in enumerate(SFX_LIBRARY):
        target = sfx_dir / SFX_LIBRARY[kind]
        if target.exists() and not force_rebuild_music:
            continue
        duration = {"room_tone": 3.5, "wind": 3.5, "door_creak": 1.3, "footsteps": 1.8, "candle_flame": 2.6,
                    "match_strike": 0.6, "warm_chime": 2.0, "soft_whoosh": 1.0, "cloth": 1.2,
                    "morning_birds": 4.0, "kitchen_ambience": 3.5, "paper_note": 1.4,
                    "train_station": 4.0, "clock_tick": 3.5, "night_crickets": 4.0,
                    "cut_whoosh": 0.42, "sparkle": 1.1, "heartbeat": 0.75, "thud": 0.40, "pop": 0.16}[kind]
        wav_path = sfx_dir / f".{kind}.wav"
        write_mono_wav(wav_path, sfx_samples(kind, duration, 97 + idx))
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path),
                 "-codec:a", "libmp3lame", "-b:a", "96k", str(target)],
                check=True,
            )
            wav_path.unlink(missing_ok=True)
        else:
            wav_path.replace(sfx_dir / f"{kind}.wav")

    index_path = root / "index.json"
    if not index_path.exists() or force_rebuild_music:
        index_data = {
            "name": "CineStory v10 Original Audio Fallback Library",
            "license": "Original procedural multi-instrument audio generated by CineStory v10 scripts; no third-party recordings bundled.",
            "music": {
                "light_cinematic_motivation": ["music/hopeful.mp3"],
                "warm_acoustic_storytelling": ["music/warm_emotional.mp3"],
                "emotional_uplifting_score": ["music/moody.mp3"],
                "hopeful": ["music/hopeful.mp3"],
                "warm_emotional": ["music/warm_emotional.mp3"],
                "moody": ["music/moody.mp3"],
            },
            "music_identities": {k: {"label": v["label"], "key": v["key"], "bpm": v["bpm"], "file": f"music/{CANONICAL_TO_FILENAME[k]}"} for k, v in MUSIC_IDENTITIES.items()},
            "sfx": {k: [f"sfx/{v}"] for k, v in SFX_LIBRARY.items()},
        }
        temp_index = index_path.with_suffix(".json.tmp")
        temp_index.write_text(json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_index.replace(index_path)


def find_library_sfx(root: Path, kind: str) -> Path:
    mapped = kind if kind in SFX_LIBRARY else "soft_whoosh"
    direct = root / "sfx" / SFX_LIBRARY[mapped]
    if direct.is_file():
        return direct
    for ext in (".mp3", ".wav", ".ogg"):
        candidate = root / "sfx" / f"{mapped}{ext}"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No valid library SFX file for {kind!r} (fallback {mapped!r}) under {root}")


def validate_generated_audio(path: Path) -> dict:
    """Reject empty/invalid provider responses before they enter the final mix."""
    if not path.is_file() or path.stat().st_size < 256:
        raise RuntimeError("Audio provider returned an empty or implausibly small file.")
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels", "-show_entries", "format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"Generated audio is not decodable: {result.stderr.strip()}")
        details = json.loads(result.stdout)
        streams = details.get("streams", [])
        duration = float(details.get("format", {}).get("duration", 0) or 0)
        if not streams or duration <= 0:
            raise RuntimeError("Generated audio has no decodable audio stream or positive duration.")
        return {"duration_seconds": round(duration, 3), **streams[0]}
    head = path.read_bytes()[:16]
    if not (head.startswith(b"ID3") or any(head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0 for i in range(len(head) - 1))):
        raise RuntimeError("Provider response does not look like an MP3 audio stream (ffprobe unavailable).")
    return {"validation": "mp3-header-only; ffprobe unavailable"}


def write_and_validate_audio(raw: bytes, output: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_bytes(raw)
    try:
        validation = validate_generated_audio(temp)
        temp.replace(output)
        return validation
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def generate_ai_music(key: str, prompt: str, duration: float, output: Path, seed: int | None = None) -> dict:
    length_ms = int(max(3.0, min(600.0, duration)) * 1000)
    model_id = os.getenv("ELEVENLABS_MUSIC_MODEL", "music_v2_5").strip() or "music_v2_5"
    payload: dict = {"prompt": prompt, "music_length_ms": length_ms, "model_id": model_id, "force_instrumental": True}
    if seed is not None:
        payload["seed"] = int(seed)
    raw, headers = api_request("https://api.elevenlabs.io/v1/music?output_format=mp3_48000_192", key, payload, timeout=300)
    validation = write_and_validate_audio(raw, output)
    return {
        "provider": "elevenlabs",
        "model": model_id,
        "song_id": headers.get("song-id") or headers.get("Song-Id"),
        "bytes": len(raw),
        "sha256": sha256_file(output),
        "validation": validation,
    }


def generate_ai_sfx(key: str, prompt: str, duration: float, output: Path) -> dict:
    payload = {"text": prompt[:440], "loop": False, "prompt_influence": 0.78, "model_id": "eleven_text_to_sound_v3"}
    raw, headers = api_request("https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128", key, payload, timeout=180)
    validation = write_and_validate_audio(raw, output)
    return {
        "provider": "elevenlabs",
        "model": "eleven_text_to_sound_v3",
        "character_cost": headers.get("character-cost"),
        "generation_duration": "model-selected; trimmed by final mixer",
        "mix_window_seconds": round(duration, 2),
        "bytes": len(raw),
        "validation": validation,
    }


def generate_music_auditions(plan: dict, out_dir: Path, root: Path, mode: str, key: str, audition_seconds: float = 18.0) -> dict:
    """Create 3 genuinely distinct music auditions and verify hash + spectral distinctness."""
    aud_dir = out_dir / "music_auditions"
    aud_dir.mkdir(parents=True, exist_ok=True)
    options: list[dict] = []
    fallback_reasons: list[str] = []
    active_mode = mode

    for idx, (style_key, identity) in enumerate(MUSIC_IDENTITIES.items(), 1):
        prompt = make_music_prompt(plan, style_key)
        dest = aud_dir / f"audition_{idx:02d}_{style_key}.mp3"
        local_src = root / "music" / CANONICAL_TO_FILENAME[style_key]
        if active_mode == "elevenlabs" and key:
            try:
                info = generate_ai_music(key, prompt, audition_seconds, dest, seed=identity["seed"])
                record = {
                    "option": idx,
                    "style": style_key,
                    "label": identity["label"],
                    "key": identity["key"],
                    "bpm": identity["bpm"],
                    "file": str(dest),
                    "prompt": prompt,
                    "provider": "elevenlabs",
                    "model": info["model"],
                    "source": "ai",
                    "duration_seconds": info["validation"].get("duration_seconds", audition_seconds),
                    "sha256": info["sha256"],
                    "spectral_fingerprint": compute_audio_fingerprint(dest),
                }
                options.append(record)
                continue
            except Exception as exc:
                fallback_reasons.append(f"audition_{idx}_{style_key}: {exc}")
                active_mode = "library"

        # Honest fallback to distinct bundled composition (never duplicating the same file!)
        shutil.copy2(local_src, dest)
        val = validate_generated_audio(dest)
        options.append({
            "option": idx,
            "style": style_key,
            "label": identity["label"],
            "key": identity["key"],
            "bpm": identity["bpm"],
            "file": str(dest),
            "prompt": prompt,
            "provider": "CineStory v10 Original Audio Fallback Library",
            "model": "procedural_multi_instrument_v10",
            "source": "library_fallback",
            "duration_seconds": val.get("duration_seconds", 60.0),
            "sha256": sha256_file(dest),
            "spectral_fingerprint": compute_audio_fingerprint(dest),
        })

    distinctness = verify_distinct_tracks(options)
    if not distinctness["distinct"]:
        raise RuntimeError(f"Music audition distinctness check failed: {json.dumps(distinctness)}")

    result = {
        "version": "10.0",
        "auditions": options,
        "distinctness_check": distinctness,
        "fallback_reasons": fallback_reasons,
    }
    (out_dir / "music_auditions.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def resolve_max_sfx(requested: int | None, configured: str | None, scene_count: int) -> int:
    """Choose a safe cue cap that always covers every scene by default."""
    if scene_count < 1:
        raise ValueError("A story plan must contain at least one scene.")
    if requested is not None:
        value = requested
    else:
        try:
            value = int(configured or "32")
        except ValueError as exc:
            raise ValueError("CINESTORY_MAX_SFX_COUNT must be an integer.") from exc
        value = max(value, scene_count)
    if value < 0 or value > 100:
        raise ValueError("Maximum SFX cues must be between 0 and 100.")
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan")
    ap.add_argument("--out", default="audio")
    ap.add_argument(
        "--music-style",
        choices=("warm_emotional", "hopeful", "moody", "light_cinematic_motivation", "warm_acoustic_storytelling", "emotional_uplifting_score"),
        default="warm_acoustic_storytelling",
    )
    ap.add_argument("--provider", choices=("auto", "elevenlabs", "library"), default=os.getenv("CINESTORY_AUDIO_PROVIDER", "auto"))
    ap.add_argument("--max-sfx", type=int, default=None, help="Maximum timed SFX cues. Default is max(16, scene count).")
    ap.add_argument("--auditions", action="store_true", help="Generate 3 genuinely distinct music auditions and exit.")
    ap.add_argument("--audition-seconds", type=float, default=18.0)
    ap.add_argument("--rebuild-library", action="store_true", help="Regenerate the 3 distinct fallback library music tracks.")
    ap.add_argument("--no-music", action="store_true")
    ap.add_argument("--no-sfx", action="store_true")
    args = ap.parse_args()

    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    load_env(plan_path.parent)
    scene_count = len(plan.get("scenes", [])) if isinstance(plan.get("scenes"), list) else 0
    try:
        args.max_sfx = resolve_max_sfx(args.max_sfx, os.getenv("CINESTORY_MAX_SFX_COUNT", "32"), scene_count)
    except ValueError as error:
        ap.error(str(error))
    if not args.no_sfx and not args.auditions and args.max_sfx < scene_count:
        ap.error(f"--max-sfx must be at least the scene count ({scene_count}) to guarantee one SFX cue per scene, or explicitly pass --no-sfx.")

    duration = float(plan.get("duration_seconds") or max(float(s["end"]) for s in plan["scenes"]))
    out = Path(args.out)
    out = out if out.is_absolute() else plan_path.parent / out
    out.mkdir(parents=True, exist_ok=True)
    sfx_dir = out / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    root = library_root(plan_path.parent)
    ensure_library_assets(root, force_rebuild_music=args.rebuild_library)

    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    mode = args.provider
    fallback_reasons: list[str] = []
    if mode == "auto":
        if key:
            mode = "elevenlabs"
        else:
            mode = "library"
            fallback_reasons.append("ELEVENLABS_API_KEY is not configured; used the bundled CineStory v10 Original Audio Fallback Library.")
    if mode == "elevenlabs" and not key:
        reason = "ELEVENLABS_API_KEY not configured; used the bundled CineStory v10 Original Audio Fallback Library."
        print(reason, file=sys.stderr)
        fallback_reasons.append(reason)
        mode = "library"

    if args.auditions:
        aud = generate_music_auditions(plan, out, root, mode, key, args.audition_seconds)
        print(f"MUSIC AUDITIONS READY: {len(aud['auditions'])} distinct options (distinct={aud['distinctness_check']['distinct']})")
        return 0

    assets: dict = {}
    generated: list[str] = []
    music_manifest: dict = {}
    chosen_style = canonical_style(args.music_style)
    identity = MUSIC_IDENTITIES[chosen_style]

    if not args.no_music:
        ai_path = out / "music_ai.mp3"
        local_path = root / "music" / CANONICAL_TO_FILENAME[chosen_style]
        prompt = make_music_prompt(plan, chosen_style)
        if mode == "elevenlabs":
            try:
                info = generate_ai_music(key, prompt, duration, ai_path, seed=identity["seed"])
                assets["music"] = {"file": str(ai_path), "source": "ai", "provider": info}
                generated.append("music:elevenlabs")
                music_manifest = {
                    "version": "10.0",
                    "style": chosen_style,
                    "label": identity["label"],
                    "file": str(ai_path),
                    "prompt": prompt,
                    "provider": "elevenlabs",
                    "model": info["model"],
                    "source": "ai",
                    "is_fallback": False,
                    "duration_seconds": info["validation"].get("duration_seconds", duration),
                    "sha256": info["sha256"],
                    "spectral_fingerprint": compute_audio_fingerprint(ai_path),
                    "fallback_reason": None,
                }
            except Exception as exc:
                fallback_reasons.append(f"music: {exc}")
                mode = "library"
                if local_path.exists():
                    val = validate_generated_audio(local_path)
                    assets["music"] = {"file": str(local_path), "source": "library_fallback", "provider": "CineStory v10 Original Audio Fallback Library"}
                    music_manifest = {
                        "version": "10.0",
                        "style": chosen_style,
                        "label": identity["label"],
                        "file": str(local_path),
                        "prompt": prompt,
                        "provider": "CineStory v10 Original Audio Fallback Library",
                        "model": "procedural_multi_instrument_v10",
                        "source": "library_fallback",
                        "is_fallback": True,
                        "duration_seconds": val.get("duration_seconds", 60.0),
                        "sha256": sha256_file(local_path),
                        "spectral_fingerprint": compute_audio_fingerprint(local_path),
                        "fallback_reason": str(exc),
                    }
                else:
                    raise
        else:
            val = validate_generated_audio(local_path)
            assets["music"] = {"file": str(local_path), "source": "library_fallback", "provider": "CineStory v10 Original Audio Fallback Library"}
            music_manifest = {
                "version": "10.0",
                "style": chosen_style,
                "label": identity["label"],
                "file": str(local_path),
                "prompt": prompt,
                "provider": "CineStory v10 Original Audio Fallback Library",
                "model": "procedural_multi_instrument_v10",
                "source": "library_fallback",
                "is_fallback": True,
                "duration_seconds": val.get("duration_seconds", 60.0),
                "sha256": sha256_file(local_path),
                "spectral_fingerprint": compute_audio_fingerprint(local_path),
                "fallback_reason": fallback_reasons[0] if fallback_reasons else "library provider explicitly requested",
            }
        # Also record distinctness across the 3 bundled fallback library tracks for QA auditability.
        lib_checks = []
        for sk, fn in CANONICAL_TO_FILENAME.items():
            p = root / "music" / fn
            if p.is_file():
                lib_checks.append({"style": sk, "file": str(p), "sha256": sha256_file(p), "spectral_fingerprint": compute_audio_fingerprint(p)})
        music_manifest["library_distinctness_check"] = verify_distinct_tracks(lib_checks)
        (out / "music_manifest.json").write_text(json.dumps(music_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    events = []
    cue_cache: dict[str, Path] = {}
    if not args.no_sfx:
        cues = build_cues(plan, args.max_sfx)
        for index, cue in enumerate(cues, 1):
            cue_id = f"sfx_{index:02d}_{cue['kind']}"
            dest = sfx_dir / f"{cue_id}.mp3"
            cache_key = cue["prompt"].strip().lower()
            if cache_key in cue_cache:
                source = cue_cache[cache_key]
                provider_info = {"provider": "reused-identical-prompt"}
                source_type = "reused"
            elif mode == "elevenlabs":
                try:
                    provider_info = generate_ai_sfx(key, cue["prompt"], cue["duration"], dest)
                    source = dest
                    source_type = "ai"
                    cue_cache[cache_key] = source
                    generated.append(f"sfx:{cue_id}")
                except Exception as exc:
                    fallback_reasons.append(f"{cue_id}: {exc}")
                    mode = "library"
                    source = find_library_sfx(root, cue["kind"])
                    provider_info = {"provider": "CineStory v10 Original Audio Fallback Library", "fallback_kind": cue["kind"] if cue["kind"] in SFX_LIBRARY else "soft_whoosh"}
                    source_type = "library"
                    cue_cache[cache_key] = source
            else:
                source = find_library_sfx(root, cue["kind"])
                provider_info = {"provider": "CineStory v10 Original Audio Fallback Library", "fallback_kind": cue["kind"] if cue["kind"] in SFX_LIBRARY else "soft_whoosh"}
                source_type = "library"
                cue_cache[cache_key] = source
            events.append({
                "scene": cue["scene"], "kind": cue["kind"], "prompt": cue["prompt"],
                "file": str(source), "start": round(cue["start"], 3),
                "duration": cue["duration"], "volume": cue["volume"],
                "source": source_type, "provider": provider_info,
            })

    manifest = {
        "version": "10.0",
        "duration_seconds": duration,
        "music": assets.get("music", {}).get("file"),
        "music_source": assets.get("music", {}).get("source"),
        "music_provider": assets.get("music", {}).get("provider"),
        "music_style": chosen_style if not args.no_music else None,
        "music_sha256": music_manifest.get("sha256"),
        "music_manifest": str(out / "music_manifest.json") if music_manifest else None,
        "sfx_events": events,
        "audio_provider_requested": args.provider,
        "audio_provider_resolved": mode,
        "ai_assets_generated": generated,
        "fallback_reasons": fallback_reasons,
        "mix_notes": "Music is sidechain-ducked under narration by render_video.py with natural pause lift and clean fade-in/out; SFX are scene-timed and gain-limited.",
    }
    (out / "audio_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"AUDIO READY: music={assets.get('music', {}).get('source', 'off')} ({chosen_style}), SFX={len(events)}, AI assets={len(generated)}, fallback issues={len(fallback_reasons)}")
    if fallback_reasons:
        print("AUDIO FALLBACKS:", json.dumps(fallback_reasons, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
