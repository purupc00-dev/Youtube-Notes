#!/usr/bin/env python3
"""Generate CineStory AI v10 Final word-by-word fade-in Hindi Subtitle Cards and ASS fallback.

v10 Final Redesign:
- Renders real CineStory Subtitle Cards (warm off-white/cream rounded rectangle,
  dark charcoal Devanagari text, soft drop shadow, tasteful single-word amber/gold
  emphasis, centered in lower-third subtitle-safe area).
- Uses Pillow with libraqm (HarfBuzz + FriBidi) and bundled NotoSansDevanagari-Bold.ttf
  so Hindi matras (including `ि`), conjuncts, nukta, and chandrabindu shape accurately.
- WORD-BY-WORD CUMULATIVE SOFT FADE-IN:
  Instead of popping all 2-3 words onto screen at once, each word fades in smoothly
  in sequence (`word 1` -> `word 1 + word 2` -> `word 1 + word 2 + word 3`) at its
  aligned spoken timestamp, with a pure gentle fade-in (~140ms) and NO harsh pop-up
  or bounce animation.
- Also preserves full ASS subtitle export compatibility (`--out captions.ass`).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

HIGHLIGHT_WORDS = {
    "माँ", "मां", "पिता", "बाबा", "बेटा", "बेटी", "भूखा", "भूख", "अंधेरा", "अँधेरा", "रोशनी", "उम्मीद",
    "प्यार", "सपना", "सपने", "हिम्मत", "मदद", "दुआ", "किस्मत", "सफल", "सफलता", "घर", "मुस्कुराए",
    "मुस्कुराई", "मुस्कुराया", "अकेला", "अकेली", "आखिरी", "आख़िरी", "साथ", "जिंदगी", "ज़िंदगी",
    "खुशी", "खुशियां", "कबीर", "चायवाले", "चायवाला", "लालटेन", "किताब", "थकान", "गरीबी",
    "रोटी", "इंजीनियर", "पाठशाला", "मुफ़्त", "घड़ीसाज़", "घड़ी", "संतोष", "सोफिया", "समय",
    "गर्व", "हमेशा", "टिफ़िन", "नोट", "इंतज़ार",
}

# CineStory Subtitle Card Visual Design Tokens
CARD_BG_RGBA = (250, 246, 238, 244)       # Warm off-white / soft cream (#FAF6EE)
CARD_BORDER_RGBA = (228, 214, 190, 215)   # Subtle warm parchment border
CARD_SHADOW_RGBA = (15, 12, 8, 110)       # Soft warm charcoal shadow
TEXT_PRIMARY_RGB = (28, 25, 23)           # Dark charcoal / near-black (#1C1917)
TEXT_AMBER_RGB = (184, 102, 14)           # Tasteful high-contrast amber/gold (#B8660E)
CARD_CORNER_RADIUS = 24
CARD_PAD_X = 38
CARD_PAD_Y = 20
CARD_FADE_IN_MS = 120                     # Soft card fade-in (no harsh pop-up)
WORD_FADE_IN_MS = 150                     # Smooth word-by-word fade-in
EXIT_MS = 95                              # Gentle card fade-out


def ass_time(value: str) -> str:
    m = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", value.strip())
    if not m:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    h, mm, ss, ms = m.groups()
    cs = int(ms.ljust(3, "0")[:3]) // 10
    return f"{int(h)}:{mm}:{ss}.{cs:02d}"


def srt_time_to_seconds(value: str) -> float:
    m = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", value.strip())
    if not m:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    h, mm, ss, ms = m.groups()
    return int(h) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")[:3]) / 1000.0


def strip_edge_punctuation(token: str) -> str:
    start, end = 0, len(token)
    while start < end and unicodedata.category(token[start])[0] in {"P", "S"}:
        start += 1
    while end > start and unicodedata.category(token[end - 1])[0] in {"P", "S"}:
        end -= 1
    return token[start:end]


def escape_ass_text(token: str) -> str:
    return token.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def wrap_caption_text(text: str, max_chars: int | None = None) -> str:
    normalised = re.sub(r"\s+", " ", text.replace(r"\N", " ").replace("\r", " ")).strip()
    if not normalised:
        return ""
    if max_chars is None:
        max_chars = 30 if any("\u0900" <= char <= "\u097f" for char in normalised) else 42
    words = normalised.split(" ")
    lines: list[str] = [""]
    for word in words:
        current = lines[-1]
        candidate = f"{current} {word}".strip()
        if not current or len(candidate) <= max_chars:
            lines[-1] = candidate
            continue
        if len(lines) == 1:
            lines.append(word)
        else:
            lines[1] = f"{lines[1]} {word}".strip()
    return r"\N".join(line for line in lines if line)


def highlight(text: str) -> str:
    rendered_lines = []
    for line in text.split(r"\N"):
        tokens = re.findall(r"\S+", line)
        out = []
        for token in tokens:
            bare = strip_edge_punctuation(token)
            safe_token = escape_ass_text(token)
            if bare in HIGHLIGHT_WORDS:
                out.append(r"{\c&H0080BEFF&\b1}" + safe_token + r"{\c&H00FFFFFF&\b1}")
            else:
                out.append(safe_token)
        rendered_lines.append(" ".join(out))
    return r"\N".join(rendered_lines)


def resolve_font(requested: str, text: str) -> str:
    devanagari = any("\u0900" <= char <= "\u097f" for char in text)
    if not devanagari:
        return requested
    bundled_font = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansDevanagari-Bold.ttf"
    if bundled_font.is_file():
        return "Noto Sans Devanagari"
    bundled_regular = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansDevanagari-Regular.ttf"
    if bundled_regular.is_file():
        return "Noto Sans Devanagari"
    fc_match = shutil.which("fc-match")
    if not fc_match:
        raise RuntimeError("No bundled or system Devanagari font is available.")
    for candidate in [requested, "Noto Sans Devanagari", "Mukta", "Nirmala UI", "FreeSerif"]:
        result = subprocess.run([fc_match, "-f", "%{family}|%{lang}\n", candidate], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            family, _, language_tags = result.stdout.strip().partition("|")
            if "hi" in language_tags.split("|") or "hi" in language_tags.split():
                return family.split(",", 1)[0].strip()
    raise RuntimeError("No installed font with Hindi/Devanagari glyph coverage was found.")


def resolve_font_file(text: str) -> Path:
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    candidates = [
        fonts_dir / "NotoSansDevanagari-Bold.ttf",
        fonts_dir / "NotoSansDevanagari-Regular.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Could not find NotoSansDevanagari-Bold.ttf in assets/fonts or system fonts.")


def parse_srt(text: str):
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        time_line = next((x for x in lines if "-->" in x), None)
        if not time_line:
            continue
        start, end = [x.strip().split()[0] for x in time_line.split("-->", 1)]
        idx = lines.index(time_line)
        body = "\\N".join(lines[idx + 1:])
        if body:
            yield ass_time(start), ass_time(end), body


def parse_srt_seconds(text: str) -> list[dict]:
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[dict] = []
    for block in blocks:
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        time_line = next((x for x in lines if "-->" in x), None)
        if not time_line:
            continue
        start_s, end_s = [x.strip().split()[0] for x in time_line.split("-->", 1)]
        idx = lines.index(time_line)
        body = " ".join(lines[idx + 1:]).strip()
        if body:
            words = body.split()
            highlight_word = next((w for w in words if strip_edge_punctuation(w) in HIGHLIGHT_WORDS), None)
            cues.append({
                "index": len(cues) + 1,
                "text": unicodedata.normalize("NFC", body),
                "words": words,
                "start": round(srt_time_to_seconds(start_s), 3),
                "end": round(srt_time_to_seconds(end_s), 3),
                "highlight_word": highlight_word,
            })
    return cues


def split_cues_to_short_cards(cues: list[dict], max_words: int = 3) -> list[dict]:
    out: list[dict] = []
    for cue in cues:
        words = cue["text"].split()
        if len(words) <= max_words:
            out.append(cue)
            continue
        start, end = float(cue["start"]), float(cue["end"])
        total_dur = max(0.3, end - start)
        chunks = [words[i: i + max_words] for i in range(0, len(words), max_words)]
        if len(chunks) >= 2 and len(chunks[-1]) == 1 and len(chunks[-2]) >= 3:
            chunks[-1].insert(0, chunks[-2].pop())
        total_chars = sum(sum(len(w) for w in ch) for ch in chunks) or 1
        cursor = start
        for ch in chunks:
            share = sum(len(w) for w in ch) / total_chars
            dur = max(0.35, total_dur * share)
            c_end = min(end, cursor + dur)
            highlight_word = next((w for w in ch if strip_edge_punctuation(w) in HIGHLIGHT_WORDS), None)
            out.append({
                "index": len(out) + 1,
                "text": " ".join(ch),
                "words": ch,
                "start": round(cursor, 3),
                "end": round(max(cursor + 0.15, c_end), 3),
                "highlight_word": highlight_word,
            })
            cursor = c_end
    for i, item in enumerate(out, 1):
        item["index"] = i
    return out


def ensure_word_timings(cue: dict) -> list[dict]:
    """Return per-word start/end timings for cumulative word-by-word fade-in."""
    wt = cue.get("word_timings")
    words = cue.get("words") or cue["text"].split()
    if isinstance(wt, list) and len(wt) == len(words):
        return wt
    start = float(cue["start"])
    end = float(cue["end"])
    span = max(0.24, end - start)
    # Distribute word entry times across the first ~72% of the card so the complete phrase lingers legibly.
    reveal_window = span * 0.72
    weights = [max(1, len(strip_edge_punctuation(w))) for w in words]
    total_w = sum(weights) or 1
    out = []
    cur = start
    for idx, (w, wt_val) in enumerate(zip(words, weights)):
        dur = reveal_window * (wt_val / total_w)
        out.append({"word": w, "start": round(cur, 3), "end": round(min(end, cur + dur), 3)})
        cur += dur
    return out


def check_devanagari_shaping_support() -> tuple[bool, str]:
    try:
        from PIL import features
        if features.check("raqm"):
            return True, "pillow_raqm_harfbuzz"
        return False, "pillow_no_raqm"
    except Exception as exc:
        return False, f"pillow_unavailable: {exc}"


def render_subtitle_card_image(
    text: str,
    font_path: Path,
    font_size: int = 52,
    max_width: int = 900,
    highlight_word: str | None = None,
    word_alphas: list[float] | None = None,
    card_alpha: float = 1.0,
):
    """Render a crisp RGBA CineStory Subtitle Card supporting per-word soft fade-in."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    text = unicodedata.normalize("NFC", text).strip()
    words = text.split()
    if highlight_word is None:
        highlight_word = next((w for w in words if strip_edge_punctuation(w) in HIGHLIGHT_WORDS), None)
    if word_alphas is None:
        word_alphas = [1.0] * len(words)

    has_raqm, _ = check_devanagari_shaping_support()
    layout_engine = ImageFont.Layout.RAQM if has_raqm else ImageFont.Layout.BASIC

    chosen_size = font_size
    dummy = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    ddraw = ImageDraw.Draw(dummy)
    while chosen_size >= 26:
        font = ImageFont.truetype(str(font_path), chosen_size, layout_engine=layout_engine)
        bbox = ddraw.textbbox((0, 0), text, font=font)
        scale_k = max(0.5, min(1.2, chosen_size / 52.0))
        pad_x = max(18, int(round(CARD_PAD_X * scale_k)))
        if (bbox[2] - bbox[0]) + 2 * pad_x <= max_width:
            break
        chosen_size -= 2
    font = ImageFont.truetype(str(font_path), chosen_size, layout_engine=layout_engine)

    space_w = max(8, int(ddraw.textlength(" ", font=font)))
    token_metrics = []
    total_text_w = 0
    max_top = 0
    max_bottom = 0
    for idx, tok in enumerate(words):
        tb = ddraw.textbbox((0, 0), tok, font=font)
        tw = max(1, int(math.ceil(ddraw.textlength(tok, font=font))))
        token_metrics.append((tok, tw, tb))
        total_text_w += tw + (space_w if idx > 0 else 0)
        max_top = min(max_top, tb[1])
        max_bottom = max(max_bottom, tb[3])

    scale_k = max(0.5, min(1.2, chosen_size / 52.0))
    pad_x = max(18, int(round(CARD_PAD_X * scale_k)))
    pad_y = max(10, int(round(CARD_PAD_Y * scale_k)))
    radius = max(14, int(round(CARD_CORNER_RADIUS * scale_k)))

    text_h = max(chosen_size, max_bottom - max_top)
    card_w = total_text_w + 2 * pad_x
    card_h = text_h + 2 * pad_y

    shadow_pad = 22
    canvas_w = card_w + 2 * shadow_pad
    canvas_h = card_h + 2 * shadow_pad

    ca = max(0.0, min(1.0, float(card_alpha)))
    shadow_rgba = (CARD_SHADOW_RGBA[0], CARD_SHADOW_RGBA[1], CARD_SHADOW_RGBA[2], int(round(CARD_SHADOW_RGBA[3] * ca)))
    bg_rgba = (CARD_BG_RGBA[0], CARD_BG_RGBA[1], CARD_BG_RGBA[2], int(round(CARD_BG_RGBA[3] * ca)))
    border_rgba = (CARD_BORDER_RGBA[0], CARD_BORDER_RGBA[1], CARD_BORDER_RGBA[2], int(round(CARD_BORDER_RGBA[3] * ca)))

    # 1. Soft drop shadow
    shadow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_layer)
    sx0, sy0 = shadow_pad, shadow_pad + 7
    sdraw.rounded_rectangle((sx0, sy0, sx0 + card_w, sy0 + card_h), radius=radius, fill=shadow_rgba)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=10))

    # 2. Warm cream rounded card
    card_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(card_layer)
    cx0, cy0 = shadow_pad, shadow_pad
    cdraw.rounded_rectangle((cx0, cy0, cx0 + card_w, cy0 + card_h), radius=radius, fill=bg_rgba, outline=border_rgba, width=2)

    # 3. Draw each shaped Devanagari word at its own cumulative fade-in alpha (fixed positions so words never jump!)
    cursor_x = cx0 + pad_x
    text_y = cy0 + pad_y - max_top
    highlighted_used = False
    for idx, (tok, tw, _tb) in enumerate(token_metrics):
        if idx > 0:
            cursor_x += space_w
        w_alpha = max(0.0, min(1.0, float(word_alphas[idx] if idx < len(word_alphas) else 1.0))) * ca
        is_accent = (not highlighted_used) and (highlight_word is not None) and (tok == highlight_word)
        if is_accent:
            highlighted_used = True
        if w_alpha > 0.01:
            rgb = TEXT_AMBER_RGB if is_accent else TEXT_PRIMARY_RGB
            cdraw.text((cursor_x, text_y), tok, font=font, fill=rgb + (int(round(255 * w_alpha)),))
        cursor_x += tw

    final = Image.alpha_composite(shadow_layer, card_layer)
    return final, {"width": canvas_w, "height": canvas_h, "card_width": card_w, "card_height": card_h, "font_size": chosen_size}


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def render_subtitle_cards_and_overlay(
    cues: list[dict],
    duration: float,
    overlay_out: Path,
    cards_dir: Path,
    width: int = 1080,
    height: int = 1920,
    fps: int = 24,
    font_size: int = 52,
    center_y_ratio: float = 0.805,
) -> dict:
    """Render word-by-word soft fade-in Subtitle Cards into an RGBA overlay (`captions_overlay.mov`)."""
    from PIL import Image

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to encode the animated subtitle card overlay.")

    shaping_ok, shaping_backend = check_devanagari_shaping_support()
    font_path = resolve_font_file(" ".join(c["text"] for c in cues))
    cards_dir.mkdir(parents=True, exist_ok=True)
    overlay_out.parent.mkdir(parents=True, exist_ok=True)

    rendered_cards = []
    for cue in cues:
        wt = ensure_word_timings(cue)
        # Save full-phrase preview PNG for QA contact sheet
        card_img, metrics = render_subtitle_card_image(
            cue["text"],
            font_path=font_path,
            font_size=font_size,
            max_width=int(width * 0.84),
            highlight_word=cue.get("highlight_word"),
        )
        card_png = cards_dir / f"card_{cue['index']:03d}.png"
        card_img.save(card_png, optimize=True)
        rendered_cards.append({
            **cue,
            "word_timings": wt,
            "card_png": str(card_png),
            "metrics": metrics,
            "_full_img": card_img,
        })

    total_frames = max(1, int(round(duration * fps)))
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
        "-c:v", "qtrle", "-pix_fmt", "argb", str(overlay_out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None

    empty_frame = bytes(width * height * 4)
    card_in_s = CARD_FADE_IN_MS / 1000.0
    word_in_s = WORD_FADE_IN_MS / 1000.0
    exit_s = EXIT_MS / 1000.0
    anchor_cy = int(height * center_y_ratio)
    anchor_cx = width // 2

    cue_idx = 0
    n_cues = len(rendered_cards)
    try:
        for frame_i in range(total_frames):
            t = frame_i / float(fps)
            while cue_idx < n_cues and t > rendered_cards[cue_idx]["end"] + 1e-4:
                cue_idx += 1
            active = rendered_cards[cue_idx] if (cue_idx < n_cues and rendered_cards[cue_idx]["start"] <= t <= rendered_cards[cue_idx]["end"]) else None
            if active is None:
                proc.stdin.write(empty_frame)
                continue

            dt_in = t - active["start"]
            dt_out = active["end"] - t
            card_alpha = 1.0
            if dt_in < card_in_s:
                card_alpha = smoothstep(dt_in / card_in_s)
            elif dt_out < exit_s:
                card_alpha = smoothstep(dt_out / exit_s)

            # Compute per-word cumulative fade-in alpha (NO scale pop-up, pure smooth fade-in!)
            word_alphas = []
            all_words_full = True
            for w_item in active["word_timings"]:
                dt_w = t - float(w_item["start"])
                if dt_w <= 0.0:
                    wa = 0.0
                    all_words_full = False
                elif dt_w < word_in_s:
                    wa = smoothstep(dt_w / word_in_s)
                    all_words_full = False
                else:
                    wa = 1.0
                word_alphas.append(wa)

            if all_words_full and card_alpha >= 0.995:
                card_draw = active["_full_img"]
            else:
                card_draw, _ = render_subtitle_card_image(
                    active["text"],
                    font_path=font_path,
                    font_size=font_size,
                    max_width=int(width * 0.84),
                    highlight_word=active.get("highlight_word"),
                    word_alphas=word_alphas,
                    card_alpha=card_alpha,
                )

            frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            px = anchor_cx - card_draw.width // 2
            py = anchor_cy - card_draw.height // 2
            frame.alpha_composite(card_draw, dest=(max(0, px), max(0, py)))
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"FFmpeg subtitle card overlay encoding failed with exit code {rc}.")

    manifest_cards = [{k: v for k, v in item.items() if k != "_full_img"} for item in rendered_cards]
    manifest = {
        "version": "10.1",
        "style": "cinestory_subtitle_card_word_by_word_fade",
        "shaping_backend": shaping_backend,
        "devanagari_shaping_verified": shaping_ok,
        "font_file": str(font_path),
        "overlay_video": str(overlay_out),
        "cards_dir": str(cards_dir),
        "width": width,
        "height": height,
        "fps": fps,
        "duration_seconds": round(duration, 3),
        "animation": {
            "mode": "word_by_word_cumulative_fade_in",
            "card_fade_in_ms": CARD_FADE_IN_MS,
            "word_fade_in_ms": WORD_FADE_IN_MS,
            "exit_ms": EXIT_MS,
            "pop_up_scale": False,
        },
        "design": {
            "background_rgba": list(CARD_BG_RGBA),
            "text_rgb": list(TEXT_PRIMARY_RGB),
            "highlight_amber_rgb": list(TEXT_AMBER_RGB),
            "corner_radius": CARD_CORNER_RADIUS,
            "center_y_ratio": center_y_ratio,
        },
        "card_count": len(manifest_cards),
        "cards": manifest_cards,
    }
    manifest_path = overlay_out.with_name("subtitle_cards_manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def write_ass_file(events: list[tuple[str, str, str]], out: Path, font: str, font_size: int, margin_v: int) -> None:
    header = f"""[Script Info]
Title: CineStory Warm Cinematic Subtitles
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: CineStory,{font},{font_size},&H00FFFFFF,&H0080BEFF,&HCC100A06,&H99000000,-1,0,0,0,100,100,0,0,1,4,2,2,90,90,{margin_v},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    rows = []
    for start, end, text in events:
        wrapped = wrap_caption_text(text)
        lines = wrapped.split(r"\N") if wrapped else [""]
        longest = max((len(line) for line in lines), default=0)
        target = 30 if any("\u0900" <= char <= "\u097f" for char in text) else 42
        fitted_size = max(32, min(font_size, round(font_size * target / max(target, longest))))
        font_override = f"{{\\fs{fitted_size}}}" if fitted_size != font_size else ""
        safe = r"{\fad(100,130)}" + font_override + highlight(wrapped)
        rows.append(f"Dialogue: 0,{start},{end},CineStory,,0,0,0,,{safe}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + "\n".join(rows) + "\n", encoding="utf-8-sig")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("srt", help="Input SRT file (or phrase-aligned SRT from align_narration.py)")
    ap.add_argument("--out", default=None, help="Output ASS subtitle path")
    ap.add_argument("--alignment", default=None, help="Optional audio_alignment.json to drive 2-3 word cards directly")
    ap.add_argument("--cards-out", default=None, help="Output animated RGBA subtitle card overlay video (e.g. captions_overlay.mov)")
    ap.add_argument("--cards-dir", default=None, help="Directory for individual subtitle card preview PNGs")
    ap.add_argument("--duration", type=float, default=None, help="Master timeline duration in seconds for overlay video")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--font", default="Noto Sans Devanagari")
    ap.add_argument("--font-size", type=int, default=52)
    ap.add_argument("--margin-v", type=int, default=170)
    args = ap.parse_args()

    src = Path(args.srt)
    out = Path(args.out) if args.out else src.with_suffix(".ass")
    source_text = src.read_text(encoding="utf-8-sig")
    events = list(parse_srt(source_text))
    if not events:
        raise SystemExit("No valid subtitle events found in the SRT file.")
    resolved_font = resolve_font(args.font, source_text)
    write_ass_file(events, out, resolved_font, args.font_size, args.margin_v)
    print(f"STYLED SUBTITLES: {out} ({len(events)} cues; white bold, dark outline/shadow, selective warm-amber emphasis)")

    if args.cards_out:
        if args.alignment and Path(args.alignment).is_file():
            align_doc = json.loads(Path(args.alignment).read_text(encoding="utf-8"))
            cues = align_doc.get("phrases") or []
            duration = args.duration or float(align_doc.get("actual_voice_duration") or 0.0)
        else:
            cues = split_cues_to_short_cards(parse_srt_seconds(source_text), max_words=3)
            duration = args.duration or max((float(c["end"]) for c in cues), default=1.0)
        if not cues:
            raise SystemExit("No subtitle cues available for CineStory Subtitle Card rendering.")
        overlay_out = Path(args.cards_out)
        cards_dir = Path(args.cards_dir) if args.cards_dir else (overlay_out.parent / "subtitle_cards")
        scaled_font = max(26, int(round(max(46, args.font_size) * (args.width / 1080.0))))
        manifest = render_subtitle_cards_and_overlay(
            cues=cues,
            duration=duration,
            overlay_out=overlay_out,
            cards_dir=cards_dir,
            width=args.width,
            height=args.height,
            fps=args.fps,
            font_size=scaled_font,
        )
        print(
            f"SUBTITLE CARDS: {overlay_out} ({manifest['card_count']} word-by-word fade-in cards; "
            f"shaping={manifest['shaping_backend']}, word_fade={WORD_FADE_IN_MS}ms)"
        )


if __name__ == "__main__":
    main()
