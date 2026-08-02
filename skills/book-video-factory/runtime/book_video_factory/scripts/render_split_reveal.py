#!/usr/bin/env python3
"""Generate a knockout-text split-reveal opening.

Black overlay with two calligraphic characters punched through (revealing B-roll
behind), a hook sentence on the black area, then the overlay splits horizontally —
top half slides up, bottom slides down — revealing the full background.

Usage:
    python3 scripts/render_split_reveal.py <project> --text "沟通" \
        --hook "这是我听过，关于沟通最简单的一句话。" [--output PATH]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import _bootstrap  # noqa: F401
from book_video_factory.font_resolver import FontResolutionError, resolve_font

WIDTH = 720
HEIGHT = 960
FPS = 30
FADE_IN_SECONDS = 0.3
HOLD_SECONDS = 1.2       # hold with hook text visible
SPLIT_SECONDS = 0.9      # quick split; the drama comes from the stop, not the slide
TEXT_WIDTH_RATIO = 0.92  # knockout text spans 92% of frame width


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def _load_category(category: str, fallback: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a policy category, degrading to *fallback* then Pillow's default.

    Resolution is delegated to config/font_policy.json so this script carries no
    machine-specific font paths.
    """
    factory = Path(__file__).resolve().parents[1]
    for name in (category, fallback):
        try:
            return resolve_font(name, factory_root=factory).load(size)
        except FontResolutionError:
            continue
    return ImageFont.load_default()


def find_calligraphy_font(size: int) -> ImageFont.FreeTypeFont:
    """Bold, expressive calligraphy face for the knockout reveal."""
    return _load_category("chinese_handwriting", "chinese_title", size)


def find_body_font(size: int) -> ImageFont.FreeTypeFont:
    """Rounded display face for the hook sentence."""
    return _load_category("chinese_rounded", "chinese_body", size)


def build_text_mask(text: str) -> np.ndarray:
    """Pre-compute knockout text mask — sharp edges, 80% frame width."""
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(mask)

    # Find font size that makes text ~80% of frame width
    target_w = round(WIDTH * TEXT_WIDTH_RATIO)
    font_size = 200
    font = find_calligraphy_font(font_size)
    while font_size > 40:
        font = find_calligraphy_font(font_size)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        if tw <= target_w:
            break
        font_size -= 4

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (WIDTH - tw) // 2 - bbox[0]
    y = (HEIGHT - th) // 2 - bbox[1] - round(HEIGHT * 0.05)  # slightly above center
    draw.text((x, y), text, font=font, fill=255)
    # NO blur — sharp, clean edges
    return np.array(mask, dtype=np.int16)


def draw_hook_text(overlay: Image.Image, hook: str, hook_en: str = "") -> None:
    """Draw hook sentence as white artistic text above the knockout characters."""
    draw = ImageDraw.Draw(overlay)

    # Chinese hook — artistic calligraphy style, white
    zh_font = find_calligraphy_font(32)
    max_w = WIDTH - 100
    # Wrap Chinese
    zh_lines = []
    current = ""
    for ch in hook:
        test = current + ch
        bbox = draw.textbbox((0, 0), test, font=zh_font)
        if bbox[2] - bbox[0] > max_w:
            zh_lines.append(current)
            current = ch
        else:
            current = test
    if current:
        zh_lines.append(current)

    # Position above the knockout text (which is centered)
    y = round(HEIGHT * 0.18)
    for line in zh_lines:
        bbox = draw.textbbox((0, 0), line, font=zh_font)
        lw = bbox[2] - bbox[0]
        x = (WIDTH - lw) // 2
        # Super white text with glow effect (draw multiple passes)
        draw.text((x, y), line, font=zh_font, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(255, 255, 255, 200))
        draw.text((x, y), line, font=zh_font, fill=(255, 255, 255, 255))
        y += 46

    # English translation below — smaller, lighter
    if hook_en:
        y += 10
        en_font = find_body_font(18)
        # Wrap English
        en_words = hook_en.split()
        en_lines = []
        current = ""
        for word in en_words:
            test = (current + " " + word).strip()
            bbox = draw.textbbox((0, 0), test, font=en_font)
            if bbox[2] - bbox[0] > max_w:
                en_lines.append(current)
                current = word
            else:
                current = test
        if current:
            en_lines.append(current)
        for line in en_lines:
            bbox = draw.textbbox((0, 0), line, font=en_font)
            lw = bbox[2] - bbox[0]
            x = (WIDTH - lw) // 2
            draw.text((x, y), line, font=en_font, fill=(220, 215, 205, 200))
            y += 28


def make_knockout_frame(
    bg_np: np.ndarray,
    text_mask_np: np.ndarray,
    hook: str,
    hook_en: str = "",
    split_progress: float = 0.0,
    alpha: float = 1.0,
) -> Image.Image:
    """Vectorized: black overlay with calligraphic text knocked out + hook text."""
    base_alpha = round(255 * alpha)

    # Build overlay with hook text
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, base_alpha))
    if hook and split_progress <= 0:
        draw_hook_text(overlay, hook, hook_en)
    overlay_np = np.array(overlay)
    overlay_alpha_ch = overlay_np[:, :, 3].astype(np.int16)

    # Punch text out
    overlay_alpha_ch = np.clip(overlay_alpha_ch - text_mask_np, 0, 255).astype(np.uint8)

    if split_progress <= 0:
        a = overlay_alpha_ch.astype(np.float32) / 255.0
        a3 = a[:, :, np.newaxis]
        # Use overlay RGB (white hook text) instead of pure black
        overlay_rgb = overlay_np[:, :, :3].astype(np.float32)
        result = (bg_np.astype(np.float32) * (1 - a3) + overlay_rgb * a3).astype(np.uint8)
        return Image.fromarray(result)
    else:
        offset = round(split_progress * HEIGHT * 0.6)
        mid = HEIGHT // 2
        result = bg_np.copy()

        # Top half slides up
        top_src_s, top_src_e = offset, mid
        top_dst_s, top_dst_e = 0, mid - offset
        if top_dst_e > top_dst_s:
            a = overlay_alpha_ch[top_src_s:top_src_e, :].astype(np.float32) / 255.0
            a3 = a[:, :, np.newaxis]
            ov_rgb = overlay_np[top_src_s:top_src_e, :, :3].astype(np.float32)
            result[top_dst_s:top_dst_e, :] = (
                bg_np[top_dst_s:top_dst_e, :].astype(np.float32) * (1 - a3) + ov_rgb * a3
            ).astype(np.uint8)

        # Bottom half slides down
        bot_src_s, bot_src_e = mid, HEIGHT - offset
        bot_dst_s, bot_dst_e = mid + offset, HEIGHT
        if bot_dst_e > bot_dst_s:
            a = overlay_alpha_ch[bot_src_s:bot_src_e, :].astype(np.float32) / 255.0
            a3 = a[:, :, np.newaxis]
            ov_rgb = overlay_np[bot_src_s:bot_src_e, :, :3].astype(np.float32)
            result[bot_dst_s:bot_dst_e, :] = (
                bg_np[bot_dst_s:bot_dst_e, :].astype(np.float32) * (1 - a3) + ov_rgb * a3
            ).astype(np.uint8)

        return Image.fromarray(result)


def generate_split_reveal(bg_clip: Path, text: str, hook: str, hook_en: str, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    total_seconds = FADE_IN_SECONDS + HOLD_SECONDS + SPLIT_SECONDS
    total_frames = round(total_seconds * FPS)

    tmp_frame = output.with_suffix(".bg.png")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(bg_clip), "-frames:v", "1",
        "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}",
        str(tmp_frame),
    ])
    bg_np = np.array(Image.open(tmp_frame).convert("RGB"))
    text_mask_np = build_text_mask(text)

    frame_dir = output.parent / "split_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)

    fade_frames = round(FADE_IN_SECONDS * FPS)
    hold_frames = round(HOLD_SECONDS * FPS)
    split_frames = round(SPLIT_SECONDS * FPS)

    for i in range(total_frames):
        if i < fade_frames:
            alpha = i / max(fade_frames - 1, 1)
            frame = make_knockout_frame(bg_np, text_mask_np, hook, hook_en, 0.0, alpha)
        elif i < fade_frames + hold_frames:
            frame = make_knockout_frame(bg_np, text_mask_np, hook, hook_en, 0.0, 1.0)
        else:
            t = (i - fade_frames - hold_frames) / max(split_frames - 1, 1)
            progress = 1 - (1 - t) ** 3
            frame = make_knockout_frame(bg_np, text_mask_np, "", "", progress, 1.0)
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
    shutil.rmtree(frame_dir)
    tmp_frame.unlink(missing_ok=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate knockout text split-reveal opening")
    parser.add_argument("project", type=Path)
    parser.add_argument("--text", required=True, help="Two characters to knockout, e.g. 沟通")
    parser.add_argument("--hook", default="", help="Hook sentence (Chinese) shown on black overlay")
    parser.add_argument("--hook-en", default="", help="English translation of hook sentence")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    project = args.project.resolve()
    broll_dir = project / "03_images_生成图片/broll-approved/normalized"
    clips = sorted(broll_dir.glob("broll-*.mp4"))
    if not clips:
        raise FileNotFoundError(f"No B-roll clips in {broll_dir}")

    output = args.output or (project / "08_render_合成/split_reveal/split_reveal.mp4")
    generate_split_reveal(clips[0], args.text, args.hook, args.hook_en, output)

    probe = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", str(output),
    ])
    print(json.dumps({
        "output": str(output),
        "text": args.text,
        "hook": args.hook,
        "duration": round(float(probe.stdout.strip()), 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
