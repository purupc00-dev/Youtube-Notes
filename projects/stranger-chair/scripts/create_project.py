#!/usr/bin/env python3
"""Create a CineStory project with a starter plan and media folders."""
import argparse, json, re
from pathlib import Path

STARTER = {
  "title": "My Cinematic Short", "language": "Hindi", "duration_seconds": 5,
  "fps": 24, "aspect_ratio": "9:16", "narration": "अपनी कहानी यहाँ लिखें।",
  "characters": [{"id": "hero", "name": "Hero", "description": "Character appearance, face, hair, body build and exact outfit. Keep identical in every scene.", "reference_image": None}],
  "scenes": [{"id": "scene_01", "start": 0, "end": 5, "narration": "पहली पंक्ति यहाँ लिखें।", "shot": "Close-up, eye level", "action": "One clear visible action.", "image_prompt": "Write the complete cinematic image prompt and repeat the exact character description.", "motion_prompt": "One small action, slow camera push-in, preserve character identity and clothing, 5 seconds, no cuts, no text.", "negative_prompt": "text, captions, watermark, logo, extra fingers, malformed hands, inconsistent face, inconsistent clothes", "caption": "पहली पंक्ति", "sfx": "soft room tone"}]
}

def slug(s):
    s = re.sub(r'[^a-zA-Z0-9_-]+', '-', s.strip()).strip('-').lower()
    return s or 'story'

p = argparse.ArgumentParser()
p.add_argument('name')
p.add_argument('--root', default='.')
a = p.parse_args()
project = Path(a.root).expanduser().resolve() / slug(a.name)
for d in ['assets/images', 'assets/video', 'audio', 'renders', 'review']:
    (project / d).mkdir(parents=True, exist_ok=True)
plan = project / 'story_plan.json'
if not plan.exists(): plan.write_text(json.dumps(STARTER, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
(project / 'captions.srt').write_text('', encoding='utf-8')
(project / 'README.txt').write_text('Edit story_plan.json, then validate and generate assets using the CineStory AI skill scripts.\n', encoding='utf-8')
print(project)
