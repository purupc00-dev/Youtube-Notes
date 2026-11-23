#!/usr/bin/env python3
"""Strict validation for CineStory story_plan.json; no external dependencies."""
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    args = parser.parse_args()
    try:
        data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: cannot read JSON: {exc}")
        return 2
    errors: list[str] = []
    if not isinstance(data, dict):
        print("PLAN INVALID: top-level JSON must be an object")
        return 1
    required_top = ["title", "language", "duration_seconds", "fps", "aspect_ratio", "narration", "characters", "scenes"]
    for key in required_top:
        if key not in data:
            errors.append(f"missing top-level field: {key}")
    if data.get("aspect_ratio") != "9:16":
        errors.append("aspect_ratio must be 9:16")
    try:
        duration = float(data.get("duration_seconds"))
        if not math.isfinite(duration) or duration <= 0:
            errors.append("duration_seconds must be a finite positive number")
    except (TypeError, ValueError):
        duration = None
        errors.append("duration_seconds must be a finite positive number")
    try:
        fps = int(data.get("fps"))
        if fps < 12 or fps > 60:
            errors.append("fps must be between 12 and 60")
    except (TypeError, ValueError):
        errors.append("fps must be an integer between 12 and 60")
    character_refs = data.get("character_references", [])
    if not isinstance(character_refs, list):
        errors.append("character_references must be an array")
        character_refs = []
    if len(character_refs) > 4:
        errors.append("character_references may contain at most four images (Cloudflare FLUX.2 Klein limit)")
    reference_ids: set[str] = set()
    for ref_index, ref in enumerate(character_refs, 1):
        if not isinstance(ref, dict):
            errors.append(f"character_references[{ref_index}]: must be an object")
            continue
        ref_id = str(ref.get("id", "")).strip()
        if not ref_id:
            errors.append(f"character_references[{ref_index}]: id is required")
        elif ref_id in reference_ids:
            errors.append(f"character_references[{ref_index}]: duplicate id {ref_id!r}")
        else:
            reference_ids.add(ref_id)
        prompt = ref.get("image_prompt") or ref.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"character_references[{ref_index}]: image_prompt is required")

    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("scenes must be a non-empty array")
        scenes = []
    scene_ids: set[str] = set()
    expected = ["id", "start", "end", "narration", "shot", "action", "image_prompt", "motion_prompt", "negative_prompt"]
    previous_end = 0.0
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            errors.append(f"scene {index}: must be an object")
            continue
        for key in expected:
            value = scene.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"scene {index}: missing/empty {key}")
        sid = str(scene.get("id", "")).strip()
        if sid:
            if sid in scene_ids:
                errors.append(f"scene {index}: duplicate id {sid!r}")
            scene_ids.add(sid)
        requested_refs = scene.get("reference_character_ids", [])
        if not isinstance(requested_refs, list):
            errors.append(f"scene {index}: reference_character_ids must be an array")
        else:
            unknown_refs = {str(value) for value in requested_refs} - reference_ids
            if unknown_refs:
                errors.append(f"scene {index}: unknown character reference ID(s): {', '.join(sorted(unknown_refs))}")
        try:
            start = float(scene["start"]); end = float(scene["end"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                errors.append(f"scene {index}: invalid finite time range {start}-{end}")
                continue
            if index == 1 and abs(start) > 0.03:
                errors.append(f"scene 1: must start at 0.00s (got {start:.3f}s)")
            elif index > 1 and abs(start - previous_end) > 0.03:
                relation = "overlaps" if start < previous_end else "has a gap after"
                errors.append(f"scene {index}: {relation} previous scene ({start:.3f}s vs {previous_end:.3f}s); keep scene timecodes contiguous")
            previous_end = end
        except (KeyError, TypeError, ValueError):
            errors.append(f"scene {index}: start/end must be finite numbers")
    if scenes and duration is not None and abs(previous_end - duration) > 0.10:
        errors.append(f"last scene ends at {previous_end:.2f}s but duration_seconds is {duration:.2f}s (tolerance 0.10s)")
    if not isinstance(data.get("characters"), (list, dict)):
        errors.append("characters must be an array or object")
    if errors:
        print("PLAN INVALID")
        for error in errors: print(" - " + error)
        return 1
    print(f"PLAN OK: {len(scenes)} scenes, {duration:.2f}s, 9:16, {fps} fps")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
