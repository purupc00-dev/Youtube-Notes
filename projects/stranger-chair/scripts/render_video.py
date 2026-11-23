#!/usr/bin/env python3
"""Assemble variable-duration scene clips, narration, lush ducked music, rich SFX/ambience,
word-by-word Subtitle Cards, and subtle persistent channel watermark."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path


def require(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"{name} is required and was not found on PATH.")
    return exe


def run(cmd: list[str]) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), check=True)


def esc_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")


def resolve_file(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def build_subtitle_filter(subtitle_path: Path, fonts_dir: Path | None = None) -> str:
    options = f":fontsdir='{esc_filter_path(fonts_dir)}'" if fonts_dir and fonts_dir.is_dir() else ""
    return f"subtitles='{esc_filter_path(subtitle_path)}'{options}"


def render_watermark_png(text: str, out_path: Path, width: int, height: int, opacity: float = 0.35, y_ratio: float = 0.905) -> Path:
    """Render a subtle translucent embossed channel watermark PNG (below subtitles)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    opacity = max(0.05, min(0.85, float(opacity)))
    font_size = max(18, int(round(width * 0.044)))
    font_candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansDevanagari-Bold.ttf",
    ]
    font_path = next((p for p in font_candidates if p.is_file()), None)
    font = ImageFont.truetype(str(font_path), font_size) if font_path else ImageFont.load_default()

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    sdraw = ImageDraw.Draw(shadow)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (width - tw) // 2
    ty = int(height * y_ratio) - th // 2

    # Soft dark drop shadow + warm translucent light-embossed text so it reads cleanly on dark & bright frames
    shadow_alpha = int(round(255 * min(0.65, opacity * 1.15)))
    fill_alpha = int(round(255 * opacity))
    stroke_alpha = int(round(255 * min(0.55, opacity * 0.85)))

    sdraw.text((tx + 2, ty + 2), text, font=font, fill=(10, 8, 6, shadow_alpha))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=3))
    draw.text(
        (tx, ty), text, font=font,
        fill=(242, 236, 224, fill_alpha),
        stroke_width=1,
        stroke_fill=(20, 16, 12, stroke_alpha),
    )
    final = Image.alpha_composite(shadow, canvas)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(out_path, optimize=True)
    return out_path


def bounded_sfx_duration(event: dict, scene_times: dict[str, tuple[float, float]], timeline: float) -> float:
    scene_id = str(event.get("scene", ""))
    if scene_id not in scene_times:
        raise RuntimeError(f"SFX event references unknown scene {scene_id!r}.")
    start = float(event.get("start", 0.0))
    requested = float(event.get("duration", 1.8))
    scene_end = float(scene_times[scene_id][1])
    available = min(scene_end, float(timeline)) - start
    if not math.isfinite(start) or not math.isfinite(requested) or requested <= 0 or available <= 0:
        raise RuntimeError(f"SFX event for {scene_id} has invalid timing.")
    return min(requested, available)


def probe(path: Path, ffprobe: str) -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
         "-show_entries", "format=duration,size", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def validate_inputs(
    data: dict, plan_path: Path, images: Path, clips: Path, voice_value: str | None,
    music_value: str | None, sfx_map_value: str | None, ass_value: str | None,
    srt_value: str | None, allow_no_voice: bool, allow_still_fallback: bool,
    allow_no_music: bool = False, allow_no_sfx: bool = False,
    allow_no_subtitles: bool = False, subtitle_overlay_value: str | None = None,
) -> tuple[Path | None, Path | None, dict, list[tuple[dict, Path]], Path | None]:
    project = plan_path.parent
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("Story plan must contain at least one scene.")
    for scene in scenes:
        scene_id = str(scene.get("id", "")).strip()
        if not scene_id:
            raise RuntimeError("Every scene must have a non-empty id.")
        clip = resolve_file(clips / (scene.get("video_file") or f"{scene_id}.mp4"), project)
        image = resolve_file(images / (scene.get("image_file") or f"{scene_id}.png"), project)
        if not clip.is_file() and not (allow_still_fallback and image.is_file()):
            raise RuntimeError(f"Missing required motion clip for {scene_id}: {clip}. Run animate_scenes.py first.")

    voice_path = resolve_file(voice_value, project) if voice_value else None
    if voice_path and not voice_path.is_file():
        raise RuntimeError(f"Narration file does not exist: {voice_path}")
    if not voice_path and not allow_no_voice:
        raise RuntimeError("Narration is required. Supply --voice, or explicitly pass --allow-no-voice for captions-only production.")

    manifest_path = resolve_file(sfx_map_value, project) if sfx_map_value else None
    manifest: dict = {}
    if not manifest_path:
        if not (allow_no_music and allow_no_sfx):
            raise RuntimeError("An audio manifest is required so music and timed SFX can be checked.")
    else:
        if not manifest_path.is_file():
            raise RuntimeError(f"Audio manifest does not exist: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("sfx_events", []), list):
            raise RuntimeError("Audio manifest is malformed: sfx_events must be an array.")
    music_value = music_value or manifest.get("music")
    music_path = resolve_file(music_value, manifest_path.parent if manifest_path else project) if music_value else None
    if music_path and not music_path.is_file():
        raise RuntimeError(f"Music file listed by the audio manifest does not exist: {music_path}")
    if not music_path and not allow_no_music:
        raise RuntimeError("A music track is required. Generate AI music or use the bundled fallback library.")

    sfx_inputs: list[tuple[dict, Path]] = []
    timeline = float(data.get("duration_seconds", 0))
    scene_times = {str(scene["id"]): (float(scene["start"]), float(scene["end"])) for scene in scenes}
    seen_sfx_scenes: set[str] = set()
    for index, event in enumerate(manifest.get("sfx_events", []), 1):
        if not isinstance(event, dict) or not event.get("file"):
            raise RuntimeError(f"Audio manifest SFX event {index} is missing its file path.")
        start = float(event.get("start", -1))
        duration = float(event.get("duration", 0))
        volume = float(event.get("volume", 0.24))
        scene_id = str(event.get("scene", ""))
        if not (math.isfinite(start) and math.isfinite(duration) and math.isfinite(volume)) or start < 0 or duration <= 0 or start >= timeline or not 0 <= volume <= 1:
            raise RuntimeError(f"Audio manifest SFX event {index} has invalid timing or volume.")
        if scene_id not in scene_times:
            raise RuntimeError(f"Audio manifest SFX event {index} references unknown scene {scene_id!r}.")
        scene_start, scene_end = scene_times[scene_id]
        if not (scene_start <= start < scene_end):
            raise RuntimeError(f"Audio manifest SFX event {index} starts outside its scene {scene_id}.")
        seen_sfx_scenes.add(scene_id)
        sfx_path = resolve_file(event["file"], manifest_path.parent if manifest_path else project)
        if not sfx_path.is_file():
            raise RuntimeError(f"SFX file listed by the audio manifest does not exist: {sfx_path}")
        sfx_inputs.append((event, sfx_path))

    if not sfx_inputs and not allow_no_sfx:
        raise RuntimeError("At least one timed SFX event is required.")
    if sfx_inputs and not allow_no_sfx:
        missing_sfx_scenes = sorted(set(scene_times) - seen_sfx_scenes)
        if missing_sfx_scenes:
            raise RuntimeError("Every scene must have at least one timed SFX/ambience cue. Missing: " + ", ".join(missing_sfx_scenes))

    subtitle_path = None
    if subtitle_overlay_value:
        subtitle_path = resolve_file(subtitle_overlay_value, project)
        if not subtitle_path.is_file():
            raise RuntimeError(f"Subtitle card overlay video does not exist: {subtitle_path}")
    elif ass_value:
        subtitle_path = resolve_file(ass_value, project)
        if not subtitle_path.is_file():
            raise RuntimeError(f"ASS subtitle file does not exist: {subtitle_path}")
    elif srt_value:
        srt_path = resolve_file(srt_value, project)
        if not srt_path.is_file():
            raise RuntimeError(f"SRT subtitle file does not exist: {srt_path}")
        subtitle_path = srt_path
    if not subtitle_path and not allow_no_subtitles:
        raise RuntimeError("Subtitles are required. Supply a subtitle card overlay, styled ASS, or SRT file.")
    return voice_path, music_path, manifest, sfx_inputs, subtitle_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--images", default="assets/images")
    parser.add_argument("--clips", default="assets/video")
    parser.add_argument("--voice")
    parser.add_argument("--music")
    parser.add_argument("--sfx-map", help="JSON manifest produced by generate_audio_assets.py")
    parser.add_argument("--srt", help="Existing SRT subtitle file.")
    parser.add_argument("--ass", help="Pre-styled ASS subtitle file.")
    parser.add_argument("--subtitle-overlay", help="Animated RGBA CineStory Subtitle Cards overlay video.")
    parser.add_argument("--watermark", default="Inspired Tales AI", help="Persistent subtle bottom watermark text (empty to disable).")
    parser.add_argument("--watermark-opacity", type=float, default=0.35, help="Watermark opacity between 0.10 and 0.65 (default 0.35).")
    parser.add_argument("--no-watermark", action="store_true", help="Disable channel watermark overlay.")
    parser.add_argument("--out", default="renders/final.mp4")
    parser.add_argument("--allow-still-fallback", action="store_true")
    parser.add_argument("--allow-no-voice", action="store_true")
    parser.add_argument("--allow-no-music", action="store_true")
    parser.add_argument("--allow-no-sfx", action="store_true")
    parser.add_argument("--allow-no-subtitles", action="store_true")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--fps", type=int)
    parser.add_argument("--crf", type=int, default=19)
    parser.add_argument("--keep-scenes", action="store_true")
    args = parser.parse_args()

    try:
        if args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2:
            raise RuntimeError("Width and height must be positive even numbers for H.264 yuv420p output.")
        ffmpeg, ffprobe = require("ffmpeg"), require("ffprobe")
        plan_path = Path(args.plan).expanduser().resolve()
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        fps = args.fps or int(data.get("fps", 24))
        if not 12 <= fps <= 60:
            raise RuntimeError("FPS must be between 12 and 60.")
        W, H = args.width, args.height
        project = plan_path.parent
        images = resolve_file(args.images, project)
        clips = resolve_file(args.clips, project)

        voice_path, music_path, manifest, sfx_inputs, subtitle_path = validate_inputs(
            data, plan_path, images, clips, args.voice, args.music, args.sfx_map, args.ass, args.srt,
            args.allow_no_voice, args.allow_still_fallback, args.allow_no_music, args.allow_no_sfx,
            args.allow_no_subtitles, subtitle_overlay_value=args.subtitle_overlay,
        )

        out = resolve_file(args.out, project)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.parent / "_cinestory_segments"
        tmp.mkdir(parents=True, exist_ok=True)

        segments = []
        for i, scene in enumerate(data["scenes"]):
            duration = float(scene["end"]) - float(scene["start"])
            clip = resolve_file(clips / (scene.get("video_file") or f"{scene['id']}.mp4"), project)
            image = resolve_file(images / (scene.get("image_file") or f"{scene['id']}.png"), project)
            segment = tmp / f"{i:03d}.mp4"
            if clip.is_file():
                vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1,format=yuv420p"
                run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-t", f"{duration:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf), "-r", str(fps), str(segment)])
            elif image.is_file() and args.allow_still_fallback:
                frames = max(1, round(duration * fps))
                vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},zoompan=z='min(zoom+0.00045,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={fps},setsar=1,format=yuv420p"
                run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(image), "-t", f"{duration:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf), "-r", str(fps), str(segment)])
            else:
                raise RuntimeError(f"Missing required scene clip: {clip}")
            segments.append(segment)

        concat = tmp / "concat.txt"
        concat.write_text("".join("file '" + str(segment.resolve()).replace("'", "'\\''") + "'\n" for segment in segments), encoding="utf-8")
        base = tmp / "base.mp4"
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-r", str(fps), "-an", str(base)])

        watermark_png = None
        wm_text = "" if args.no_watermark else str(args.watermark or "").strip()
        if wm_text:
            watermark_png = render_watermark_png(wm_text, tmp / "watermark.png", W, H, opacity=args.watermark_opacity)

        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(base)]
        next_idx = 1
        overlay_input_idx = None
        wm_input_idx = None
        if args.subtitle_overlay and subtitle_path:
            cmd += ["-i", str(subtitle_path)]
            overlay_input_idx = next_idx
            next_idx += 1
        if watermark_png and watermark_png.is_file():
            cmd += ["-i", str(watermark_png)]
            wm_input_idx = next_idx
            next_idx += 1

        inputs = []
        if voice_path:
            cmd += ["-i", str(voice_path)]
            inputs.append(("voice", next_idx, None))
            next_idx += 1
        if music_path:
            cmd += ["-stream_loop", "-1", "-i", str(music_path)]
            inputs.append(("music", next_idx, None))
            next_idx += 1
        for event, sfx_path in sfx_inputs:
            # Loop ambience/SFX input seamlessly if cue duration exceeds clip length
            cmd += ["-stream_loop", "-1", "-i", str(sfx_path)]
            inputs.append(("sfx", next_idx, event))
            next_idx += 1
        if not inputs:
            raise RuntimeError("No audio tracks were supplied.")

        filters: list[str] = []
        voice_label = None
        sidechain_label = None
        music_label = None
        sfx_labels: list[str] = []
        timeline = float(data.get("duration_seconds", sum(float(s["end"]) - float(s["start"]) for s in data["scenes"])))

        # Video graph: base -> Subtitle Cards (or ASS) -> Persistent Watermark
        cur_v = "0:v"
        if overlay_input_idx is not None:
            filters.append(f"[{cur_v}][{overlay_input_idx}:v]overlay=0:0:eof_action=pass[vsub]")
            cur_v = "vsub"
        elif subtitle_path:
            bundled_fonts = Path(__file__).resolve().parents[1] / "assets" / "fonts"
            sub_f = build_subtitle_filter(subtitle_path, bundled_fonts)
            filters.append(f"[{cur_v}]{sub_f}[vsub]")
            cur_v = "vsub"
        if wm_input_idx is not None:
            filters.append(f"[{cur_v}][{wm_input_idx}:v]overlay=0:0,format=yuv420p[vout]")
            video_map = "[vout]"
        elif cur_v != "0:v":
            filters.append(f"[{cur_v}]format=yuv420p[vout]")
            video_map = "[vout]"
        else:
            video_map = "0:v"

        # Audio graph: calibrated to One-More-Light-Hindi's lush ~11 dB voice-to-music blend + audible scene ambience/SFX
        scene_times = {str(scene["id"]): (float(scene["start"]), float(scene["end"])) for scene in data["scenes"]}
        for index, (kind, input_id, event) in enumerate(inputs):
            label = f"aud{index}"
            if kind == "voice":
                voice_label = label + "mix"
                if any(item[0] == "music" for item in inputs):
                    sidechain_label = label + "sc"
                    filters.append(
                        f"[{input_id}:a]aresample=48000,highpass=f=65,lowpass=f=15500,"
                        f"loudnorm=I=-16:TP=-1.5:LRA=10:linear=true,aresample=48000,"
                        f"aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[{voice_label}][{sidechain_label}]"
                    )
                else:
                    filters.append(
                        f"[{input_id}:a]aresample=48000,highpass=f=65,lowpass=f=15500,"
                        f"loudnorm=I=-16:TP=-1.5:LRA=10:linear=true,aresample=48000,"
                        f"aformat=sample_rates=48000:channel_layouts=stereo[{voice_label}]"
                    )
            elif kind == "music":
                music_label = label
                fade_in = min(1.0, timeline * 0.12)
                fade_out = min(2.2, timeline * 0.25)
                fade_start = max(0.0, timeline - fade_out)
                # Normalize music first to a consistent -21 LUFS bed so fallback & AI tracks feel equally rich and present
                filters.append(
                    f"[{input_id}:a]atrim=0:{timeline:.3f},aresample=48000,highpass=f=40,lowpass=f=15000,"
                    f"loudnorm=I=-22:TP=-2.0:LRA=11:linear=true,volume=0.64,"
                    f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={fade_start:.3f}:d={fade_out:.3f},"
                    f"aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[{label}]"
                )
            else:
                delay = max(0, int(float(event.get("start", 0)) * 1000))
                volume = max(0.01, min(0.11, float(event.get("volume", 0.08))))
                sfx_duration = bounded_sfx_duration(event, scene_times, timeline)
                fade_in = min(0.12, sfx_duration / 4.0)
                fade_out_duration = min(0.28, sfx_duration / 4.0)
                fade_out = max(0.0, sfx_duration - fade_out_duration)
                filters.append(
                    f"[{input_id}:a]aresample=48000,atrim=0:{sfx_duration:.3f},highpass=f=120,lowpass=f=1900,"
                    f"volume={volume:.3f},afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={fade_out:.3f}:d={fade_out_duration:.3f},"
                    f"adelay={delay}|{delay},aformat=sample_rates=48000:channel_layouts=stereo[{label}]"
                )
                sfx_labels.append(label)

        if music_label and sidechain_label:
            # Gentle cinematic ducking: keeps music emotionally felt underneath narration and lifts warmly during pauses
            filters.append(f"[{music_label}][{sidechain_label}]sidechaincompress=threshold=0.04:ratio=3.0:attack=45:release=600:makeup=1.08[duckedmusic]")
            music_label = "duckedmusic"

        mix = ([f"[{voice_label}]"] if voice_label else []) + ([f"[{music_label}]"] if music_label else []) + [f"[{label}]" for label in sfx_labels]
        filters.append(
            "".join(mix) + f"amix=inputs={len(mix)}:duration=longest:dropout_transition=2:normalize=0,"
            f"alimiter=limit=0.94,atrim=0:{timeline:.3f},aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[aout]"
        )

        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", video_map, "-map", "[aout]",
            "-t", f"{timeline:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(out),
        ]
        run(cmd)
        if not out.is_file() or out.stat().st_size < 1024:
            raise RuntimeError(f"Final MP4 was not created correctly: {out}")
        media = probe(out, ffprobe)
        streams = media.get("streams", [])
        v = next((x for x in streams if x.get("codec_type") == "video"), {})
        au = next((x for x in streams if x.get("codec_type") == "audio"), {})
        if v.get("codec_name") != "h264" or (int(v.get("width", 0)), int(v.get("height", 0))) != (W, H):
            raise RuntimeError("Final MP4 failed H.264/resolution validation.")
        if int(au.get("sample_rate", 0)) != 48000 or int(au.get("channels", 0)) != 2:
            raise RuntimeError("Final MP4 failed stereo AAC 48 kHz audio validation.")
        print("OUTPUT CHECK:", json.dumps(media, indent=2))
        print(f"RENDERED: {out}")
        if not args.keep_scenes:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"RENDER FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
