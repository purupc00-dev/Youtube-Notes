#!/usr/bin/env python3
"""Audition Hindi/selected-language voices and synthesize the approved narration.

Uses the ElevenLabs Voice List API and:
- Eleven v4 / v3: Text-to-Dialogue API (`/v1/text-to-dialogue` or `/v1/text-to-dialogue/with-timestamps`).
- Multilingual v2: Text-to-Speech API (`/v1/text-to-speech/{voice_id}` or `/v1/text-to-speech/{voice_id}/with-timestamps`).

In v10, final synthesis defaults to requesting official character-level timestamps so
`align_narration.py` can lock every scene cut and 2-3 word subtitle card to the exact
spoken narration without guessing. Every billable synthesis request is sent exactly once.
Secrets are loaded from ELEVENLABS_API_KEY or a private .env file.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ROOT = "https://api.elevenlabs.io/v1"
VOICES_ROOT = "https://api.elevenlabs.io/v2"
KEY_ENV = "ELEVENLABS_API_KEY"
DEFAULT_MODEL = "eleven_v4"


def load_env(project_root: Path) -> None:
    candidates = (project_root / ".env", Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name and name not in os.environ:
                os.environ[name] = value


def configured_model(cli_model: str | None = None) -> str:
    """Resolve and validate the TTS model from CLI override or private env."""
    model = (cli_model or os.getenv("ELEVENLABS_TTS_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    supported = {"eleven_v4", "eleven_v3", "eleven_multilingual_v2"}
    if model not in supported:
        raise ValueError("supported ELEVENLABS_TTS_MODEL values are eleven_v4, eleven_v3, or eleven_multilingual_v2")
    return model


def request_json(url: str, key: str, method: str = "GET", body: dict | None = None, timeout: int = 90) -> dict:
    headers = {"xi-api-key": key, "Accept": "application/json"}
    raw_body = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1600).decode("utf-8", "replace")
        raise RuntimeError(f"ElevenLabs HTTP {exc.code}: {detail}") from exc


def request_audio(url: str, key: str, body: dict, timeout: int = 180) -> bytes:
    """One billable TTS call. Never replay after a timeout/uncertain outcome."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
        if not data:
            raise RuntimeError("ElevenLabs returned an empty audio response.")
        return data
    except urllib.error.HTTPError as exc:
        detail = exc.read(1600).decode("utf-8", "replace")
        raise RuntimeError(f"ElevenLabs TTS HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"TTS request failed and was not retried to avoid duplicate billing: {exc}") from exc


def request_audio_with_timestamps(url: str, key: str, body: dict, timeout: int = 180) -> dict:
    """One billable TTS-with-timestamps call returning JSON (`audio_base64`, `alignment`). Never retried."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("audio_base64"):
            raise RuntimeError("ElevenLabs with-timestamps response is missing audio_base64.")
        return payload
    except urllib.error.HTTPError as exc:
        detail = exc.read(1600).decode("utf-8", "replace")
        raise RuntimeError(f"ElevenLabs TTS-with-timestamps HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"TTS-with-timestamps request failed and was not retried to avoid duplicate billing: {exc}") from exc


def characters_to_words(alignment: dict | None) -> list[dict]:
    """Convert ElevenLabs character-level alignment arrays into Devanagari-safe word timestamps."""
    if not isinstance(alignment, dict):
        return []
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
                "confidence": 0.96,
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


def list_voices(key: str, language: str, page_size: int = 100) -> list[dict]:
    params = urllib.parse.urlencode({"page_size": min(max(page_size, 1), 100), "language": language, "use_cases": "narrative_story"})
    payload = request_json(f"{VOICES_ROOT}/voices?{params}", key)
    voices = payload.get("voices", [])
    if not isinstance(voices, list):
        raise RuntimeError("ElevenLabs voice list response did not contain a voices array.")
    return [voice for voice in voices if isinstance(voice, dict) and voice.get("voice_id")]


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("._")
    return value[:48] or "voice"


def load_plan_text(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    text = str(data.get("narration", "")).strip()
    if not text:
        scenes = data.get("scenes", [])
        text = " ".join(str(scene.get("narration", "")).strip() for scene in scenes if isinstance(scene, dict)).strip()
    if not text:
        raise ValueError("No narration found in story_plan.json.")
    return text


def make_preview_text(text: str, max_chars: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    candidate = text[:max_chars]
    match = re.search(r"^(.{60,}?)[.!?।॥](?:\s|$)", candidate)
    if match:
        return match.group(0).strip()
    return candidate.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def synthesize(text: str, voice_id: str, key: str, output: Path, model: str = DEFAULT_MODEL,
               speed: float = 0.96, stability: float = 0.46, style: float = 0.28,
               language: str = "hi", with_timestamps: bool = False) -> dict:
    if not text.strip():
        raise ValueError("Narration text must not be empty.")
    if not voice_id.strip():
        raise ValueError("A selected ElevenLabs voice_id is required.")
    if not (0.7 <= speed <= 1.2 and 0 <= stability <= 1 and 0 <= style <= 1):
        raise ValueError("Voice settings are out of supported range.")
    if len(text) > 2000 and model in {"eleven_v4", "eleven_v3"}:
        raise ValueError("Eleven v4/v3 Text-to-Dialogue accepts up to 2,000 total input characters per request. Use eleven_multilingual_v2 for longer narration or split it into continuity-aware segments.")

    zero_retention = os.getenv("ELEVENLABS_ZERO_RETENTION", "0").strip().lower() in {"1", "true", "yes", "on"}
    if model in {"eleven_v4", "eleven_v3"}:
        query_params = {"output_format": "mp3_44100_128"}
        body = {
            "inputs": [{"text": text, "voice_id": voice_id}],
            "model_id": model,
            "language_code": language,
        }
        endpoint = f"{API_ROOT}/text-to-dialogue/with-timestamps" if with_timestamps else f"{API_ROOT}/text-to-dialogue"
    else:
        query_params = {"output_format": "mp3_44100_128"}
        body = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": 0.78,
                "style": style,
                "use_speaker_boost": True,
                "speed": speed,
            },
        }
        base_tts = f"{API_ROOT}/text-to-speech/{urllib.parse.quote(voice_id, safe='')}"
        endpoint = f"{base_tts}/with-timestamps" if with_timestamps else base_tts

    if zero_retention:
        query_params["enable_logging"] = "false"
    url = endpoint + "?" + urllib.parse.urlencode(query_params)

    alignment_file: Path | None = None
    words: list[dict] = []
    if with_timestamps:
        payload = request_audio_with_timestamps(url, key, body)
        audio = base64.b64decode(payload["audio_base64"])
        raw_alignment = payload.get("alignment") or payload.get("normalized_alignment")
        words = characters_to_words(raw_alignment)
    else:
        audio = request_audio(url, key, body)
        raw_alignment = None

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_bytes(audio)
    temp.replace(output)
    voice_sha256 = hashlib.sha256(audio).hexdigest()

    if with_timestamps and (raw_alignment or words):
        alignment_file = output.parent / "voice_alignment_raw.json"
        alignment_file.write_text(
            json.dumps(
                {
                    "provider": "elevenlabs_tts_timestamps",
                    "model_id": model,
                    "voice_id": voice_id,
                    "voice_file": str(output),
                    "voice_sha256": voice_sha256,
                    "transcript": text,
                    "words": words,
                    "alignment": raw_alignment,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    return {
        "voice_id": voice_id,
        "model_id": model,
        "endpoint": endpoint.split("/v1/", 1)[-1] if with_timestamps else endpoint.rsplit("/", 1)[-1],
        "output": str(output),
        "bytes": len(audio),
        "voice_sha256": voice_sha256,
        "zero_retention_requested": zero_retention,
        "timestamps_requested": with_timestamps,
        "alignment_words_count": len(words),
        "alignment_file": str(alignment_file) if alignment_file else None,
        "provider": "elevenlabs",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="List available voices for a language.")
    listing.add_argument("--project", default=".")
    listing.add_argument("--language", default="hi", help="Language code, e.g. hi or en.")
    listing.add_argument("--out", default="audio/voice-catalog.json")
    audition = sub.add_parser("audition", help="Generate up to three voice auditions for the approved script.")
    audition.add_argument("plan")
    audition.add_argument("--out", default="audio/auditions")
    audition.add_argument("--language", default="hi")
    audition.add_argument("--voice-ids", nargs="+", help="Optional explicit voice IDs. Maximum three.")
    audition.add_argument("--count", type=int, default=3)
    audition.add_argument("--model", default=None, help="Optional override; otherwise ELEVENLABS_TTS_MODEL or the package default is used.")
    final = sub.add_parser("synthesize", help="Generate the final approved narration once with word timestamps.")
    final.add_argument("plan")
    final.add_argument("--voice-id", required=True)
    final.add_argument("--out", default="audio/voice-final.mp3")
    final.add_argument("--model", default=None, help="Optional override; otherwise ELEVENLABS_TTS_MODEL or the package default is used.")
    final.add_argument("--speed", type=float, default=0.96)
    final.add_argument("--stability", type=float, default=0.46)
    final.add_argument("--style", type=float, default=0.28)
    final.add_argument("--no-timestamps", action="store_true", help="Disable official ElevenLabs /with-timestamps alignment request.")
    args = parser.parse_args()

    project = Path(getattr(args, "plan", getattr(args, "project", "."))).resolve()
    project_root = project.parent if project.is_file() else project
    load_env(project_root)
    key = os.getenv(KEY_ENV, "").strip()
    if args.command in {"audition", "synthesize"}:
        try:
            args.model = configured_model(args.model)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    if not key:
        print(f"ERROR: configure {KEY_ENV} in the private runtime secret store or project .env.", file=sys.stderr)
        return 2

    try:
        if args.command == "list":
            voices = list_voices(key, args.language)
            output = Path(args.out)
            output = output if output.is_absolute() else project_root / output
            output.parent.mkdir(parents=True, exist_ok=True)
            safe = [{"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category"),
                     "labels": v.get("labels", {}), "description": v.get("description", ""),
                     "preview_url": v.get("preview_url")} for v in voices]
            output.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"VOICES: {len(safe)} candidate(s) for language={args.language}; saved {output}")
            return 0

        if args.command == "audition":
            if args.count < 1 or args.count > 3:
                parser.error("--count must be 1 to 3")
            plan_path = Path(args.plan).resolve()
            full_text = load_plan_text(plan_path)
            preview_text = make_preview_text(full_text)
            if args.voice_ids:
                if len(args.voice_ids) > 3:
                    parser.error("--voice-ids accepts at most three IDs")
                ids = args.voice_ids
                catalog = []
            else:
                catalog = list_voices(key, args.language)
                ids = [str(v["voice_id"]) for v in catalog[:args.count]]
            if not ids:
                raise RuntimeError(f"No voices were returned for language={args.language!r}. Run `list --language {args.language}` or provide --voice-ids.")
            out = Path(args.out); out = out if out.is_absolute() else plan_path.parent / out
            out.mkdir(parents=True, exist_ok=True)
            generated = []
            for index, voice_id in enumerate(ids, 1):
                record = next((v for v in catalog if v.get("voice_id") == voice_id), {})
                name = slug(str(record.get("name") or voice_id))
                dest = out / f"audition_{index:02d}_{name}.mp3"
                try:
                    info = synthesize(preview_text, voice_id, key, dest, args.model, language=args.language, with_timestamps=False)
                    info.update({"name": record.get("name", voice_id), "preview_text": preview_text})
                    generated.append(info)
                    print(f"AUDITION {index}: {record.get('name', voice_id)} -> {dest}")
                except Exception as exc:
                    print(f"AUDITION FAILED for {voice_id}: {exc}", file=sys.stderr)
            manifest = out / "voice-auditions.json"
            manifest.write_text(json.dumps({"language": args.language, "model": args.model, "auditions": generated}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if not generated:
                raise RuntimeError("All requested voice auditions failed; no billable requests will be retried automatically.")
            print(f"VOICE AUDITIONS READY: {manifest} ({len(generated)} successful audition(s))")
            return 0

        plan_path = Path(args.plan).resolve()
        text = load_plan_text(plan_path)
        out = Path(args.out); out = out if out.is_absolute() else plan_path.parent / out
        result = synthesize(
            text, args.voice_id, key, out, args.model, args.speed, args.stability, args.style,
            os.getenv("ELEVENLABS_TTS_LANGUAGE", "hi"),
            with_timestamps=not args.no_timestamps,
        )
        manifest = out.parent / "voice_manifest.json"
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"VOICE READY: {out} (timestamps={result['alignment_words_count']} words)")
        return 0
    except Exception as exc:
        print(f"VOICE GENERATION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
