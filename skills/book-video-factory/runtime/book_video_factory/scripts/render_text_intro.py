#!/usr/bin/env python3
"""Generate a minimal text intro: hook sentence appearing character by
character on a plain black background.

Replaces the old knockout split-reveal (text + black mask) with a quiet
progressive reveal — no mask, no split. Each character emerges blur-to-sharp
with a slight upward drift, one sentence per line, in an expressive display
face. The clip duration is matched to the hook voice so the carousel (and
the "今天分享" guide) starts right after it.

Usage:
    python3 scripts/render_text_intro.py <project> \
        --text "你以为自己是在解释，别人听到的却是指责" \
        --duration 5.2 [--output PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import _bootstrap  # noqa: F401
from book_video_factory.font_resolver import FontResolutionError, resolve_font

WIDTH = 720
HEIGHT = 960
FPS = 30
FONT_SIZE = 48
LINE_SPACING = 76
CHAR_RAMP_SECONDS = 0.32   # each character emerges over this window
HOLD_TAIL_SECONDS = 0.6    # all text fully visible for at least this long
BLUR_START = 4.0           # gaussian radius at the start of a char's ramp
RISE_START = 10.0          # px the char rises during its ramp
SPRITE_PAD = 16            # tile margin so blur never clips

# Splits that end a display line (punctuation stays attached to its sentence)
SENTENCE_BREAKS = "，。！？；："


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def _load_category(categories: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    factory = Path(__file__).resolve().parents[1]
    for name in categories:
        try:
            return resolve_font(name, factory_root=factory).load(size)
        except FontResolutionError:
            continue
    return ImageFont.load_default()


def find_display_font(size: int) -> ImageFont.FreeTypeFont:
    """Expressive display face for the opening hook (calligraphy first)."""
    return _load_category(("chinese_handwriting", "chinese_title", "chinese_rounded", "chinese_body"), size)


def layout_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> list[str]:
    """One sentence per line; greedy-wrap only if a sentence overflows."""
    max_w = WIDTH - 120
    sentences: list[str] = []
    current = ""
    for ch in text:
        current += ch
        if ch in SENTENCE_BREAKS:
            sentences.append(current)
            current = ""
    if current:
        sentences.append(current)

    lines: list[str] = []
    for sentence in sentences:
        line = ""
        for ch in sentence:
            test = line + ch
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_w and line:
                lines.append(line)
                line = ch
            else:
                line = test
        if line:
            lines.append(line)
    return lines


def make_char_sprite(ch: str, font: ImageFont.FreeTypeFont) -> tuple[Image.Image, int, int]:
    """Pre-render one character on a padded RGBA tile.

    Returns (tile, offset_x, offset_y): paste the tile at (x + offset_x,
    y + offset_y) to land the glyph at the same place ImageDraw.text((x, y))
    would have put it.
    """
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    bbox = probe.textbbox((0, 0), ch, font=font)
    w = bbox[2] - bbox[0] + SPRITE_PAD * 2
    h = bbox[3] - bbox[1] + SPRITE_PAD * 2
    tile = Image.new("RGBA", (max(w, 1), max(h, 1)), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((SPRITE_PAD - bbox[0], SPRITE_PAD - bbox[1]), ch, font=font, fill=(255, 255, 255, 255))
    return tile, -SPRITE_PAD + bbox[0], -SPRITE_PAD + bbox[1]


def render_frame(
    chars: list[tuple[Image.Image, int, int, float, float, float]],
    t: float,
) -> Image.Image:
    """Black frame; each char emerges blur-to-sharp with a small rise."""
    frame = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 255))
    for tile, x, y, t0, char_w, char_h in chars:
        p = min(max((t - t0) / CHAR_RAMP_SECONDS, 0.0), 1.0)
        if p <= 0:
            continue
        eased = 1 - (1 - p) ** 3
        sprite = tile
        if eased < 1.0:
            radius = BLUR_START * (1 - eased)
            if radius > 0.3:
                sprite = sprite.filter(ImageFilter.GaussianBlur(radius))
            sprite = sprite.copy()
            alpha = sprite.split()[3].point(lambda v: round(v * eased))
            sprite.putalpha(alpha)
        rise = RISE_START * (1 - eased)
        frame.alpha_composite(sprite, (round(x), round(y - rise)))
    return frame.convert("RGB")


def generate_text_intro(text: str, duration: float, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    font = find_display_font(FONT_SIZE)

    measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    lines = layout_lines(measure, text, font)

    # Flatten to per-char sprites, centered per line, block centered ~42% height
    block_h = LINE_SPACING * len(lines)
    y0 = round(HEIGHT * 0.42) - block_h // 2
    chars: list[tuple[Image.Image, int, int, float, float, float]] = []
    n_chars = sum(len(line) for line in lines)
    reveal_span = max(duration - HOLD_TAIL_SECONDS - CHAR_RAMP_SECONDS, 0.5)
    idx = 0
    for li, line in enumerate(lines):
        bbox = measure.textbbox((0, 0), line, font=font)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2 - bbox[0]
        y = y0 + li * LINE_SPACING - bbox[1]
        for ch in line:
            t0 = reveal_span * idx / max(n_chars - 1, 1)
            tile, off_x, off_y = make_char_sprite(ch, font)
            chars.append((tile, x + off_x, y + off_y, t0, tile.width, tile.height))
            x += measure.textlength(ch, font=font)
            idx += 1

    total_frames = round(duration * FPS)
    frame_dir = output.parent / "text_intro_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    for i in range(total_frames):
        frame = render_frame(chars, i / FPS)
        frame.save(frame_dir / f"frame_{i:04d}.png")

    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(FPS),
        "-i", str(frame_dir / "frame_%04d.png"),
        "-frames:v", str(total_frames),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        str(output),
    ])
    for f in frame_dir.glob("frame_*.png"):
        f.unlink()
    frame_dir.rmdir()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate progressive character-reveal text intro")
    parser.add_argument("project", type=Path)
    parser.add_argument("--text", required=True, help="Hook sentence to reveal")
    parser.add_argument("--duration", type=float, required=True, help="Clip length in seconds")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    project = args.project.resolve()
    output = args.output or (project / "08_render_合成/text_intro/text_intro.mp4")
    generate_text_intro(args.text, args.duration, output)

    probe = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", str(output),
    ])
    print(json.dumps({
        "output": str(output),
        "text": args.text,
        "duration": round(float(probe.stdout.strip()), 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
