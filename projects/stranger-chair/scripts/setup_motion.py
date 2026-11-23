#!/usr/bin/env python3
"""Check or install CineStory's optional CPU 2.5D segmentation runtime.

`--help` is side-effect free. Use `--check` to inspect readiness, or `--install
--download-model` for an explicit first-time setup. The production orchestrator
may invoke the latter automatically when its runtime permits package installs.
"""
from __future__ import annotations
import argparse
import importlib.util
import subprocess
import sys

REQUIRED = {
    "rembg": "rembg[cpu]>=2.0.60",
    "cv2": "opencv-python-headless>=4.8",
    "PIL": "Pillow>=10.0",
    "numpy": "numpy>=1.24",
}

def missing_modules() -> list[str]:
    return [module for module in REQUIRED if importlib.util.find_spec(module) is None]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check imports without installing or downloading a model.")
    parser.add_argument("--install", action="store_true", help="Install missing CPU segmentation dependencies.")
    parser.add_argument("--download-model", action="store_true", help="Load U2Net once after dependencies are ready.")
    parser.add_argument("--model", default="u2net", help="rembg model name (default: u2net).")
    args = parser.parse_args()

    missing = missing_modules()
    if missing and args.install:
        packages = [REQUIRED[module] for module in missing]
        print("Installing motion dependencies:", ", ".join(packages), flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", *packages], check=True)
        missing = missing_modules()

    if missing:
        print("NOT READY. Missing Python modules:", ", ".join(missing), file=sys.stderr)
        if not args.install:
            print("Run: python scripts/setup_motion.py --install --download-model", file=sys.stderr)
        return 2

    print("Motion dependencies: READY")
    if args.download_model:
        try:
            from rembg import new_session
            new_session(args.model)
        except Exception as exc:
            print(f"Could not initialize segmentation model {args.model!r}: {exc}", file=sys.stderr)
            return 3
        print(f"Segmentation model: {args.model} READY")
    elif args.check:
        print("Model download not requested. Use --download-model to warm the model cache.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
