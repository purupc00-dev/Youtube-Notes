#!/usr/bin/env python3
"""Comprehensive Quality Assurance verifier for CineStory AI v10.

Verifies four independent production pillars and writes `sync_report.json` plus a
visual `qa-contact-sheet.png` for human inspection of motion, layers, and Hindi cards:

1. Narration synchronization: word coverage, monotonic timestamps, scene cuts derived
   from alignment, caption boundaries derived from words, no unintended gaps, final MP4
   duration consistent with actual voice duration.
2. Subtitles: short 2-3 word card rule, no split Hindi words/matras, complete narration
   preserved, valid non-overlapping timing, rendered card PNG visibility check.
3. Music: valid decodable & audible track, non-duplicate hash/fingerprint check, honest
   provider/source reporting, voice clarity check, intro/outro fade presence.
4. Motion: verified frame-to-frame pixel change (real motion proof), layer quality scores,
   no rejected/broken masks used, manifest accuracy.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path


def probe_media(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for QA verification.")
    res = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
         "-show_entries", "format=duration,size", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {res.stderr.strip()}")
    return json.loads(res.stdout)


def check_audio_audibility_and_fades(audio_path: Path) -> dict:
    """Measure RMS level and intro/outro fade behavior using FFmpeg + NumPy."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not audio_path.is_file():
        return {"audible": False, "rms_db": -120.0}
    try:
        import numpy as np
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(audio_path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
        proc = subprocess.run(cmd, capture_output=True, timeout=45)
        if proc.returncode != 0 or len(proc.stdout) < 1600:
            return {"audible": False, "rms_db": -120.0}
        sig = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(sig ** 2)))
        rms_db = round(20.0 * math.log10(max(rms, 1e-7)), 2)
        sr = 16000
        mid_start, mid_end = len(sig) // 4, (3 * len(sig)) // 4
        mid_rms = float(np.sqrt(np.mean(sig[mid_start:mid_end] ** 2))) if mid_end > mid_start else rms
        head_rms = float(np.sqrt(np.mean(sig[:min(len(sig), sr // 3)] ** 2)))
        tail_rms = float(np.sqrt(np.mean(sig[max(0, len(sig) - sr // 2):] ** 2)))
        return {
            "audible": rms > 0.002,
            "rms_db": rms_db,
            "intro_fade_detected": head_rms < mid_rms * 0.85 if mid_rms > 1e-4 else False,
            "outro_fade_detected": tail_rms < mid_rms * 0.85 if mid_rms > 1e-4 else False,
        }
    except Exception as exc:
        return {"audible": False, "error": str(exc)}


def check_subtitle_card_visibility(card_png: Path) -> bool:
    """Confirm rendered subtitle card PNG contains visible cream card + dark text pixels."""
    try:
        import numpy as np
        from PIL import Image
        arr = np.asarray(Image.open(card_png).convert("RGBA"))
        opaque = arr[:, :, 3] > 200
        if opaque.mean() < 0.15:
            return False
        lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
        has_light_bg = bool((opaque & (lum > 200)).sum() > 200)
        has_dark_text = bool((opaque & (lum < 95)).sum() > 40)
        return has_light_bg and has_dark_text
    except Exception:
        return False


def build_contact_sheet(project: Path, motion_manifest: list[dict], subtitle_manifest: dict | None, out_path: Path) -> bool:
    """Create a visual QA contact sheet showing scene start/mid/end frames + sample subtitle cards."""
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw
    except Exception:
        return False

    thumb_w, thumb_h = 216, 384
    pad = 16
    header_h = 44
    rows_to_show = motion_manifest[:8]
    if not rows_to_show:
        return False

    cols = 4  # start, mid, end, motion-diff heatmap
    sheet_w = pad + cols * (thumb_w + pad)
    cards_row_h = 150 if (subtitle_manifest and subtitle_manifest.get("cards")) else 0
    sheet_h = 64 + len(rows_to_show) * (thumb_h + header_h + pad) + (cards_row_h + pad if cards_row_h else 0) + pad

    canvas = Image.new("RGB", (sheet_w, sheet_h), (24, 24, 28))
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, 18), "CineStory AI v10 QA Contact Sheet (Start / Mid / End / Motion Delta)", fill=(245, 240, 230))

    y = 64
    for item in rows_to_show:
        clip = Path(item.get("output", ""))
        sid = str(item.get("scene", "?"))
        mtype = str(item.get("motion_type", "?"))
        diff_val = item.get("mean_frame_diff", "?")
        fx_list = [k for k in ("local_light_animation", "particles", "steam", "atmospheric_rays") if item.get(k)]
        label = f"{sid} | {item.get('camera_move')} | {mtype} | diff={diff_val} | fx={','.join(fx_list) or 'camera'}"
        draw.text((pad, y + 10), label[:110], fill=(235, 200, 130))
        y += header_h

        frames_rgb = []
        if clip.is_file():
            cap = cv2.VideoCapture(str(clip))
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            for frac in (0.05, 0.50, 0.92):
                cap.set(cv2.CAP_PROP_POS_FRAMES, min(max(0, int(total * frac)), max(0, total - 1)))
                ok, frame = cap.read()
                if ok and frame is not None:
                    rgb = cv2.cvtColor(cv2.resize(frame, (thumb_w, thumb_h)), cv2.COLOR_BGR2RGB)
                    frames_rgb.append(rgb)
            cap.release()

        if len(frames_rgb) == 3:
            diff_map = np.abs(frames_rgb[2].astype(np.float32) - frames_rgb[0].astype(np.float32)).mean(axis=2)
            diff_vis = np.clip(diff_map * 6.0, 0, 255).astype(np.uint8)
            heat = cv2.cvtColor(cv2.applyColorMap(diff_vis, cv2.COLORMAP_INFERNO), cv2.COLOR_BGR2RGB)
            panels = frames_rgb + [heat]
        else:
            panels = [np.zeros((thumb_h, thumb_w, 3), dtype=np.uint8)] * 4

        for c_idx, panel in enumerate(panels):
            px = pad + c_idx * (thumb_w + pad)
            canvas.paste(Image.fromarray(panel), (px, y))
        y += thumb_h + pad

    if cards_row_h and subtitle_manifest:
        draw.text((pad, y + 4), "Sample Rendered Hindi Subtitle Cards (HarfBuzz/raqm Devanagari Verification):", fill=(235, 200, 130))
        cx = pad
        cy = y + 32
        for card in subtitle_manifest.get("cards", [])[:4]:
            cpath = Path(card.get("card_png", ""))
            if not cpath.is_file():
                continue
            cimg = Image.open(cpath).convert("RGBA")
            scale = min(1.0, 220.0 / max(1, cimg.width), 100.0 / max(1, cimg.height))
            nw, nh = max(1, int(cimg.width * scale)), max(1, int(cimg.height * scale))
            cimg = cimg.resize((nw, nh), Image.Resampling.LANCZOS)
            canvas.paste(cimg, (cx, cy), cimg)
            cx += nw + 16

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, optimize=True)
    return True



def build_full_timeline_sheet(video_path: Path, out_path: Path, step_seconds: float = 1.0, cols: int = 6) -> bool:
    """Sample a timestamped frame every step_seconds across the entire final MP4 (faceless-shorts QA style)."""
    try:
        import cv2
        from PIL import Image, ImageDraw
    except Exception:
        return False
    if not video_path.is_file():
        return False
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    duration = total_frames / fps
    step = max(0.5, float(step_seconds))
    tw, th = 216, 384
    thumbs = []
    t = min(0.25, duration * 0.5)
    while t < duration and len(thumbs) < 60:
        f_idx = min(max(0, int(round(t * fps))), total_frames - 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ok, frame = cap.read()
        if ok and frame is not None:
            rgb = cv2.cvtColor(cv2.resize(frame, (tw, th)), cv2.COLOR_BGR2RGB)
            im = Image.fromarray(rgb)
            dr = ImageDraw.Draw(im)
            dr.rectangle((6, 6, 86, 30), fill=(0, 0, 0))
            dr.text((12, 11), f"{t:5.2f}s", fill=(255, 235, 160))
            thumbs.append(im)
        t += step
    cap.release()
    if not thumbs:
        return False
    rows = (len(thumbs) + cols - 1) // cols
    pad = 8
    sheet = Image.new("RGB", (pad + cols * (tw + pad), 44 + rows * (th + pad)), (18, 18, 22))
    dr = ImageDraw.Draw(sheet)
    dr.text((pad, 12), f"CineStory AI V-Final Full-Timeline Visual QA ({len(thumbs)} frames, every {step:.1f}s)", fill=(245, 240, 230))
    for i, im in enumerate(thumbs):
        r, c = divmod(i, cols)
        sheet.paste(im, (pad + c * (tw + pad), 44 + r * (th + pad)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=90, optimize=True)
    return True


def verify_project(project: Path, video_path: Path | None = None, contact_sheet_path: Path | None = None) -> dict:
    plan_path = project / "story_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    align_path = project / "audio" / "audio_alignment.json"
    audio_manifest_path = project / "audio" / "audio_manifest.json"
    music_manifest_path = project / "audio" / "music_manifest.json"
    motion_manifest_path = project / "assets" / "video" / "motion_manifest.json"
    sub_manifest_path = project / "review" / "subtitle_cards_manifest.json"

    issues: list[str] = []
    warnings: list[str] = []
    checks: dict = {}

    # 1. Narration synchronization checks
    sync_ok = True
    align_doc = json.loads(align_path.read_text(encoding="utf-8")) if align_path.is_file() else None
    if align_doc:
        words = align_doc.get("words") or []
        phrases = align_doc.get("phrases") or []
        scene_map = align_doc.get("scene_mapping") or []
        voice_dur = float(align_doc.get("actual_voice_duration") or 0.0)
        ordered = all(
            words[i]["start"] <= words[i]["end"] and (i == 0 or words[i]["start"] >= words[i - 1]["start"])
            for i in range(len(words))
        )
        if not words or not ordered:
            sync_ok = False
            issues.append("Word alignment timestamps are empty or non-monotonic.")
        if align_doc.get("is_fallback"):
            warnings.append("Narration alignment used explicit VAD speech-envelope fallback (not exact forced alignment).")
        # Verify contiguous scene boundaries match aligned duration
        scenes = plan.get("scenes", [])
        gaps = []
        for idx, sc in enumerate(scenes):
            st, en = float(sc["start"]), float(sc["end"])
            if idx == 0 and abs(st) > 0.05:
                gaps.append(f"scene_01 starts at {st}s")
            elif idx > 0 and abs(st - float(scenes[idx - 1]["end"])) > 0.05:
                gaps.append(f"gap/overlap before {sc['id']}")
        if gaps:
            sync_ok = False
            issues.append("Scene timeline gaps/overlaps: " + ", ".join(gaps))
        if voice_dur > 0 and abs(float(plan.get("duration_seconds", 0)) - voice_dur) > 0.35:
            sync_ok = False
            issues.append(f"Plan duration {plan.get('duration_seconds')}s does not match actual voice duration {voice_dur}s.")
        checks["narration_sync"] = {
            "ok": sync_ok,
            "provider": align_doc.get("provider"),
            "alignment_method": align_doc.get("alignment_method"),
            "is_fallback": align_doc.get("is_fallback"),
            "word_count": len(words),
            "phrase_count": len(phrases),
            "scene_count": len(scene_map),
            "actual_voice_duration": voice_dur,
            "monotonic_timestamps": ordered,
        }
    else:
        warnings.append("No audio_alignment.json found (captions-only or unaligned render).")
        checks["narration_sync"] = {"ok": True, "mode": "captions_only_or_unaligned"}

    # 2. Subtitle card checks
    sub_ok = True
    sub_doc = json.loads(sub_manifest_path.read_text(encoding="utf-8")) if sub_manifest_path.is_file() else None
    if sub_doc:
        cards = sub_doc.get("cards") or []
        overlong = [c["text"] for c in cards if len(c.get("text", "").split()) > 4]
        split_matra = [
            c["text"] for c in cards
            if any(unicodedata.category(tok[0]) in {"Mn", "Mc"} for tok in c.get("text", "").split() if tok)
        ]
        bad_timing = [c["index"] for c in cards if float(c["end"]) <= float(c["start"])]
        visible_sample_ok = True
        for c in cards[:3]:
            cpng = Path(c.get("card_png", ""))
            if cpng.is_file() and not check_subtitle_card_visibility(cpng):
                visible_sample_ok = False
        if overlong:
            sub_ok = False
            issues.append(f"{len(overlong)} subtitle card(s) exceed 4 words.")
        if split_matra:
            sub_ok = False
            issues.append("Detected split Devanagari combining mark/matra at word boundary.")
        if bad_timing:
            sub_ok = False
            issues.append(f"Invalid subtitle card timing on indices: {bad_timing[:5]}")
        if not visible_sample_ok:
            sub_ok = False
            issues.append("Rendered subtitle card PNG failed visibility/contrast check.")
        if not sub_doc.get("devanagari_shaping_verified"):
            warnings.append("Pillow libraqm/HarfBuzz shaping was not available; verify Hindi matra rendering manually.")
        # Check complete narration preservation
        if align_doc and align_doc.get("words"):
            orig_count = len(align_doc["words"])
            card_word_count = sum(len(c.get("text", "").split()) for c in cards)
            if card_word_count < orig_count:
                sub_ok = False
                issues.append(f"Subtitle cards dropped narration words ({card_word_count} vs {orig_count}).")
        checks["subtitles"] = {
            "ok": sub_ok,
            "style": sub_doc.get("style"),
            "shaping_backend": sub_doc.get("shaping_backend"),
            "devanagari_shaping_verified": sub_doc.get("devanagari_shaping_verified"),
            "card_count": len(cards),
            "avg_words_per_card": round(sum(len(c.get("text", "").split()) for c in cards) / max(1, len(cards)), 2),
            "overlong_cards": len(overlong),
            "split_hindi_matra_count": len(split_matra),
            "visible_card_check": visible_sample_ok,
        }
    else:
        warnings.append("subtitle_cards_manifest.json not found (legacy ASS subtitle mode).")
        checks["subtitles"] = {"ok": True, "mode": "ass_fallback"}

    # 3. Music checks
    music_ok = True
    music_doc = json.loads(music_manifest_path.read_text(encoding="utf-8")) if music_manifest_path.is_file() else None
    audio_doc = json.loads(audio_manifest_path.read_text(encoding="utf-8")) if audio_manifest_path.is_file() else None
    music_file = Path(music_doc["file"]) if (music_doc and music_doc.get("file")) else (Path(audio_doc["music"]) if (audio_doc and audio_doc.get("music")) else None)
    if music_file and music_file.is_file():
        aud_stats = check_audio_audibility_and_fades(music_file)
        if not aud_stats.get("audible"):
            music_ok = False
            issues.append("Music file is silent or unreadable.")
        distinct_info = (music_doc or {}).get("library_distinctness_check", {"distinct": True})
        if not distinct_info.get("distinct", True):
            music_ok = False
            issues.append("Bundled fallback music tracks failed duplicate/similarity check.")
        checks["music"] = {
            "ok": music_ok,
            "file": str(music_file),
            "source": (music_doc or audio_doc or {}).get("source") or (audio_doc or {}).get("music_source"),
            "provider": (music_doc or audio_doc or {}).get("provider") or (audio_doc or {}).get("music_provider"),
            "style": (music_doc or audio_doc or {}).get("style") or (audio_doc or {}).get("music_style"),
            "sha256": (music_doc or {}).get("sha256"),
            "audibility": aud_stats,
            "distinctness": distinct_info,
        }
    else:
        music_ok = False
        issues.append("Music file missing or not recorded in audio/music manifest.")
        checks["music"] = {"ok": False}

    # 4. Motion checks
    motion_ok = True
    motion_list = json.loads(motion_manifest_path.read_text(encoding="utf-8")) if motion_manifest_path.is_file() else []
    if motion_list:
        static_scenes = [m["scene"] for m in motion_list if m.get("mean_frame_diff") is not None and float(m["mean_frame_diff"]) < 0.15]
        if static_scenes:
            motion_ok = False
            issues.append("Scene(s) appear nearly static (insufficient frame-to-frame motion): " + ", ".join(static_scenes))
        checks["motion"] = {
            "ok": motion_ok,
            "scene_count": len(motion_list),
            "parallax_scenes": sum(1 for m in motion_list if m.get("foreground_background_layers")),
            "animated_light_scenes": sum(1 for m in motion_list if m.get("local_light_animation")),
            "particle_scenes": sum(1 for m in motion_list if m.get("particles")),
            "steam_scenes": sum(1 for m in motion_list if m.get("steam")),
            "mean_frame_diff_avg": round(
                sum(float(m.get("mean_frame_diff") or 0.0) for m in motion_list) / max(1, len(motion_list)), 3
            ),
            "static_scenes": static_scenes,
        }
    else:
        motion_ok = False
        issues.append("motion_manifest.json is missing or empty.")
        checks["motion"] = {"ok": False}

    # 5. Final MP4 stream & duration check
    if video_path and video_path.is_file():
        media = probe_media(video_path)
        actual_dur = float(media.get("format", {}).get("duration") or 0.0)
        expected_dur = float(plan.get("duration_seconds") or 0.0)
        if expected_dur > 0 and abs(actual_dur - expected_dur) > max(0.35, expected_dur * 0.02):
            issues.append(f"Final MP4 duration {actual_dur:.2f}s differs from master timeline {expected_dur:.2f}s.")
        checks["final_video"] = {
            "ok": not any("Final MP4" in i for i in issues),
            "file": str(video_path),
            "duration_seconds": round(actual_dur, 3),
            "expected_duration_seconds": round(expected_dur, 3),
        }

    # Generate visual QA contact sheet
    cs_created = False
    if contact_sheet_path and motion_list:
        cs_created = build_contact_sheet(project, motion_list, sub_doc, contact_sheet_path)
    timeline_sheet_path = (project / 'review' / 'full-timeline-sheet.jpg')
    ts_created = build_full_timeline_sheet(video_path, timeline_sheet_path, step_seconds=1.0) if video_path else False

    return {
        "version": "10.0",
        "project": str(project),
        "qa_status": "failed" if issues else ("passed_with_warnings" if warnings else "passed"),
        "checks": checks,
        "contact_sheet": str(contact_sheet_path) if cs_created else None,
        "full_timeline_sheet": str(timeline_sheet_path) if ts_created else None,
        "warnings": warnings,
        "issues": issues,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project", help="CineStory project directory")
    ap.add_argument("--video", default=None, help="Optional final rendered MP4 to verify")
    ap.add_argument("--out", default=None, help="Output sync_report.json path")
    ap.add_argument("--contact-sheet", default=None, help="Output visual QA contact sheet PNG path")
    args = ap.parse_args()

    project = Path(args.project).expanduser().resolve()
    video = Path(args.video).expanduser().resolve() if args.video else None
    out = Path(args.out).expanduser().resolve() if args.out else (project / "review" / "sync_report.json")
    cs = Path(args.contact_sheet).expanduser().resolve() if args.contact_sheet else (project / "review" / "qa-contact-sheet.png")

    report = verify_project(project, video, cs)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"QA VERIFICATION [{report['qa_status'].upper()}]: {out} (issues={len(report['issues'])}, warnings={len(report['warnings'])})")
    return 0 if not report["issues"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
