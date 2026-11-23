#!/usr/bin/env python3
"""Run every automatic post-storyboard production stage on the unified narration timeline and write strict QA."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


def find_voice(project: Path, supplied: str | None) -> Path:
    if supplied:
        path = Path(supplied).expanduser()
        path = path if path.is_absolute() else project / path
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"Voice file not found: {path}")
    audio = project / "audio"
    preferred = [audio / f"voice{ext}" for ext in (".wav", ".mp3", ".m4a")] + [audio / f"voice-original{ext}" for ext in (".wav", ".mp3")]
    for path in preferred:
        if path.is_file():
            return path.resolve()
    for pattern in ("voice-final.*", "voice-original.*", "narration.*", "voice.*"):
        found = sorted(x for x in audio.glob(pattern) if x.suffix.lower() in AUDIO_EXTS and x.is_file())
        if found:
            return found[0].resolve()
    raise FileNotFoundError("No approved narration master found. Generate it with voice_generation.py or attach the user's recording.")


def has_valid_srt_events(path: Path) -> bool:
    """Return true only when an SRT has at least one timed, non-empty cue."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return False
    if not text:
        return False
    import re
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), None)
        if not time_line:
            continue
        try:
            start, end = [part.strip().split()[0] for part in time_line.split("-->", 1)]
            timestamp = r"\d{2,}:\d{2}:\d{2}[,.]\d{1,3}"
            if re.fullmatch(timestamp, start) and re.fullmatch(timestamp, end):
                index = lines.index(time_line)
                if any(lines[index + 1:]):
                    return True
        except (ValueError, IndexError):
            continue
    return False


def create_scene_srt(plan: dict, path: Path) -> None:
    """Fallback captions (captions-only mode without voice) split into short 2-3 word chunks."""
    def ts(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        secs, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

    blocks = []
    cue_num = 1
    for scene in plan.get("scenes", []):
        text = str(scene.get("narration") or scene.get("caption") or "").strip()
        if not text:
            continue
        words = text.split()
        chunks = [words[i: i + 3] for i in range(0, len(words), 3)]
        if len(chunks) >= 2 and len(chunks[-1]) == 1 and len(chunks[-2]) >= 3:
            chunks[-1].insert(0, chunks[-2].pop())
        s_start, s_end = float(scene["start"]), float(scene["end"])
        span = max(0.3, s_end - s_start)
        step = span / max(1, len(chunks))
        for idx, ch in enumerate(chunks):
            c0 = s_start + idx * step
            c1 = min(s_end, c0 + step)
            blocks.append(f"{cue_num}\n{ts(c0)} --> {ts(c1)}\n{' '.join(ch)}")
            cue_num += 1
    if not blocks:
        raise RuntimeError("No scene captions/narration are available for caption generation.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def probe(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for final QA.")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
         "-show_entries", "format=duration,size", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def make_audio_command(plan_path: Path, audio_dir: Path, music_style: str, provider: str, max_sfx: int | None = None) -> list[str]:
    """Build the audio stage command without overriding dynamic per-scene SFX coverage."""
    command = [
        sys.executable, str(ROOT / "scripts" / "generate_audio_assets.py"), str(plan_path),
        "--out", str(audio_dir), "--music-style", music_style, "--provider", provider,
    ]
    if max_sfx is not None:
        command.extend(["--max-sfx", str(max_sfx)])
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="Approved CineStory project folder")
    parser.add_argument("--voice", help="Approved voice file; auto-detected from PROJECT/audio")
    parser.add_argument("--captions-only", action="store_true", help="Explicitly render without narration, using scene-timed captions")
    parser.add_argument("--captions", help="Optional SRT path; defaults to PROJECT/captions.srt")
    parser.add_argument("--aligner-provider", default="auto",
                        choices=("auto", "elevenlabs_tts", "elevenlabs_forced", "whisperx", "faster_whisper", "vad_fallback"),
                        help="Narration word-alignment provider.")
    parser.add_argument("--out", default="renders/CineStory-Final.mp4")
    parser.add_argument("--style", default="warm_cinematic_3d", choices=("warm_cinematic_3d", "moody_cinematic_3d", "golden_fairytale_3d", "soft_storybook_3d"))
    parser.add_argument(
        "--music-style", default="warm_acoustic_storytelling",
        choices=("warm_emotional", "hopeful", "moody", "light_cinematic_motivation", "warm_acoustic_storytelling", "emotional_uplifting_score"),
    )
    parser.add_argument("--audio-provider", default="auto", choices=("auto", "elevenlabs", "library"))
    parser.add_argument("--max-sfx", type=int, default=None, help="Maximum timed SFX cues; default automatically covers every scene.")
    parser.add_argument("--parallax", choices=("on", "auto", "off"), default="auto",
                        help="Quality-verified 2.5D parallax. Default 'auto' safely rejects damaged inpainting layers.")
    parser.add_argument("--subtitle-mode", choices=("cards", "ass"), default="cards",
                        help="Render v10 animated Devanagari-safe Subtitle Cards (default) or legacy ASS.")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--watermark", default="Inspired Tales AI", help="Persistent subtle bottom watermark text (empty to disable).")
    parser.add_argument("--watermark-opacity", type=float, default=0.35)
    parser.add_argument("--no-watermark", action="store_true")
    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    plan_path = project / "story_plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"Missing approved story plan: {plan_path}")
    if not 12 <= args.fps <= 60 or args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2:
        raise ValueError("Output must use 12–60 fps and positive even width/height values.")

    images = project / "assets" / "images"
    video = project / "assets" / "video"
    audio = project / "audio"
    review = project / "review"
    renders = project / "renders"
    for directory in (video, audio, review, renders):
        directory.mkdir(parents=True, exist_ok=True)

    voice = None if args.captions_only else find_voice(project, args.voice)
    captions = Path(args.captions).expanduser() if args.captions else project / "captions.srt"
    if not captions.is_absolute():
        captions = project / captions
    alignment_path = audio / "audio_alignment.json"

    # STAGE 1: Master Narration Timeline Alignment (voice drives scene cuts & subtitle timing!)
    if voice:
        run([
            sys.executable, str(ROOT / "scripts" / "align_narration.py"),
            str(plan_path), "--voice", str(voice),
            "--out", str(alignment_path), "--srt", str(captions),
            "--provider", args.aligner_provider,
        ], ROOT)
    else:
        plan_tmp = json.loads(plan_path.read_text(encoding="utf-8"))
        if not has_valid_srt_events(captions):
            print("Captions-only mode: creating short 2-3 word scene captions.")
            create_scene_srt(plan_tmp, captions)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    run([sys.executable, str(ROOT / "scripts" / "validate_plan.py"), str(plan_path)], ROOT)

    # STAGE 2: Cinematic Motion & Layer Quality Gate (before any billable audio API calls)
    run([
        sys.executable, str(ROOT / "scripts" / "animate_scenes.py"), str(plan_path),
        "--images", str(images), "--out", str(video), "--layers", str(project / "assets" / "layers"),
        "--style", args.style, "--intensity", "cinematic", "--parallax", args.parallax, "--motion-provider", args.motion_provider, "--veo-model", args.veo_model,
        "--width", str(args.width), "--height", str(args.height), "--fps", str(args.fps),
    ], ROOT)

    motion_path = video / "motion_manifest.json"
    if not motion_path.is_file():
        raise RuntimeError("Motion stage did not write motion_manifest.json; audio generation was not started.")
    motion_preflight = json.loads(motion_path.read_text(encoding="utf-8"))
    if not isinstance(motion_preflight, list) or len(motion_preflight) != len(plan.get("scenes", [])):
        raise RuntimeError("Motion manifest is incomplete; audio generation was not started.")
    for scene, item in zip(plan["scenes"], motion_preflight):
        expected_id = str(scene["id"])
        clip = Path(item.get("output", ""))
        if str(item.get("scene")) != expected_id or not clip.is_file() or clip.stat().st_size < 1024:
            raise RuntimeError(f"Motion preflight failed for scene {expected_id}; audio generation was not started.")

    # STAGE 3: CineStory Subtitle Cards (Devanagari-safe RGBA overlay) + ASS companion
    ass = captions.with_suffix(".ass")
    overlay_mov = review / "captions_overlay.mov"
    sub_cmd = [
        sys.executable, str(ROOT / "scripts" / "generate_styled_subtitles.py"),
        str(captions), "--out", str(ass),
        "--width", str(args.width), "--height", str(args.height), "--fps", str(args.fps),
        "--duration", str(plan["duration_seconds"]),
    ]
    if args.subtitle_mode == "cards":
        sub_cmd.extend(["--cards-out", str(overlay_mov), "--cards-dir", str(review / "subtitle_cards")])
        if alignment_path.is_file():
            sub_cmd.extend(["--alignment", str(alignment_path)])
    run(sub_cmd, ROOT)

    run([
        sys.executable, str(ROOT / "scripts" / "make_motion_review.py"),
        "--clips", str(video), "--out", str(review / "motion-preview.mp4"),
        "--width", "540", "--height", "960", "--fps", "12",
    ], ROOT)

    # STAGE 4: Music & Timed SFX (distinct musical identities + honest provenance)
    run(make_audio_command(plan_path, audio, args.music_style, args.audio_provider, args.max_sfx), ROOT)

    # STAGE 5: Unified Final Render
    output = Path(args.out).expanduser()
    output = output if output.is_absolute() else project / output
    render_cmd = [
        sys.executable, str(ROOT / "scripts" / "render_video.py"), str(plan_path),
        "--images", str(images), "--clips", str(video),
        "--sfx-map", str(audio / "audio_manifest.json"),
        "--width", str(args.width), "--height", str(args.height), "--fps", str(args.fps),
        "--out", str(output),
    ]
    if args.subtitle_mode == "cards" and overlay_mov.is_file():
        render_cmd.extend(["--subtitle-overlay", str(overlay_mov)])
    else:
        render_cmd.extend(["--ass", str(ass)])
    if args.no_watermark:
        render_cmd.append("--no-watermark")
    elif args.watermark:
        render_cmd.extend(["--watermark", str(args.watermark), "--watermark-opacity", str(args.watermark_opacity)])
    if voice:
        render_cmd.extend(["--voice", str(voice)])
    else:
        render_cmd.append("--allow-no-voice")
    run(render_cmd, ROOT)

    # STAGE 6: Real Quality Assurance (`sync_report.json` + `qa-contact-sheet.png` + `production-report.json`)
    qa_script = ROOT / "scripts" / "verify_qa.py"
    if qa_script.is_file():
        run([
            sys.executable, str(qa_script), str(project),
            "--video", str(output),
            "--out", str(review / "sync_report.json"),
            "--contact-sheet", str(review / "qa-contact-sheet.png"),
        ], ROOT)

    media = probe(output)
    streams = media.get("streams", [])
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), {})
    motion = json.loads(motion_path.read_text(encoding="utf-8"))
    audio_manifest = json.loads((audio / "audio_manifest.json").read_text(encoding="utf-8"))
    sync_report = json.loads((review / "sync_report.json").read_text(encoding="utf-8")) if (review / "sync_report.json").is_file() else {}

    issues = list(sync_report.get("issues", []))
    warnings = list(sync_report.get("warnings", []))
    parallax_count = sum(1 for item in motion if item.get("foreground_background_layers"))
    report = {
        "version": "10.0",
        "output": str(output),
        "duration_seconds": float(media.get("format", {}).get("duration", 0)),
        "expected_duration_seconds": float(plan["duration_seconds"]),
        "timeline_source": plan.get("timeline_source", "planned"),
        "alignment_method": plan.get("alignment_method"),
        "alignment_is_fallback": plan.get("alignment_is_fallback"),
        "video": video_stream,
        "audio": audio_stream,
        "scene_count": len(plan.get("scenes", [])),
        "motion_clips": len(motion),
        "parallax_scenes": parallax_count,
        "music_source": audio_manifest.get("music_source"),
        "music_provider": audio_manifest.get("music_provider"),
        "music_style": audio_manifest.get("music_style"),
        "sfx_count": len(audio_manifest.get("sfx_events", [])),
        "subtitle_mode": args.subtitle_mode,
        "sync_report": str(review / "sync_report.json"),
        "contact_sheet": str(review / "qa-contact-sheet.png"),
        "qa": "passed" if not issues and not warnings else ("passed_with_warnings" if not issues else "failed"),
        "warnings": warnings,
        "issues": issues,
    }
    (review / "production-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if issues:
        return 3
    print(f"CINESTORY v10 COMPLETE: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"PRODUCTION FAILED at command: {exc.cmd}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
