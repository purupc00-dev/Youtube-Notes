#!/usr/bin/env python3
"""Copy a user's own narration into a CineStory project without altering the original."""
import argparse, shutil, sys
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('audio_file'); p.add_argument('--project',required=True)
a=p.parse_args(); src=Path(a.audio_file).expanduser().resolve(); project=Path(a.project).expanduser().resolve()
if not src.is_file(): sys.exit(f'ERROR: audio file not found: {src}')
allowed={'.wav','.mp3','.m4a','.aac','.ogg','.flac','.opus'}
if src.suffix.lower() not in allowed: sys.exit(f'ERROR: unsupported audio extension {src.suffix}; supported: {", ".join(sorted(allowed))}')
outdir=project/'audio'; outdir.mkdir(parents=True,exist_ok=True)
dest=outdir/f'voice-original{src.suffix.lower()}'
if dest.exists():
    n=2
    while (outdir/f'voice-original-{n}{src.suffix.lower()}').exists(): n+=1
    dest=outdir/f'voice-original-{n}{src.suffix.lower()}'
shutil.copy2(src,dest)
print(f'IMPORTED: {dest}')
print('Original source was not modified. Use this copy as the narration master; align captions and scene timing to its actual duration.')
