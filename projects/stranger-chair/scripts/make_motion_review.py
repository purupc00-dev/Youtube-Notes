#!/usr/bin/env python3
"""Build a short review reel from actual rendered scene-motion clips."""
import argparse, shutil, subprocess, sys
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--clips', default='assets/video')
p.add_argument('--out', default='review/motion-preview.mp4')
p.add_argument('--seconds-per-scene', type=float, default=1.5)
p.add_argument('--width', type=int, default=540)
p.add_argument('--height', type=int, default=960)
p.add_argument('--fps', type=int, default=12)
p.add_argument('--crf', type=int, default=24)
a = p.parse_args()
ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
if not ffmpeg or not ffprobe:
    sys.exit('FFmpeg and FFprobe are required.')
clips = Path(a.clips)
files = sorted(clips.glob('scene_*.mp4'))
if not files:
    sys.exit(f'No scene_*.mp4 clips found in {clips}')
out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
tmp = out.parent / '_motion_preview_segments'; tmp.mkdir(exist_ok=True)
parts = []
for i, source in enumerate(files):
    meta = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', str(source)], capture_output=True, text=True, check=True)
    duration = float(meta.stdout.strip() or 0)
    sample = min(a.seconds_per_scene, max(0.5, duration))
    start = max(0.0, (duration - sample) / 2.0)
    part = tmp / f'{i:03d}.mp4'
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-ss', f'{start:.3f}', '-i', str(source),
                    '-t', f'{sample:.3f}', '-vf', f'scale={a.width}:{a.height}:force_original_aspect_ratio=increase,crop={a.width}:{a.height},fps={a.fps},setsar=1',
                    '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(a.crf), '-pix_fmt', 'yuv420p', str(part)], check=True)
    parts.append(part)
concat = tmp / 'concat.txt'
concat.write_text(''.join("file '" + str(x.resolve()).replace("'", "'\\''") + "'\n" for x in parts), encoding='utf-8')
subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(concat),
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(a.crf), '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(out)], check=True)
print(f'MOTION PREVIEW: {out} ({len(parts)} scenes)')
