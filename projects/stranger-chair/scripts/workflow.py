#!/usr/bin/env python3
"""Safe, sequential checkpoint state manager for the interactive CineStory workflow."""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

STAGES = ["concepts", "script", "voice", "characters", "storyboard", "motion", "sound_captions", "render", "qa", "delivered"]

def load_state(project_dir: str | Path) -> tuple[Path, dict]:
    project = Path(project_dir).expanduser().resolve()
    state_path = project / "workflow.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"No workflow state at {state_path}. Run: workflow.py init <project-dir>")
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read workflow state {state_path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("approved"), list) or not isinstance(data.get("selections"), dict):
        raise ValueError(f"Workflow state is malformed: {state_path}")
    if data.get("current_stage") not in STAGES:
        raise ValueError(f"Workflow state has an unknown current_stage: {data.get('current_stage')!r}")
    return state_path, data

def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try: os.unlink(temp_name)
        except OSError: pass
        raise

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("project_dir")
    init.add_argument("--start-at", choices=("concepts", "script"), default="concepts",
                      help="Start at script when the user supplies an existing script (skips concept ideation).")
    status = sub.add_parser("status"); status.add_argument("project_dir")
    approve = sub.add_parser("approve"); approve.add_argument("project_dir"); approve.add_argument("stage", choices=STAGES); approve.add_argument("--note", default="")
    select = sub.add_parser("select"); select.add_argument("project_dir"); select.add_argument("key"); select.add_argument("value")
    nxt = sub.add_parser("next"); nxt.add_argument("project_dir")
    args = parser.parse_args()
    try:
        if args.command == "init":
            project = Path(args.project_dir).expanduser().resolve(); project.mkdir(parents=True, exist_ok=True)
            state = project / "workflow.json"
            if state.exists(): print(f"EXISTS: {state}"); return 0
            start_stage = args.start_at
            approved = ["concepts"] if start_stage == "script" else []
            atomic_save(state, {"project": project.name, "current_stage": start_stage, "approved": approved, "selections": {}, "notes": {}, "assets": {}, "created_by": "CineStory AI"})
            print(f"INITIALIZED: {state}"); return 0
        state_path, data = load_state(args.project_dir)
        if args.command == "status":
            print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
        if args.command == "approve":
            current = data["current_stage"]
            if args.stage != current:
                print(f"REFUSED: current stage is {current!r}; cannot approve {args.stage!r} out of order.", file=sys.stderr)
                return 2
            if args.stage not in data["approved"]: data["approved"].append(args.stage)
            if args.note: data.setdefault("notes", {})[args.stage] = args.note
            idx = STAGES.index(args.stage)
            data["current_stage"] = STAGES[min(idx + 1, len(STAGES) - 1)]
            atomic_save(state_path, data)
            print(f"APPROVED: {args.stage}; NEXT: {data['current_stage']}"); return 0
        if args.command == "select":
            data.setdefault("selections", {})[args.key] = args.value
            atomic_save(state_path, data)
            print(f"SAVED: {args.key}={args.value}"); return 0
        if args.command == "next":
            print(f"CURRENT STAGE: {data['current_stage']}")
            print("APPROVED: " + (", ".join(data["approved"]) or "none"))
            print("NEXT ACTION: follow the gate instructions for this stage in references/approval-workflow.md")
            return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
