#!/usr/bin/env python3
"""Master narration alignment and timeline synchronization engine for CineStory AI v10.

The narration audio is the master timeline — NOT the storyboard's estimated durations.
This script:
1. Probes the actual duration of the approved narration audio.
2. Obtains word-level timestamps using (in priority order, or as configured):
   - Provider-returned timestamps from ElevenLabs TTS (`voice_alignment_raw.json`).
   - ElevenLabs Forced Alignment API (`POST /v1/forced-alignment`) for uploaded audio.
   - Local Hindi-capable forced aligner (`whisperx` or `faster_whisper`) when installed.
   - Explicit `vad_fallback` (speech-activity energy segmentation + syllable-weighted
     distribution) clearly marked as `is_fallback: true` — never pretending to be exact.
3. Groups spoken words into short 2-3 word semantic phrases (never splitting Devanagari
   words, matras, or conjuncts) for CineStory Subtitle Cards.
4. Maps every scene in `story_plan.json` to its spoken phrase boundaries so visual cuts
   follow the narration at variable, natural durations.
5. Writes `audio_alignment.json`, updates `story_plan.json` (preserving planned times),
   and writes phrase-aligned `captions.srt`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_ROOT = "https://api.elevenlabs.io/v1"
KEY_ENV = "ELEVENLABS_API_KEY"

HIGHLIGHT_WORDS = {
    "माँ", "मां", "पिता", "बेटा", "बेटी", "भूखा", "भूख", "अंधेरा", "अँधेरा", "रोशनी", "उम्मीद",
    "प्यार", "सपना", "सपने", "हिम्मत", "मदद", "दुआ", "किस्मत", "सफल", "सफलता", "घर", "मुस्कुराए",
    "मुस्कुराई", "मुस्कुराया", "अकेला", "अकेली", "आखिरी", "आख़िरी", "साथ", "जिंदगी", "ज़िंदगी",
    "खुशी", "खुशियां", "कबीर", "चायवाले", "चायवाला", "लालटेन", "किताब", "थकान", "गरीबी",
    "रोटी", "इंजीनियर", "पाठशाला", "मुफ़्त", "घड़ीसाज़", "घड़ी", "संतोष", "सोफिया", "समय",
}

CLAUSE_END_PUNCT = set("।॥.!?…")
SOFT_BREAK_PUNCT = set(",;:—–-\"“”'‘’()")


def load_env(project_root: Path) -> None:
    for candidate in (project_root / ".env", Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_audio_duration(audio_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to measure actual narration duration.")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio_path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {audio_path}: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration") or 0.0)
    if not math.isfinite(duration) or duration <= 0.0:
        raise RuntimeError(f"Narration audio has invalid duration: {duration}")
    return round(duration, 3)


def strip_edge_punctuation(token: str) -> str:
    """Strip leading/trailing punctuation while keeping Devanagari matras/combining marks intact."""
    start, end = 0, len(token)
    while start < end and unicodedata.category(token[start])[0] in {"P", "S"}:
        start += 1
    while end > start and unicodedata.category(token[end - 1])[0] in {"P", "S"}:
        end -= 1
    return token[start:end]


def normalize_token(token: str) -> str:
    """Normalize a word for alignment matching (preserves Devanagari letters & matras)."""
    bare = strip_edge_punctuation(unicodedata.normalize("NFC", token)).lower()
    bare = bare.replace("\u093c", "")
    return bare


def tokenize_transcript(text: str) -> list[str]:
    """Split text into complete whitespace-delimited words (never splitting Devanagari clusters)."""
    cleaned = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()
    if not cleaned:
        return []
    return [tok for tok in cleaned.split(" ") if tok.strip()]


def estimate_word_weight(word: str) -> float:
    """Estimate relative spoken duration weight for a word (Devanagari syllable-aware)."""
    bare = strip_edge_punctuation(word)
    if not bare:
        return 0.4
    dev_bases = sum(1 for ch in bare if ("\u0904" <= ch <= "\u0939") or ("\u0958" <= ch <= "\u0961"))
    if dev_bases > 0:
        viramas = bare.count("\u094d")
        syllables = max(1, dev_bases - viramas)
        weight = 0.75 + 0.45 * syllables
    else:
        weight = max(1.0, len(bare) * 0.35)
    if word and word[-1] in CLAUSE_END_PUNCT:
        weight += 0.55
    elif word and word[-1] in SOFT_BREAK_PUNCT:
        weight += 0.25
    return weight


def characters_to_words(alignment: dict) -> list[dict]:
    """Convert ElevenLabs character-level alignment arrays into Devanagari-safe word timestamps."""
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    n = min(len(chars), len(starts), len(ends))
    words: list[dict] = []
    buf: list[str] = []
    w_start: float | None = None
    w_end: float | None = None

    def flush() -> None:
        nonlocal buf, w_start, w_end
        token = unicodedata.normalize("NFC", "".join(buf)).strip()
        if token and w_start is not None and w_end is not None:
            words.append({
                "word": token,
                "start": round(max(0.0, float(w_start)), 3),
                "end": round(max(float(w_start) + 0.02, float(w_end)), 3),
                "confidence": 0.95,
            })
        buf = []
        w_start = None
        w_end = None

    for i in range(n):
        ch = str(chars[i])
        st = float(starts[i])
        en = float(ends[i])
        if ch.isspace():
            flush()
        else:
            if w_start is None:
                w_start = st
            w_end = en
            buf.append(ch)
    flush()
    return words


def _multipart_form_data(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----CineStoryBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(value.encode("utf-8") + b"\r\n")
    mime = "audio/mpeg" if file_path.suffix.lower() == ".mp3" else "application/octet-stream"
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
    )
    parts.append(file_path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def align_with_elevenlabs_forced(audio_path: Path, transcript: str, api_key: str, timeout: int = 180) -> tuple[list[dict], dict]:
    """Align uploaded or existing narration via official ElevenLabs POST /v1/forced-alignment."""
    body, content_type = _multipart_form_data({"text": transcript}, "file", audio_path)
    req = urllib.request.Request(
        f"{API_ROOT}/forced-alignment",
        data=body,
        headers={"xi-api-key": api_key, "Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", "replace")
        raise RuntimeError(f"ElevenLabs Forced Alignment HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs Forced Alignment failed (not retried): {exc}") from exc

    raw_words = payload.get("words") or []
    words: list[dict] = []
    for item in raw_words:
        text = unicodedata.normalize("NFC", str(item.get("text", ""))).strip()
        if not text:
            continue
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start + 0.05))
        loss = item.get("loss")
        confidence = round(max(0.05, min(1.0, math.exp(-max(0.0, float(loss))))), 3) if loss is not None else 0.9
        words.append({
            "word": text,
            "start": round(max(0.0, start), 3),
            "end": round(max(start + 0.02, end), 3),
            "confidence": confidence,
        })
    if not words:
        raise RuntimeError("ElevenLabs Forced Alignment returned zero word timestamps.")
    meta = {"provider": "elevenlabs_forced_alignment", "overall_loss": payload.get("loss")}
    return words, meta


def align_with_local_whisper(audio_path: Path, transcript: str, language: str = "hi", prefer: str = "auto") -> tuple[list[dict], dict]:
    """Optional local Hindi-capable forced alignment using whisperx or faster_whisper."""
    lang_code = "hi" if language.lower().startswith("hi") else ("en" if language.lower().startswith("en") else language[:2].lower())

    if prefer in {"auto", "whisperx"}:
        try:
            import whisperx  # type: ignore
            device = "cpu"
            model = whisperx.load_model("small", device, compute_type="int8", language=lang_code)
            audio = whisperx.load_audio(str(audio_path))
            result = model.transcribe(audio, batch_size=4, language=lang_code)
            model_a, metadata = whisperx.load_align_model(language_code=lang_code, device=device)
            aligned = whisperx.align(result["segments"], model_a, metadata, audio, device)
            raw_words = []
            for seg in aligned.get("segments", []):
                for w in seg.get("words", []):
                    if "start" in w and "end" in w and str(w.get("word", "")).strip():
                        raw_words.append({
                            "word": unicodedata.normalize("NFC", str(w["word"]).strip()),
                            "start": round(float(w["start"]), 3),
                            "end": round(float(w["end"]), 3),
                            "confidence": round(float(w.get("score", 0.85)), 3),
                        })
            if raw_words:
                return _reconcile_words_with_transcript(raw_words, transcript), {"provider": "whisperx", "language": lang_code}
        except ImportError:
            if prefer == "whisperx":
                raise RuntimeError("whisperx is not installed. See requirements-alignment.txt.")

    if prefer in {"auto", "faster_whisper"}:
        try:
            from faster_whisper import WhisperModel  # type: ignore
            model = WhisperModel(os.getenv("CINESTORY_WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
            segments, _info = model.transcribe(str(audio_path), language=lang_code, word_timestamps=True)
            raw_words = []
            for seg in segments:
                for w in (seg.words or []):
                    token = unicodedata.normalize("NFC", str(w.word).strip())
                    if token:
                        raw_words.append({
                            "word": token,
                            "start": round(float(w.start), 3),
                            "end": round(float(w.end), 3),
                            "confidence": round(float(getattr(w, "probability", 0.85)), 3),
                        })
            if raw_words:
                return _reconcile_words_with_transcript(raw_words, transcript), {"provider": "faster_whisper", "language": lang_code}
        except ImportError:
            if prefer == "faster_whisper":
                raise RuntimeError("faster-whisper is not installed. See requirements-alignment.txt.")

    raise RuntimeError("No local Hindi forced aligner (whisperx or faster_whisper) is installed.")


def _reconcile_words_with_transcript(asr_words: list[dict], transcript: str) -> list[dict]:
    """Preserve exact approved transcript spelling/Devanagari while inheriting ASR word timings."""
    ref_tokens = tokenize_transcript(transcript)
    if not ref_tokens:
        return asr_words
    if not asr_words:
        return []
    ref_norm = [normalize_token(t) for t in ref_tokens]
    asr_norm = [normalize_token(w["word"]) for w in asr_words]
    from difflib import SequenceMatcher
    matcher = SequenceMatcher(None, ref_norm, asr_norm, autojunk=False)
    mapped: list[dict | None] = [None] * len(ref_tokens)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                src = asr_words[j1 + offset]
                mapped[i1 + offset] = {
                    "word": ref_tokens[i1 + offset],
                    "start": src["start"],
                    "end": src["end"],
                    "confidence": src.get("confidence", 0.85),
                }
        elif tag == "replace" and j2 > j1 and i2 > i1:
            t_start = asr_words[j1]["start"]
            t_end = asr_words[j2 - 1]["end"]
            weights = [estimate_word_weight(ref_tokens[k]) for k in range(i1, i2)]
            total_w = sum(weights) or 1.0
            cur = t_start
            for k, wt in zip(range(i1, i2), weights):
                dur = max(0.03, (t_end - t_start) * (wt / total_w))
                mapped[k] = {"word": ref_tokens[k], "start": round(cur, 3), "end": round(cur + dur, 3), "confidence": 0.72}
                cur += dur
    total_end = asr_words[-1]["end"]
    i = 0
    while i < len(mapped):
        if mapped[i] is not None:
            i += 1
            continue
        gap_start = i
        while i < len(mapped) and mapped[i] is None:
            i += 1
        gap_end = i
        t0 = mapped[gap_start - 1]["end"] if gap_start > 0 else asr_words[0]["start"]
        t1 = mapped[gap_end]["start"] if gap_end < len(mapped) else total_end
        if t1 <= t0:
            t1 = t0 + 0.08 * (gap_end - gap_start)
        weights = [estimate_word_weight(ref_tokens[k]) for k in range(gap_start, gap_end)]
        total_w = sum(weights) or 1.0
        cur = t0
        for k, wt in zip(range(gap_start, gap_end), weights):
            dur = max(0.03, (t1 - t0) * (wt / total_w))
            mapped[k] = {"word": ref_tokens[k], "start": round(cur, 3), "end": round(cur + dur, 3), "confidence": 0.55}
            cur += dur
    return [m for m in mapped if m is not None]


def detect_speech_segments_vad(audio_path: Path, duration: float) -> list[tuple[float, float]]:
    """Detect active speech intervals in an audio file using FFmpeg PCM + RMS energy envelope."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return [(0.0, duration)]
    try:
        import numpy as np
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(audio_path),
            "-ac", "1", "-ar", "16000", "-f", "s16le", "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout:
            return [(0.0, duration)]
        samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        sr = 16000
        hop = int(sr * 0.02)
        win = int(sr * 0.04)
        if len(samples) < win:
            return [(0.0, duration)]
        n_frames = (len(samples) - win) // hop + 1
        rms = np.array([
            float(np.sqrt(np.mean(samples[i * hop: i * hop + win] ** 2)))
            for i in range(n_frames)
        ], dtype=np.float32)
        peak = float(np.percentile(rms, 95)) if len(rms) else 0.0
        if peak < 1e-4:
            return [(0.0, duration)]
        thresh = max(0.008, peak * 0.14)
        active = rms >= thresh
        min_silence_frames = 11
        segments: list[tuple[float, float]] = []
        in_seg = False
        seg_start = 0
        silence_run = 0
        for idx, is_act in enumerate(active):
            if is_act:
                if not in_seg:
                    seg_start = idx
                    in_seg = True
                silence_run = 0
            elif in_seg:
                silence_run += 1
                if silence_run >= min_silence_frames:
                    seg_end = idx - silence_run + 1
                    t0 = max(0.0, seg_start * 0.02 - 0.03)
                    t1 = min(duration, seg_end * 0.02 + 0.04)
                    if t1 - t0 >= 0.12:
                        segments.append((round(t0, 3), round(t1, 3)))
                    in_seg = False
                    silence_run = 0
        if in_seg:
            t0 = max(0.0, seg_start * 0.02 - 0.03)
            t1 = min(duration, (len(active) - silence_run) * 0.02 + 0.04)
            if t1 - t0 >= 0.12:
                segments.append((round(t0, 3), round(t1, 3)))
        return segments or [(0.0, duration)]
    except Exception:
        return [(0.0, duration)]


def align_with_vad_fallback(audio_path: Path, transcript: str, duration: float) -> tuple[list[dict], dict]:
    """Honest local fallback: distribute syllable-weighted words across detected speech segments."""
    tokens = tokenize_transcript(transcript)
    if not tokens:
        raise ValueError("Transcript is empty; cannot align narration.")
    segments = detect_speech_segments_vad(audio_path, duration)
    total_speech = sum(max(0.05, en - st) for st, en in segments)
    weights = [estimate_word_weight(tok) for tok in tokens]
    total_weight = sum(weights) or 1.0

    words: list[dict] = []
    seg_idx = 0
    seg_cursor = segments[0][0]
    for tok, wt in zip(tokens, weights):
        target_dur = max(0.05, total_speech * (wt / total_weight))
        while seg_idx < len(segments) - 1 and seg_cursor >= segments[seg_idx][1] - 0.015:
            seg_idx += 1
            seg_cursor = segments[seg_idx][0]
        w_start = seg_cursor
        remaining = target_dur
        while remaining > 0.005 and seg_idx < len(segments):
            avail = segments[seg_idx][1] - seg_cursor
            if remaining <= avail:
                seg_cursor += remaining
                remaining = 0.0
            else:
                remaining -= max(0.0, avail)
                if seg_idx < len(segments) - 1:
                    seg_idx += 1
                    seg_cursor = segments[seg_idx][0]
                else:
                    seg_cursor = min(duration, segments[seg_idx][1])
                    break
        w_end = max(w_start + 0.04, min(duration, seg_cursor))
        words.append({
            "word": tok,
            "start": round(w_start, 3),
            "end": round(w_end, 3),
            "confidence": 0.45,
        })
    return words, {
        "provider": "vad_speech_envelope_fallback",
        "speech_segments_detected": len(segments),
        "speech_segments": [{"start": s, "end": e} for s, e in segments],
    }


def ensure_monotonic_words(words: list[dict], duration: float) -> list[dict]:
    """Guarantee valid, ordered, non-negative word timestamps bounded by actual voice duration."""
    cleaned: list[dict] = []
    prev_end = 0.0
    for item in words:
        word = unicodedata.normalize("NFC", str(item.get("word", ""))).strip()
        if not word:
            continue
        start = max(prev_end, max(0.0, float(item.get("start", prev_end))))
        end = float(item.get("end", start + 0.08))
        if end <= start:
            end = start + 0.04
        if duration > 0:
            start = min(start, max(0.0, duration - 0.02))
            end = min(duration, max(start + 0.02, end))
        cleaned.append({
            "word": word,
            "start": round(start, 3),
            "end": round(end, 3),
            "confidence": round(float(item.get("confidence", 0.8)), 3),
        })
        prev_end = cleaned[-1]["end"]
    return cleaned


def pick_highlight_word(phrase_words: list[str]) -> str | None:
    """Choose at most one meaningful word in a 2-3 word card for amber emphasis."""
    for token in phrase_words:
        if strip_edge_punctuation(token) in HIGHLIGHT_WORDS:
            return token
    return None


def group_words_into_phrases(
    words: list[dict],
    max_words: int = 3,
    min_words: int = 2,
    pause_threshold: float = 0.28,
    min_duration: float = 0.55,
    max_duration: float = 2.4,
) -> list[dict]:
    """Group aligned words into short 2-3 word semantic phrases without ever splitting a word."""
    if not words:
        return []
    groups: list[list[dict]] = []
    current: list[dict] = []

    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            cur_dur = current[-1]["end"] - current[0]["start"]
            prev_text = current[-1]["word"]
            hard_punct = bool(prev_text and prev_text[-1] in CLAUSE_END_PUNCT)
            soft_punct = bool(prev_text and prev_text[-1] in SOFT_BREAK_PUNCT)

            should_break = False
            w_text = w["word"]
            is_trailing_clause_punct = bool(w_text and w_text[-1] in (CLAUSE_END_PUNCT | SOFT_BREAK_PUNCT))
            if len(current) >= max_words:
                # Keep a short clause-ending word (e.g. 'था,' or 'था।') attached rather than stranding it at the start of the next card.
                if not (len(current) == max_words and is_trailing_clause_punct and len(strip_edge_punctuation(w_text)) <= 4):
                    should_break = True
            elif hard_punct:
                should_break = True
            elif soft_punct and len(current) >= min_words:
                should_break = True
            elif gap >= pause_threshold and len(current) >= min_words:
                should_break = True
            elif cur_dur >= max_duration and len(current) >= min_words:
                should_break = True

            if should_break:
                groups.append(current)
                current = []
        current.append(w)

    if current:
        groups.append(current)

    merged: list[list[dict]] = []
    for grp in groups:
        if (
            len(grp) == 1
            and merged
            and len(merged[-1]) < max_words
            and not (merged[-1][-1]["word"] and merged[-1][-1]["word"][-1] in CLAUSE_END_PUNCT)
            and (grp[0]["start"] - merged[-1][-1]["end"]) < 0.45
        ):
            merged[-1].extend(grp)
        else:
            merged.append(grp)

    phrases: list[dict] = []
    for p_idx, grp in enumerate(merged, 1):
        start = grp[0]["start"]
        end = grp[-1]["end"]
        next_start = merged[p_idx][0]["start"] if p_idx < len(merged) else None
        if end - start < min_duration:
            desired_end = start + min_duration
            if next_start is not None:
                end = min(desired_end, max(end, next_start - 0.04))
            else:
                end = desired_end
        tokens = [item["word"] for item in grp]
        word_timings = [
            {"word": item["word"], "start": round(float(item["start"]), 3), "end": round(float(item["end"]), 3)}
            for item in grp
        ]
        phrases.append({
            "index": p_idx,
            "text": " ".join(tokens),
            "words": tokens,
            "word_timings": word_timings,
            "word_count": len(tokens),
            "start": round(start, 3),
            "end": round(max(start + 0.12, end), 3),
            "highlight_word": pick_highlight_word(tokens),
        })
    return phrases



def detect_vocal_pauses_ffmpeg(audio_path: Path, noise_db: float = -35.0, min_pause: float = 0.14) -> list[tuple[float, float]]:
    """Detect real vocal silences via FFmpeg silencedetect (inspired by bhul-bill/faceless-shorts)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not audio_path.is_file():
        return []
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(audio_path), "-af", f"silencedetect=noise={noise_db}dB:d={min_pause}", "-f", "null", "-"],
            capture_output=True, text=True, timeout=45,
        )
        pauses: list[tuple[float, float]] = []
        cur_start: float | None = None
        for line in proc.stderr.splitlines():
            m_st = re.search(r"silence_start:\s*([0-9.]+)", line)
            if m_st:
                cur_start = float(m_st.group(1))
                continue
            m_en = re.search(r"silence_end:\s*([0-9.]+)", line)
            if m_en and cur_start is not None:
                pauses.append((round(cur_start, 3), round(float(m_en.group(1)), 3)))
                cur_start = None
        return pauses
    except Exception:
        return []


def snap_cut_to_pause(cut_time: float, pauses: list[tuple[float, float]], window: float = 0.35) -> float:
    """Snap a scene cut cleanly into the midpoint of a nearby vocal pause so no word is ever sliced."""
    best = cut_time
    best_dist = window + 1.0
    for ps, pe in pauses:
        mid = (ps + pe) / 2.0
        dist = abs(mid - cut_time)
        if dist <= window and dist < best_dist:
            best = round(mid, 3)
            best_dist = dist
    return best


def extract_word_events(words: list[dict], scene_mapping: list[dict]) -> list[dict]:
    """Identify emotional keyword timestamps (Wd triggers) for camera punch-ins and micro-SFX."""
    events: list[dict] = []
    last_t = -2.0
    dramatic = {"अंधेरा", "अँधेरा", "भूखा", "भूख", "थकान", "गरीबी", "अकेला", "अकेली", "खामोश", "आखिरी", "आख़िरी"}
    hopeful = {"रोशनी", "उम्मीद", "सपने", "सपना", "मुस्कुराया", "मुस्कुराई", "मुस्कुराए", "गर्व", "हमेशा", "खुशी", "पाठशाला", "इंजीनियर", "प्यार", "सफलता"}
    for w in words:
        bare = strip_edge_punctuation(w["word"])
        t = float(w["start"])
        if bare in HIGHLIGHT_WORDS and (t - last_t) >= 1.6:
            kind = "dramatic_beat" if bare in dramatic else ("hopeful_reveal" if bare in hopeful else "emphasis_beat")
            sfx_kind = "heartbeat" if kind == "dramatic_beat" else ("sparkle" if kind == "hopeful_reveal" else "pop")
            owner = scene_mapping[-1]["scene_id"]
            for sm in scene_mapping:
                if sm["aligned_start"] <= t <= sm["aligned_end"] + 1e-6:
                    owner = sm["scene_id"]
                    break
            events.append({
                "word": w["word"],
                "time": round(t, 3),
                "scene_id": owner,
                "event_type": kind,
                "camera_punch": 0.045,
                "sfx_kind": sfx_kind,
            })
            last_t = t
    return events


def map_scenes_to_alignment(plan: dict, words: list[dict], voice_duration: float, pauses: list[tuple[float, float]] | None = None) -> tuple[list[dict], list[dict]]:
    """Map each scene's narration to exact aligned word spans and compute contiguous scene times."""
    scenes = plan.get("scenes") or []
    if not scenes:
        raise ValueError("Story plan has no scenes to align.")
    if not words:
        raise ValueError("No aligned words available for scene mapping.")

    word_norms = [normalize_token(w["word"]) for w in words]
    total_words = len(words)
    cursor = 0
    raw_spans: list[tuple[int, int]] = []

    for s_idx, scene in enumerate(scenes):
        s_tokens = tokenize_transcript(str(scene.get("narration") or ""))
        s_norms = [normalize_token(t) for t in s_tokens if normalize_token(t)]
        remaining_scenes = len(scenes) - s_idx - 1

        if s_idx == len(scenes) - 1:
            start_i = min(cursor, total_words - 1)
            end_i = total_words - 1
            raw_spans.append((start_i, end_i))
            cursor = total_words
            continue

        if not s_norms:
            start_i = min(cursor, total_words - 1)
            end_i = start_i
            raw_spans.append((start_i, end_i))
            continue

        max_end_allowed = max(cursor, total_words - remaining_scenes - 1)
        expected_len = len(s_norms)
        best_score = -1.0
        best_span = (cursor, min(max_end_allowed, cursor + max(0, expected_len - 1)))

        search_start_hi = min(max_end_allowed, cursor + 3)
        for st in range(cursor, search_start_hi + 1):
            for delta in range(-2, 4):
                en = st + expected_len - 1 + delta
                if en < st or en > max_end_allowed:
                    continue
                cand = word_norms[st: en + 1]
                ci = 0
                hits = 0
                for tok in s_norms:
                    while ci < len(cand) and cand[ci] != tok:
                        ci += 1
                    if ci < len(cand):
                        hits += 1
                        ci += 1
                coverage = hits / max(1, len(s_norms))
                len_penalty = abs(len(cand) - expected_len) * 0.06
                offset_penalty = (st - cursor) * 0.08
                score = coverage - len_penalty - offset_penalty
                if score > best_score:
                    best_score = score
                    best_span = (st, en)

        start_i, end_i = best_span
        raw_spans.append((start_i, end_i))
        cursor = min(total_words - 1, end_i + 1)

    scene_mapping: list[dict] = []
    updated_scenes: list[dict] = []
    n_scenes = len(scenes)
    boundaries = [0.0]

    for i in range(n_scenes - 1):
        _, end_i = raw_spans[i]
        next_start_i, _ = raw_spans[i + 1]
        t_end = words[min(end_i, total_words - 1)]["end"]
        t_next = words[min(next_start_i, total_words - 1)]["start"]
        cut = (t_end + t_next) / 2.0 if t_next >= t_end else t_end
        if pauses:
            cut = snap_cut_to_pause(cut, pauses)
        cut = max(boundaries[-1] + 0.4, min(voice_duration - 0.4 * (n_scenes - 1 - i), cut))
        boundaries.append(round(cut, 3))
    boundaries.append(round(voice_duration, 3))

    for i, scene in enumerate(scenes):
        st_i, en_i = raw_spans[i]
        aligned_start = boundaries[i]
        aligned_end = boundaries[i + 1]
        updated = dict(scene)
        if "planned_start" not in updated and "start" in updated:
            updated["planned_start"] = updated["start"]
        if "planned_end" not in updated and "end" in updated:
            updated["planned_end"] = updated["end"]
        updated["start"] = aligned_start
        updated["end"] = aligned_end
        updated_scenes.append(updated)

        scene_mapping.append({
            "scene_id": str(scene.get("id") or f"scene_{i + 1:02d}"),
            "planned_start": updated.get("planned_start"),
            "planned_end": updated.get("planned_end"),
            "aligned_start": aligned_start,
            "aligned_end": aligned_end,
            "duration": round(aligned_end - aligned_start, 3),
            "word_start_idx": st_i,
            "word_end_idx": en_i,
            "narration": str(scene.get("narration") or ""),
            "matched_words": " ".join(w["word"] for w in words[st_i: en_i + 1]),
        })

    return scene_mapping, updated_scenes


def attach_scene_ids_to_phrases(phrases: list[dict], scene_mapping: list[dict]) -> list[dict]:
    for phrase in phrases:
        mid = (phrase["start"] + phrase["end"]) / 2.0
        owner = scene_mapping[-1]["scene_id"]
        for sm in scene_mapping:
            if sm["aligned_start"] <= mid <= sm["aligned_end"] + 1e-6:
                owner = sm["scene_id"]
                break
        phrase["scene_id"] = owner
    return phrases


def write_phrase_srt(phrases: list[dict], path: Path) -> None:
    def fmt(seconds: float) -> str:
        ms = max(0, round(seconds * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for idx, p in enumerate(phrases, 1):
        blocks.append(f"{idx}\n{fmt(p['start'])} --> {fmt(p['end'])}\n{p['text']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def align_project(
    plan_path: Path,
    voice_path: Path,
    out_alignment: Path | None = None,
    out_srt: Path | None = None,
    provider: str = "auto",
    update_plan: bool = True,
) -> dict:
    plan_path = plan_path.resolve()
    project_root = plan_path.parent
    load_env(project_root)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    transcript = str(plan.get("narration") or "").strip()
    if not transcript:
        transcript = " ".join(str(s.get("narration", "")).strip() for s in plan.get("scenes", [])).strip()
    if not transcript:
        raise ValueError("Story plan has no narration text to align.")

    language = str(plan.get("language") or "hi")
    voice_duration = probe_audio_duration(voice_path)
    voice_hash = sha256_file(voice_path)

    words: list[dict] = []
    meta: dict = {}
    is_fallback = False
    warnings: list[str] = []
    method = ""

    raw_sidecar = voice_path.parent / "voice_alignment_raw.json"
    if provider in {"auto", "elevenlabs_tts"} and raw_sidecar.is_file():
        try:
            raw_data = json.loads(raw_sidecar.read_text(encoding="utf-8"))
            if not raw_data.get("voice_sha256") or raw_data.get("voice_sha256") == voice_hash:
                if raw_data.get("words"):
                    words = raw_data["words"]
                elif raw_data.get("alignment"):
                    words = characters_to_words(raw_data["alignment"])
                if words:
                    method = "elevenlabs_tts_timestamps"
                    meta = {"provider": "elevenlabs_tts", "source_file": str(raw_sidecar)}
        except Exception as exc:
            warnings.append(f"Could not read native TTS timestamp sidecar: {exc}")

    api_key = os.getenv(KEY_ENV, "").strip()
    if not words and provider in {"auto", "elevenlabs_forced"} and api_key:
        try:
            words, meta = align_with_elevenlabs_forced(voice_path, transcript, api_key)
            method = "elevenlabs_forced_alignment"
        except Exception as exc:
            warnings.append(f"ElevenLabs forced alignment unavailable: {exc}")
            if provider == "elevenlabs_forced":
                raise

    if not words and provider in {"auto", "whisperx", "faster_whisper"}:
        try:
            prefer = "auto" if provider == "auto" else provider
            words, meta = align_with_local_whisper(voice_path, transcript, language=language, prefer=prefer)
            method = meta["provider"]
        except Exception as exc:
            if provider in {"whisperx", "faster_whisper"}:
                raise
            warnings.append(f"Local forced aligner unavailable ({exc}); using explicit VAD speech-envelope fallback.")

    if not words:
        words, meta = align_with_vad_fallback(voice_path, transcript, voice_duration)
        method = "vad_speech_envelope_fallback"
        is_fallback = True
        warnings.append(
            "EXPLICIT FALLBACK: Exact word-level forced alignment was unavailable; "
            "word timings were estimated from detected speech-activity intervals and Hindi syllable weights. "
            "Configure ELEVENLABS_API_KEY (TTS timestamps or /v1/forced-alignment) or install faster-whisper/whisperx for exact word alignment."
        )

    words = ensure_monotonic_words(words, voice_duration)
    pauses = detect_vocal_pauses_ffmpeg(voice_path)
    phrases = group_words_into_phrases(words)
    scene_mapping, updated_scenes = map_scenes_to_alignment(plan, words, voice_duration, pauses=pauses)
    phrases = attach_scene_ids_to_phrases(phrases, scene_mapping)
    word_events = extract_word_events(words, scene_mapping)
    # Attach word_events onto each owning scene so animate_scenes.py can execute word-locked punch-ins
    for sc in updated_scenes:
        sid = str(sc.get('id'))
        sc['word_events'] = [ev for ev in word_events if ev['scene_id'] == sid]

    if update_plan:
        if "planned_duration_seconds" not in plan and "duration_seconds" in plan:
            plan["planned_duration_seconds"] = plan["duration_seconds"]
        plan["duration_seconds"] = voice_duration
        plan["scenes"] = updated_scenes
        plan["timeline_source"] = "narration_alignment"
        plan["alignment_method"] = method
        plan["alignment_is_fallback"] = is_fallback
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    alignment_doc = {
        "version": "10.0",
        "transcript": transcript,
        "language": language,
        "voice_file": str(voice_path),
        "voice_sha256": voice_hash,
        "actual_voice_duration": voice_duration,
        "provider": meta.get("provider", method),
        "alignment_method": method,
        "is_fallback": is_fallback,
        "synchronized": not is_fallback,
        "warnings": warnings,
        "word_count": len(words),
        "phrase_count": len(phrases),
        "words": words,
        "phrases": phrases,
        "scene_mapping": scene_mapping,
        "pauses_detected": [{"start": s, "end": e} for s, e in pauses],
        "word_events": word_events,
        "metadata": meta,
    }

    alignment_path = out_alignment or (project_root / "audio" / "audio_alignment.json")
    alignment_path.parent.mkdir(parents=True, exist_ok=True)
    alignment_path.write_text(json.dumps(alignment_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    srt_path = out_srt or (project_root / "captions.srt")
    write_phrase_srt(phrases, srt_path)

    return alignment_doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="Path to story_plan.json")
    parser.add_argument("--voice", required=True, help="Approved narration audio file")
    parser.add_argument("--out", default=None, help="Output audio_alignment.json path")
    parser.add_argument("--srt", default=None, help="Output phrase-timed captions.srt path")
    parser.add_argument(
        "--provider",
        choices=("auto", "elevenlabs_tts", "elevenlabs_forced", "whisperx", "faster_whisper", "vad_fallback"),
        default=os.getenv("CINESTORY_ALIGNER_PROVIDER", "auto"),
    )
    parser.add_argument("--no-update-plan", action="store_true", help="Do not overwrite scene timings in story_plan.json")
    args = parser.parse_args()

    plan_path = Path(args.plan).expanduser().resolve()
    voice_path = Path(args.voice).expanduser()
    if not voice_path.is_absolute():
        voice_path = (plan_path.parent / voice_path).resolve()
    if not voice_path.is_file():
        print(f"ERROR: narration audio not found: {voice_path}", file=sys.stderr)
        return 2

    out_align = Path(args.out).expanduser() if args.out else None
    if out_align and not out_align.is_absolute():
        out_align = (plan_path.parent / out_align).resolve()
    out_srt = Path(args.srt).expanduser() if args.srt else None
    if out_srt and not out_srt.is_absolute():
        out_srt = (plan_path.parent / out_srt).resolve()

    try:
        doc = align_project(plan_path, voice_path, out_align, out_srt, args.provider, not args.no_update_plan)
    except Exception as exc:
        print(f"ALIGNMENT FAILED: {exc}", file=sys.stderr)
        return 1

    status = "FALLBACK (approximate)" if doc["is_fallback"] else "SYNCHRONIZED"
    print(
        f"ALIGNMENT {status}: {doc['word_count']} words -> {doc['phrase_count']} subtitle cards -> "
        f"{len(doc['scene_mapping'])} scenes | master duration={doc['actual_voice_duration']:.2f}s | method={doc['alignment_method']}"
    )
    for w in doc["warnings"]:
        print(f"ALIGNMENT WARNING: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
