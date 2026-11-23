#!/usr/bin/env python3
"""Create a numbered storyboard contact sheet from scene images and a story plan."""
import argparse, json, math, sys, textwrap
from pathlib import Path
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit('Pillow is required: python -m pip install Pillow')

p=argparse.ArgumentParser(); p.add_argument('plan'); p.add_argument('--images',default='assets/images'); p.add_argument('--out',default='review/storyboard.jpg'); p.add_argument('--columns',type=int,default=3); p.add_argument('--thumb-width',type=int,default=360)
a=p.parse_args(); plan=json.loads(Path(a.plan).read_text(encoding='utf-8')); root=Path(a.images); scenes=plan['scenes']; cols=max(1,a.columns); tw=max(160,a.thumb_width); th=round(tw*16/9); text_h=116; rows=math.ceil(len(scenes)/cols); pad=24; gap=18
W=pad*2+cols*tw+(cols-1)*gap; H=pad*2+rows*(th+text_h)+(rows-1)*gap
canvas=Image.new('RGB',(W,H),(22,22,24)); draw=ImageDraw.Draw(canvas)
try: font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',18); small=ImageFont.truetype('/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf',14)
except: font=small=ImageFont.load_default()
missing=[]
for idx,s in enumerate(scenes):
    col=idx%cols; row=idx//cols; x=pad+col*(tw+gap); y=pad+row*(th+text_h+gap)
    name=s.get('image_file') or f"{s['id']}.png"; path=root/name
    if path.exists():
        im=Image.open(path).convert('RGB'); ratio=max(tw/im.width,th/im.height); im=im.resize((round(im.width*ratio),round(im.height*ratio))); left=(im.width-tw)//2; top=(im.height-th)//2; im=im.crop((left,top,left+tw,top+th)); canvas.paste(im,(x,y))
    else:
        missing.append(s['id']); draw.rectangle((x,y,x+tw,y+th),fill=(65,45,45)); draw.text((x+16,y+16),f"MISSING\n{name}",font=font,fill=(255,220,220))
    draw.text((x,y+th+8),f"{idx+1:02d}  {s['id']}  |  {s['start']:.1f}-{s['end']:.1f}s",font=font,fill=(250,220,160))
    narr=(s.get('narration') or '').replace('\n',' '); lines=textwrap.wrap(narr,width=43)[:3]; draw.multiline_text((x,y+th+36),'\n'.join(lines),font=small,fill=(230,230,230),spacing=4)
out=Path(a.out); out.parent.mkdir(parents=True,exist_ok=True); canvas.save(out,quality=90)
print(f"STORYBOARD: {out.resolve()} | {len(scenes)} scenes | missing: {', '.join(missing) if missing else 'none'}")
if missing: sys.exit(1)
