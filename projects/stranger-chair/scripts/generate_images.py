#!/usr/bin/env python3
"""Premium Multi-Provider Image Generator for CineStory AI v10 Final.

Supports:
1. `cloudflare` (default): Existing Cloudflare Worker (FLUX.2 Klein 4B) via
   `CINESTORY_CLOUDFLARE_API_KEY` + `CINESTORY_CLOUDFLARE_WORKER_URL`.
2. `leonardo`: Official Leonardo.ai REST API (Phoenix 1.0 / Kino XL) via
   `LEONARDO_API_KEY` — high-detail 3D cinematic depth of field & rich lighting.
3. `seedream`: ByteDance Seedream via fal.ai (`FAL_KEY`) or Ark/custom endpoint
   (`SEEDREAM_API_KEY` + `SEEDREAM_API_URL`) — ultra-rich lived-in environments.
4. `openai`: OpenAI Images API (`OPENAI_API_KEY`, requires `--allow-billable-api`).

Every provider enforces `--max-images` caps, reuses existing files, never retries
billable POSTs on ambiguous timeouts, and automatically enhances prompts with
CineStory's 85mm f/1.4 shallow-DoF & rich-environment lighting anchors when needed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LEONARDO_DEFAULT_MODEL = "de7d3faf-762f-48e0-b3b7-9d0ac3a3fcf3"  # Leonardo Phoenix 1.0
SEEDREAM_DEFAULT_FAL_URL = "https://fal.run/fal-ai/bytedance/seedream/v3/text-to-image"


def load_env(project_root: Path) -> None:
    for candidate in (project_root / ".env", Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def enhance_cinematic_prompt(prompt: str) -> str:
    """Append rich depth-of-field and lived-in environment anchors if absent."""
    low = prompt.lower()
    additions = []
    if "depth of field" not in low and "bokeh" not in low:
        additions.append("shot on 85mm f/1.4 lens with creamy shallow depth of field and soft foreground/background bokeh")
    if "background" not in low and "environment" not in low:
        additions.append("richly detailed lived-in background environment with layered props and atmospheric depth")
    if "rim light" not in low and "golden" not in low:
        additions.append("warm golden directional key light with soft volumetric rim light and tactile fabric/skin texture")
    if "no text" not in low:
        additions.append("no text, no subtitles, no watermark, no logo")
    return prompt if not additions else f"{prompt.rstrip('. ')}. {', '.join(additions)}."


def generate_leonardo_image(prompt: str, negative_prompt: str, api_key: str, width: int = 768, height: int = 1344,
                            model_id: str | None = None, poll_timeout: int = 120) -> bytes:
    """Generate one vertical portrait image via official Leonardo.ai REST API (no retry on POST)."""
    payload = {
        "prompt": prompt[:1500],
        "negative_prompt": (negative_prompt or "text, watermark, logo, blurry face, distorted hands")[:1000],
        "modelId": model_id or os.getenv("LEONARDO_MODEL_ID", LEONARDO_DEFAULT_MODEL),
        "width": width,
        "height": height,
        "num_images": 1,
        "alchemy": True,
        "contrast": 3.5,
        "ultra": False,
    }
    req = urllib.request.Request(
        "https://cloud.leonardo.ai/api/rest/v1/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        created = json.loads(resp.read().decode("utf-8"))
    gen_id = (created.get("sdGenerationJob") or {}).get("generationId")
    if not gen_id:
        raise RuntimeError(f"Leonardo API did not return generationId: {created}")

    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        time.sleep(3.0)
        poll_req = urllib.request.Request(
            f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(poll_req, timeout=30) as resp:
            status_doc = json.loads(resp.read().decode("utf-8"))
        gen = status_doc.get("generations_by_pk") or {}
        if gen.get("status") == "COMPLETE":
            imgs = gen.get("generated_images") or []
            if imgs and imgs[0].get("url"):
                with urllib.request.urlopen(imgs[0]["url"], timeout=90) as img_resp:
                    return img_resp.read()
            raise RuntimeError("Leonardo completed without image URL.")
        if gen.get("status") == "FAILED":
            raise RuntimeError(f"Leonardo generation failed: {gen}")
    raise TimeoutError(f"Leonardo generation {gen_id} timed out after {poll_timeout}s.")


def generate_seedream_image(prompt: str, api_key: str, width: int = 768, height: int = 1344,
                            endpoint_url: str | None = None) -> bytes:
    """Generate one vertical portrait image via Seedream (fal.ai or custom Seedream endpoint)."""
    url = endpoint_url or os.getenv("SEEDREAM_API_URL", SEEDREAM_DEFAULT_FAL_URL)
    is_fal = "fal.run" in url or "fal.ai" in url
    headers = {
        "Authorization": f"Key {api_key}" if is_fal else f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "prompt": prompt[:2000],
        "image_size": {"width": width, "height": height} if is_fal else f"{width}x{height}",
        "num_images": 1,
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    images = data.get("images") or data.get("data") or []
    if not images:
        raise RuntimeError(f"Seedream endpoint returned no images: {data}")
    item = images[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=90) as img_resp:
            return img_resp.read()
    raise RuntimeError("Seedream response contained neither url nor b64_json.")


def resolve_provider(requested: str) -> str:
    if requested != "auto":
        return requested
    env_pref = os.getenv("CINESTORY_IMAGE_PROVIDER", "").strip().lower()
    if env_pref in {"cloudflare", "leonardo", "seedream", "openai"}:
        return env_pref
    if os.getenv("SEEDREAM_API_KEY") or os.getenv("FAL_KEY"):
        return "seedream"
    if os.getenv("LEONARDO_API_KEY"):
        return "leonardo"
    return "cloudflare"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="Story plan JSON")
    parser.add_argument("--out", default="assets/images")
    parser.add_argument("--provider", choices=("auto", "cloudflare", "leonardo", "seedream", "openai"), default="auto")
    parser.add_argument("--model", default=None)
    parser.add_argument("--size", default=os.getenv("OPENAI_IMAGE_SIZE", "1024x1536"))
    parser.add_argument("--quality", default=os.getenv("OPENAI_IMAGE_QUALITY", "medium"))
    parser.add_argument("--allow-billable-api", action="store_true", help="Required explicit opt-in for OpenAI Images API.")
    parser.add_argument("--max-images", type=int, default=24)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    plan_path = Path(args.plan).expanduser().resolve()
    load_env(plan_path.parent)
    provider = resolve_provider(args.provider)

    if provider == "cloudflare":
        cf_script = Path(__file__).resolve().with_name("generate_cloudflare_images.py")
        cmd = [sys.executable, str(cf_script), str(plan_path), "--out", args.out, "--max-images", str(args.max_images)]
        if args.overwrite:
            cmd.append("--overwrite")
        return subprocess.run(cmd).returncode

    if provider == "openai" and not args.allow_billable_api:
        sys.exit("ERROR: OpenAI Images API requires explicit --allow-billable-api opt-in.")

    data = json.loads(plan_path.read_text(encoding="utf-8"))
    out = Path(args.out)
    out = out if out.is_absolute() else plan_path.parent / out
    out.mkdir(parents=True, exist_ok=True)
    scenes = data.get("scenes")
    if not isinstance(scenes, list):
        sys.exit("ERROR: story plan must contain a scenes list.")

    pending, manifest = [], []
    for scene in scenes:
        name = scene.get("image_file") or f"{scene['id']}.png"
        dest = out / name
        if dest.exists() and not args.overwrite:
            manifest.append({"scene": scene["id"], "image": str(dest), "provider": "reused"})
        else:
            pending.append((scene, dest))

    if len(pending) > args.max_images:
        sys.exit(f"PRE-FLIGHT STOP: {len(pending)} new image(s) needed, exceeds --max-images={args.max_images}.")

    for idx, (scene, dest) in enumerate(pending, 1):
        prompt = enhance_cinematic_prompt(scene["image_prompt"])
        neg = scene.get("negative_prompt", "")
        print(f"Generating {idx}/{len(pending)} via {provider}: {scene['id']} ...", flush=True)
        if provider == "leonardo":
            key = os.getenv("LEONARDO_API_KEY", "").strip()
            if not key:
                sys.exit("ERROR: configure LEONARDO_API_KEY in .env to use Leonardo.ai.")
            raw = generate_leonardo_image(prompt, neg, key, model_id=args.model)
        elif provider == "seedream":
            key = (os.getenv("SEEDREAM_API_KEY") or os.getenv("FAL_KEY") or "").strip()
            if not key:
                sys.exit("ERROR: configure SEEDREAM_API_KEY or FAL_KEY in .env to use Seedream.")
            raw = generate_seedream_image(prompt, key)
        else:
            key = os.getenv("OPENAI_API_KEY", "").strip()
            if not key:
                sys.exit("ERROR: configure OPENAI_API_KEY in .env.")
            payload = {"model": args.model or os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": args.size, "quality": args.quality, "n": 1}
            req = urllib.request.Request("https://api.openai.com/v1/images/generations", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=240) as resp:
                item = json.loads(resp.read())["data"][0]
            raw = base64.b64decode(item["b64_json"]) if item.get("b64_json") else urllib.request.urlopen(item["url"], timeout=120).read()
        dest.write_bytes(raw)
        scene["image_file"] = dest.name
        manifest.append({"scene": scene["id"], "image": str(dest), "provider": provider})

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"DONE: {len(manifest)} scene image(s) via {provider} -> {out}/manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
