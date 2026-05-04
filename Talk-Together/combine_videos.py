#!/usr/bin/env python3
"""
Combine four videos with the same numeric prefix into a 2x2 grid:
  top-left: v  (SelfTalk),  top-right: dt (DualTalk)
  bottom-left: r (Ours S1), bottom-right: ge (Ours S2)
White captions are overlaid using PIL + ffmpeg overlay filter.
"""

import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

INPUT_DIR  = Path(__file__).parent / "video"
OUTPUT_DIR = Path(__file__).parent / "video_presentation"
OUTPUT_DIR.mkdir(exist_ok=True)

# Per-quadrant size (pixels); combined grid is 2*W x 2*H
W, H        = 1024, 512
OUTER_PAD   = 24   # black border around the whole combined video
TEXT_PAD    = 14   # text offset from each quadrant's inner edge
FSIZE       = 40

# Text anchors account for the outer black border
CAPTIONS = [
    ("SelfTalk", OUTER_PAD + TEXT_PAD,           OUTER_PAD + TEXT_PAD),
    ("DualTalk", OUTER_PAD + W + TEXT_PAD,        OUTER_PAD + TEXT_PAD),
    ("Ours S1",  OUTER_PAD + TEXT_PAD,            OUTER_PAD + H + TEXT_PAD),
    ("Ours S2",  OUTER_PAD + W + TEXT_PAD,        OUTER_PAD + H + TEXT_PAD),
]

# Final canvas size after outer padding
OUT_W = 2 * W + 2 * OUTER_PAD
OUT_H = 2 * H + 2 * OUTER_PAD

FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial.ttf",
]


def make_overlay(path: str) -> None:
    img  = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = None
    for fp in FONT_PATHS:
        try:
            font = ImageFont.truetype(fp, FSIZE)
            break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()

    for text, x, y in CAPTIONS:
        # dark shadow for legibility
        for dx, dy in ((2, 2), (-1, -1), (2, -1), (-1, 2)):
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 200))
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    img.save(path)


# Build one overlay PNG reused across all videos
overlay_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
overlay_file.close()
make_overlay(overlay_file.name)

# Collect prefixes
prefixes = set()
for f in INPUT_DIR.iterdir():
    m = re.match(r"^(\d+)_(v|dt|r|ge)\.mp4$", f.name)
    if m:
        prefixes.add(m.group(1))

for prefix in sorted(prefixes, key=int):
    clips = {
        "v":  INPUT_DIR / f"{prefix}_v.mp4",
        "dt": INPUT_DIR / f"{prefix}_dt.mp4",
        "r":  INPUT_DIR / f"{prefix}_r.mp4",
        "ge": INPUT_DIR / f"{prefix}_ge.mp4",
    }

    missing = [k for k, p in clips.items() if not p.exists()]
    if missing:
        print(f"Skipping prefix {prefix}: missing {missing}")
        continue

    output = OUTPUT_DIR / f"{prefix}_combined.mp4"
    if output.exists():
        print(f"Already exists, skipping: {output.name}")
        continue

    cmd = [
        "ffmpeg", "-y",
        "-i", str(clips["v"]),
        "-i", str(clips["dt"]),
        "-i", str(clips["r"]),
        "-i", str(clips["ge"]),
        "-i", overlay_file.name,
        "-filter_complex",
        "[0:v][1:v]hstack=inputs=2[top];"
        "[2:v][3:v]hstack=inputs=2[bottom];"
        "[top][bottom]vstack=inputs=2[grid];"
        f"[grid]pad={OUT_W}:{OUT_H}:{OUTER_PAD}:{OUTER_PAD}:black[padded];"
        "[padded][4:v]overlay=0:0[out]",
        "-map", "[out]",
        "-map", "3:a",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-crf", "20",
        "-preset", "fast",
        str(output),
    ]

    print(f"Processing prefix {prefix} -> {output.name}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[-600:]}")
    else:
        print(f"  Done.")

Path(overlay_file.name).unlink(missing_ok=True)
print("All done.")
