#!/usr/bin/env python3
"""Automatically generate all approved CineStory scene stills via the user's Cloudflare Worker.

The Worker endpoint accepts POST / with JSON {"prompt": "..."}, a Bearer token,
and responds with raw image bytes (image/jpeg or image/png). Secrets are read from
CINESTORY_CLOUDFLARE_API_KEY or a local .env file; never from a story plan or chat.
"""
from __future__ import annotations
import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

try:
    from PIL import Image
except ImportError:
    Image = None

DEFAULT_WORKER_URL = "https://imagegen.mramazifier.workers.dev/"
KEY_ENV = "CINESTORY_CLOUDFLARE_API_KEY"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def load_dotenv(start: Path | None = None) -> None:
    """Load a local .env without overriding real process environment values."""
    candidates = []
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidates.append(parent / ".env")
    candidates.append(Path(__file__).resolve().parents[1] / ".env")
    seen = set()
    for env_file in candidates:
        if env_file in seen:
            continue
        seen.add(env_file)
        if not env_file.is_file():
            continue
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward a Bearer token to a redirect target."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_worker_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "imagegen.mramazifier.workers.dev":
        raise ValueError("Worker URL must use HTTPS and the configured host imagegen.mramazifier.workers.dev.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Worker URL must not include credentials, query strings, or fragments.")
    return value


def safe_scene_name(scene: dict, index: int) -> str:
    raw = str(scene.get("image_file") or f"scene_{index:02d}.jpg")
    name = Path(raw).name
    stem = Path(name).stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("._") or f"scene_{index:02d}"
    return stem


def image_extension(content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    mapping = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if media_type not in mapping:
        raise ValueError(f"Worker returned unsupported image type: {media_type or 'missing'}")
    return mapping[media_type]


def atomic_json_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_state(plan_path: Path, data: dict, manifest_path: Path, manifest: list) -> None:
    atomic_json_write(plan_path, data)
    atomic_json_write(manifest_path, manifest)


def reference_as_base64(path: Path, max_side: int = 511) -> dict:
    """Return a small, aspect-preserving JPEG reference compatible with FLUX.2 Klein."""
    if Image is None:
        raise RuntimeError("Pillow is required to use character reference images. Install it with: python -m pip install Pillow")
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
        raw = buffer.getvalue()
    return {"mimeType": "image/jpeg", "data": base64.b64encode(raw).decode("ascii")}


def validate_image_bytes(raw: bytes, expected_size: tuple[int, int] = (720, 1280)) -> None:
    if not raw:
        raise RuntimeError("Worker returned an empty image response.")
    if Image is None:
        raise RuntimeError("Pillow is required to validate generated images. Install project requirements before production.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise RuntimeError(f"Unexpected image format: {image.format}")
            if image.size != expected_size:
                raise RuntimeError(f"Worker returned {image.width}x{image.height}; expected {expected_size[0]}x{expected_size[1]}.")
    except Exception as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"Worker response is not a valid decodable image: {error}") from error


def request_image(opener, worker_url: str, key: str, prompt: str, references: list[dict] | None = None, reference_mode: str = "character") -> tuple[bytes, str, int | None]:
    # This contract matches the user's deployed CineStory Worker v2. Do not rename
    # the field: the Worker reads `references` (or `referenceImages`), not `reference_images`.
    payload = {"prompt": prompt, "width": 720, "height": 1280, "referenceMode": reference_mode}
    if references:
        if len(references) > 4:
            raise ValueError("FLUX.2 Klein 4B accepts at most four reference images.")
        payload["references"] = references[:4]
    request = urllib.request.Request(worker_url, data=json.dumps(payload).encode("utf-8"), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "image/jpeg, image/png, image/webp"
    }, method="POST")
    try:
        # A generation POST is not safely retryable. If the response is lost after
        # inference, replaying may consume Workers AI quota twice. Fail clearly.
        with opener.open(request, timeout=240) as response:
            content_type = response.headers.get("Content-Type", "")
            extension = image_extension(content_type)
            used_refs_header = response.headers.get("X-CineStory-Reference-Count")
            used_refs = int(used_refs_header) if used_refs_header is not None else None
            raw = response.read()
        validate_image_bytes(raw, (720, 1280))
        if references and used_refs != len(references):
            raise RuntimeError(f"Character reference conditioning was not confirmed: sent {len(references)}, Worker reported {used_refs!r}. Refusing to silently continue with unconditioned images.")
        return raw, extension, used_refs
    except urllib.error.HTTPError as error:
        body = error.read(1200).decode("utf-8", "replace")
        raise RuntimeError(f"Worker HTTP {error.code}: {body}") from error
    except Exception as error:
        raise RuntimeError(f"Cloudflare image generation failed without retry to avoid duplicate quota use: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="Approved CineStory story_plan.json")
    parser.add_argument("--out", default="assets/images", help="Image output directory")
    parser.add_argument("--max-images", type=int, default=None,
                        help="Maximum new image requests per batch (default: CINESTORY_MAX_IMAGES or 14). Larger projects are split into batches.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate existing scene images")
    parser.add_argument("--dry-run", action="store_true", help="Show the exact request count without calling the Worker")
    parser.add_argument("--scene-ids", nargs="*", help="Regenerate only these scene IDs (optional)")
    args = parser.parse_args()

    plan_path = Path(args.plan).resolve()
    load_dotenv(plan_path.parent)
    if args.max_images is None:
        try:
            args.max_images = int(os.getenv("CINESTORY_MAX_IMAGES", "14"))
        except ValueError:
            parser.error("CINESTORY_MAX_IMAGES must be an integer.")
    if args.max_images < 1:
        parser.error("--max-images/CINESTORY_MAX_IMAGES must be at least 1")
    try:
        worker_url = safe_worker_url(os.getenv("CINESTORY_CLOUDFLARE_WORKER_URL", DEFAULT_WORKER_URL))
    except ValueError as error:
        sys.exit(f"ERROR: {error}")

    key = os.getenv(KEY_ENV, "").strip()
    if not key and not args.dry_run:
        sys.exit(f"ERROR: configure {KEY_ENV} once in the agent/runtime secret settings or local .env. Never paste it into chat or story_plan.json.")

    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as error:
        sys.exit(f"ERROR: cannot read story plan: {error}")
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        sys.exit("ERROR: story plan must contain a non-empty scenes list.")

    out = Path(args.out).expanduser()
    out = out if out.is_absolute() else plan_path.parent / out
    out = out.resolve()
    manifest_path = out / "manifest.json"
    manifest: list[dict] = []
    pending: list[tuple[dict | None, str, str, str, bool]] = []
    selected = set(args.scene_ids or [])
    all_scene_ids = {str(scene.get("id") or f"scene_{i:02d}") for i, scene in enumerate(scenes, 1)}
    unknown_selected = selected - all_scene_ids
    if unknown_selected:
        sys.exit("ERROR: unknown --scene-ids value(s): " + ", ".join(sorted(unknown_selected)))

    # Character reference prompts are optional in the plan but recommended for recurring casts.
    character_refs = data.get("character_references", [])
    if not isinstance(character_refs, list):
        sys.exit("ERROR: character_references must be a list when provided.")
    if len(character_refs) > 4:
        sys.exit("ERROR: FLUX.2 Klein 4B supports at most four reference images per generation request.")
    ref_ids = []
    reference_stems = set()
    for ref_index, ref in enumerate(character_refs, 1):
        if not isinstance(ref, dict):
            sys.exit(f"ERROR: character_references[{ref_index - 1}] must be an object.")
        ref_id = str(ref.get("id") or f"character_{ref_index:02d}").strip()
        if not ref_id:
            sys.exit(f"ERROR: character_references[{ref_index - 1}] has an empty id.")
        if ref_id in ref_ids:
            sys.exit(f"ERROR: duplicate character reference id: {ref_id}")
        ref_ids.append(ref_id)
        prompt = ref.get("image_prompt") or ref.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            sys.exit(f"ERROR: character reference {ref_id} has no image_prompt.")
        raw_name = str(ref.get("image_file") or f"character_{ref_id}.jpg")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(raw_name).stem).strip("._") or f"character_{ref_index:02d}"
        if stem in reference_stems:
            sys.exit(f"ERROR: multiple character references resolve to the same image filename stem: {stem}")
        reference_stems.add(stem)
        existing = next((p for p in out.glob(stem + ".*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES), None)
        if existing and not args.overwrite:
            ref["image_file"] = existing.name
            manifest.append({"scene": f"character:{ref_id}", "image": str(existing), "status": "reused"})
        else:
            pending.append((None, f"character:{ref_id}", prompt.strip(), stem, True))

    output_stems = set(reference_stems)
    for index, scene in enumerate(scenes, 1):
        scene_id = str(scene.get("id") or f"scene_{index:02d}")
        if selected and scene_id not in selected:
            continue
        prompt = scene.get("image_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            sys.exit(f"ERROR: scene {scene_id} has no image_prompt.")
        negative = scene.get("negative_prompt")
        if isinstance(negative, str) and negative.strip():
            prompt = prompt.strip() + "\nAvoid: " + negative.strip()
        stem = safe_scene_name(scene, index)
        if stem in output_stems:
            sys.exit(f"ERROR: multiple scenes resolve to the same image filename stem: {stem}")
        output_stems.add(stem)
        existing = next((p for p in out.glob(stem + ".*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES), None)
        if existing and not args.overwrite:
            scene["image_file"] = existing.name
            manifest.append({"scene": scene_id, "image": str(existing), "status": "reused"})
        else:
            pending.append((scene, scene_id, prompt, stem, False))

    batch_count = (len(pending) + args.max_images - 1) // args.max_images
    print(f"PRE-FLIGHT: {len(pending)} new image request(s) in {batch_count} batch(es) of at most {args.max_images}; target={worker_url}; model=FLUX.2 Klein 4B; output=720x1280 (9:16). Existing images are reused.", flush=True)
    if args.dry_run:
        print("DRY RUN: no request sent.")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(NoRedirect())

    # Character references are placed first in the queue. Generate in bounded
    # sequential batches so larger stories do not fail just because they exceed
    # the per-batch request cap.
    for batch_index in range(batch_count):
        batch_start = batch_index * args.max_images
        batch = pending[batch_start:batch_start + args.max_images]
        print(f"IMAGE BATCH {batch_index + 1}/{batch_count}: {len(batch)} request(s)", flush=True)
        for offset, (scene, scene_id, prompt, stem, is_character) in enumerate(batch):
            index = batch_start + offset + 1
            refs = []
            if not is_character and character_refs:
                requested_ids = set(scene.get("reference_character_ids", []) or [])
                known_ids = {str(item.get("id")) for item in character_refs}
                unknown_ids = requested_ids - known_ids
                if unknown_ids:
                    raise SystemExit(f"Scene {scene_id} requests unknown character reference ID(s): {', '.join(sorted(unknown_ids))}")
                selected_refs = [r for r in character_refs if not requested_ids or str(r.get("id")) in requested_ids]
                if not selected_refs:
                    raise SystemExit(f"Scene {scene_id} has no matching character references; refusing unconditioned generation.")
                for ref in selected_refs[:4]:
                    ref_file = ref.get("image_file")
                    if not ref_file:
                        raise SystemExit(f"Character reference {ref.get('id')} has not been generated yet.")
                    ref_path = out / Path(ref_file).name
                    if not ref_path.is_file():
                        raise SystemExit(f"Character reference image is missing: {ref_path}")
                    refs.append(reference_as_base64(ref_path))

            print(f"Generating {index}/{len(pending)}: {scene_id}" + (f" with {len(refs)} character reference(s)" if refs else ""), flush=True)
            try:
                raw, extension, refs_used = request_image(opener, worker_url, key, prompt, refs, "character")
                if refs and refs_used != len(refs):
                    raise RuntimeError(f"Worker did not confirm all character references: requested {len(refs)}, reported {refs_used!r}.")
                dest = out / (stem + extension)
                if dest.exists() and not args.overwrite:
                    raise RuntimeError(f"Refusing to overwrite existing file: {dest}")
                temp = dest.with_suffix(dest.suffix + ".tmp")
                temp.write_bytes(raw)
                temp.replace(dest)
                if is_character:
                    ref_id = scene_id.split(":", 1)[1]
                    for ref in character_refs:
                        if str(ref.get("id")) == ref_id:
                            ref["image_file"] = dest.name
                            break
                    manifest.append({"scene": scene_id, "image": str(dest), "status": "generated", "width": 720, "height": 1280})
                else:
                    scene["image_file"] = dest.name
                    manifest.append({"scene": scene_id, "image": str(dest), "status": "generated", "width": 720, "height": 1280, "reference_images_requested": len(refs), "reference_images_applied": refs_used})
                save_state(plan_path, data, manifest_path, manifest)
            except Exception as error:
                sys.exit(f"Cloudflare image generation failed for {scene_id}: {error}")

    # Update manifest one last time, including any reused files.
    save_state(plan_path, data, manifest_path, manifest)
    print(f"DONE: {len(manifest)} image asset(s) recorded in {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
