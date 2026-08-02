#!/usr/bin/env python3
"""Generate a book-cover carousel intro: blurred bg + glowing foreground cover,
each book shrinks from 60% to 50% width with accelerating cut rhythm
(0.70s → 0.45s), last book holds, then crossfade out.

Also emits carousel_timing.json (cut times + settle time) and, when the
project has 05_voice_人声/opening/thud.wav, a synchronized crescendo thud
track at 05_voice_人声/opening/carousel_thuds.wav.

Usage:
    python3 scripts/render_book_carousel.py <project> [--output PATH]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from book_video_factory.font_resolver import FontResolutionError, resolve_font

WIDTH = 720
HEIGHT = 960
FPS = 30
# Flashes accelerate: first book lingers, each next one cuts faster (rhythm
# ramp into the hold on the selected book).
FLASH_START_DURATION = 0.70  # seconds for the first book flash
FLASH_END_DURATION = 0.45    # seconds for the fastest (last) flash
LAST_BOOK_HOLD = 1.9       # extra hold for the selected book (title voice must land inside it)
XFADE_DURATION = 0.15      # quick crossfade between books
GLOW_PADDING = 14          # white glow padding around cover
GLOW_BLUR = 12             # gaussian blur radius for glow
START_WIDTH_RATIO = 0.60   # cover starts at 60% of frame width
END_WIDTH_RATIO = 0.50     # cover shrinks to 50%
THUD_GAIN_START = 0.45     # quietest cut thud
THUD_GAIN_END = 0.90       # loudest cut thud (crescendo into the settle thud)
SETTLE_THUD_GAIN = 1.0     # final thud when the selected book locks in


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def make_blurred_bg(cover: Image.Image) -> Image.Image:
    """Scale cover to fill frame, then heavy gaussian blur."""
    bg = cover.convert("RGB")
    # Scale to fill
    scale = max(WIDTH / bg.width, HEIGHT / bg.height)
    new_w, new_h = round(bg.width * scale), round(bg.height * scale)
    bg = bg.resize((new_w, new_h), Image.Resampling.LANCZOS)
    # Center crop
    left = (new_w - WIDTH) // 2
    top = (new_h - HEIGHT) // 2
    bg = bg.crop((left, top, left + WIDTH, top + HEIGHT))
    # Darken slightly + blur
    from PIL import ImageEnhance
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    return bg


def make_glow_cover(cover: Image.Image, target_w: int) -> Image.Image:
    """Scale cover to target_w, add white glow border."""
    ratio = target_w / cover.width
    target_h = round(cover.height * ratio)
    fg = cover.convert("RGBA").resize((target_w, target_h), Image.Resampling.LANCZOS)

    # White glow: paste on white canvas with padding, blur, then paste cover on top
    pad = GLOW_PADDING
    glow_w = target_w + pad * 2
    glow_h = target_h + pad * 2
    glow_canvas = Image.new("RGBA", (glow_w, glow_h), (0, 0, 0, 0))
    # White rounded rect
    draw = ImageDraw.Draw(glow_canvas)
    draw.rounded_rectangle(
        [pad // 2, pad // 2, glow_w - pad // 2, glow_h - pad // 2],
        radius=8,
        fill=(255, 255, 255, 220),
    )
    glow_canvas = glow_canvas.filter(ImageFilter.GaussianBlur(radius=GLOW_BLUR))
    # Paste cover on top
    glow_canvas.paste(fg, (pad, pad), fg)
    return glow_canvas


def generate_book_frames(
    cover: Image.Image,
    output_dir: Path,
    duration: float,
    is_last: bool = False,
    title: str | None = None,
    author: str | None = None,
    title_font: ImageFont.FreeTypeFont | None = None,
    author_font: ImageFont.FreeTypeFont | None = None,
) -> int:
    """Generate frames for one book: shrink from 60% to 50%, hold if last.

    The selected book's title and author are drawn on the hold frames, so the
    cover and the book name appear together the moment the carousel stops.
    """
    bg = make_blurred_bg(cover)
    total_frames = round(duration * FPS)
    shrink_frames = round((duration - (LAST_BOOK_HOLD if is_last else 0)) * FPS)

    for i in range(total_frames):
        if i < shrink_frames:
            t = i / max(shrink_frames - 1, 1)
        else:
            t = 1.0  # hold at end
        ratio = START_WIDTH_RATIO + (END_WIDTH_RATIO - START_WIDTH_RATIO) * t
        target_w = round(WIDTH * ratio)
        glow = make_glow_cover(cover, target_w)

        frame = bg.copy().convert("RGBA")
        x = (WIDTH - glow.width) // 2
        y = (HEIGHT - glow.height) // 2
        frame.paste(glow, (x, y), glow)
        if is_last and title and title_font and i >= shrink_frames:
            draw = ImageDraw.Draw(frame)
            text_y = min(y + glow.height + 40, HEIGHT - 150)
            draw.text(
                (WIDTH // 2 + 2, text_y + 2), title,
                font=title_font, fill=(0, 0, 0, 170), anchor="mm",
            )
            draw.text(
                (WIDTH // 2, text_y), title,
                font=title_font, fill=(255, 255, 255, 255), anchor="mm",
            )
            if author and author_font:
                draw.text(
                    (WIDTH // 2, text_y + 58), author,
                    font=author_font, fill=(226, 179, 92, 235), anchor="mm",
                )
        frame.convert("RGB").save(output_dir / f"frame_{i:04d}.png")

    return total_frames


def frames_to_video(frame_dir: Path, output: Path, frame_count: int) -> None:
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(FPS),
        "-i", str(frame_dir / "frame_%04d.png"),
        "-frames:v", str(frame_count),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        str(output),
    ])


def concat_with_xfade(clips: list[Path], output: Path) -> None:
    if len(clips) == 1:
        shutil.copy2(clips[0], output)
        return
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for c in clips:
        cmd.extend(["-i", str(c)])
    filters = []
    prev = "0:v"
    cumulative = 0.0
    for i in range(1, len(clips)):
        probe = run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nk=1:nw=1", str(clips[i - 1]),
        ])
        dur = float(probe.stdout.strip())
        if i == 1:
            cumulative = dur
        offset = round(cumulative - XFADE_DURATION, 3)
        out = f"xf{i}"
        filters.append(f"[{prev}][{i}:v]xfade=transition=fade:duration={XFADE_DURATION}:offset={offset:.3f}[{out}]")
        prev = out
        cumulative += dur - XFADE_DURATION
    filters.append(f"[{prev}]fps={FPS},format=yuv420p[out]")
    cmd.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-r", str(FPS), "-pix_fmt", "yuv420p", str(output)])
    run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate book carousel intro")
    parser.add_argument("project", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    project = args.project.resolve()
    carousel_dir = project / "01_research_资料搜集/sources/carousel"
    covers = sorted(carousel_dir.glob("*.jpg"))
    if not covers:
        raise FileNotFoundError(f"No cover images in {carousel_dir}")

    # This project's own book lands last, so the carousel resolves onto it.
    # Match against the project slug rather than one hardcoded book name, which
    # would silently mis-order the carousel for every other project.
    slug = json.loads((project / "project.json").read_text(encoding="utf-8"))["project_id"]
    selected = [cover for cover in covers if slug in cover.stem]
    others = [cover for cover in covers if slug not in cover.stem]
    if not selected:
        raise FileNotFoundError(
            f"No carousel cover matches this project's slug {slug!r} in {carousel_dir}. "
            f"Name this project's cover so it contains the slug (found: "
            f"{', '.join(cover.name for cover in covers)}), otherwise the carousel "
            "would resolve onto another book's cover."
        )
    ordered = others + selected  # this project's book last

    # Book title/author for the hold frame: cover and name appear together.
    book_title = slug
    book_author = ""
    script_path = project / "02_story_script_故事脚本/script.v2.bilingual.json"
    if script_path.is_file():
        book = json.loads(script_path.read_text(encoding="utf-8")).get("book", {})
        book_title = book.get("title") or slug
        book_author = book.get("author", "")
    display_title = f"《{book_title}》" if not book_title.startswith("《") else book_title
    display_author = f"{book_author} 著" if book_author else ""
    factory = Path(__file__).resolve().parents[1]
    try:
        title_font = resolve_font("chinese_title", factory_root=factory).load(58)
        author_font = resolve_font("chinese_title", factory_root=factory).load(30)
    except FontResolutionError:
        title_font = author_font = ImageFont.load_default()

    render_dir = project / "08_render_合成/carousel"
    render_dir.mkdir(parents=True, exist_ok=True)

    clips: list[Path] = []
    # Accelerating flash rhythm: first books linger, each next cut is faster.
    n_mid = len(ordered) - 1
    durations: list[float] = []
    for idx in range(len(ordered)):
        if n_mid <= 1:
            flash = FLASH_START_DURATION
        else:
            ramp = min(idx, n_mid - 1) / (n_mid - 1)
            flash = FLASH_START_DURATION + (FLASH_END_DURATION - FLASH_START_DURATION) * ramp
        is_last = idx == len(ordered) - 1
        durations.append(flash + (LAST_BOOK_HOLD if is_last else 0))

    for idx, cover_path in enumerate(ordered):
        is_last = idx == len(ordered) - 1
        duration = durations[idx]
        cover = Image.open(cover_path)
        frame_dir = render_dir / f"frames_{idx:02d}"
        frame_dir.mkdir(parents=True, exist_ok=True)
        print(f"  generating {cover_path.stem} ({duration:.1f}s, {'LAST' if is_last else 'mid'})...", flush=True)
        n = generate_book_frames(
            cover, frame_dir, duration, is_last=is_last,
            title=display_title if is_last else None,
            author=display_author if is_last else None,
            title_font=title_font, author_font=author_font,
        )
        clip = render_dir / f"book_{idx:02d}.mp4"
        frames_to_video(frame_dir, clip, n)
        clips.append(clip)
        # Clean up frames to save disk
        shutil.rmtree(frame_dir)

    output = args.output or (render_dir / "carousel.mp4")
    print(f"  concatenating {len(clips)} clips with xfade...", flush=True)
    concat_with_xfade(clips, output)

    # Probe final duration
    probe = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", str(output),
    ])
    duration = float(probe.stdout.strip())

    # Cut points: each book appears during its xfade; the thud hits mid-xfade.
    cut_times: list[float] = []
    cumulative = 0.0
    for idx in range(1, len(durations)):
        cumulative += durations[idx - 1] - XFADE_DURATION
        cut_times.append(round(cumulative + XFADE_DURATION / 2, 3))
    settle_time = round(duration - LAST_BOOK_HOLD, 3)

    timing = {
        "duration": round(duration, 3),
        "settle_time": settle_time,
        "cut_times": cut_times,
        "flash_durations": [round(d, 3) for d in durations],
        "title_embedded": True,
    }
    (render_dir / "carousel_timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # Synchronized crescendo thud track, if the project owns a thud sample.
    thud_src = project / "05_voice_人声/opening/thud.wav"
    thud_points = cut_times + [settle_time]
    gains = [
        THUD_GAIN_START + (THUD_GAIN_END - THUD_GAIN_START) * (i / max(len(cut_times) - 1, 1))
        for i in range(len(cut_times))
    ] + [SETTLE_THUD_GAIN]
    if thud_src.is_file():
        thuds_out = project / "05_voice_人声/opening/carousel_thuds.wav"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for _ in thud_points:
            cmd.extend(["-i", str(thud_src)])
        filters = []
        labels = []
        for i, (t, g) in enumerate(zip(thud_points, gains, strict=True)):
            delay_ms = round(t * 1000)
            filters.append(f"[{i}:a]adelay={delay_ms}|{delay_ms},volume={g:.2f}[t{i}]")
            labels.append(f"[t{i}]")
        filters.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,"
            f"atrim=0:{duration:.3f}[out]"
        )
        cmd.extend([
            "-filter_complex", ";".join(filters), "-map", "[out]",
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(thuds_out),
        ])
        run(cmd)
        print(f"  thud track: {thuds_out} ({len(thud_points)} hits, crescendo {gains[0]:.2f}->{gains[-1]:.2f})", flush=True)

    print(json.dumps({
        "output": str(output),
        "books": len(ordered),
        "duration": round(duration, 2),
        "settle_time": settle_time,
        "selected_book": ordered[-1].stem if ordered else None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
