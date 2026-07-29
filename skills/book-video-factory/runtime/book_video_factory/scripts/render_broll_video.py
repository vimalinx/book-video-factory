#!/usr/bin/env python3
"""Render a book video with stock B-roll backgrounds, text-safe frame,
large bilingual captions, and smooth crossfade transitions.

Default mode downloads commercial-capable stock clips (Coverr first, then
Pexels/Pixabay when API keys exist) using config/broll_scene_queries.json.
Procedural nature gradients remain available only via --mode nature.

Usage:
    python3 scripts/render_broll_video.py <project> [--mode stock|nature] [--output DIR] [--xfade 0.8]
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
from PIL import Image, ImageDraw, ImageFont

import build_final_video_v2 as v2
from book_video_factory.backgrounds import (
    NATURE_PALETTES,
    concat_with_crossfades,
    generate_background,
    render_text_safe_frame,
)
from book_video_factory.broll import BrollError, prepare_scene_stock
from book_video_factory.font_resolver import FontResolutionError, resolve_font
from book_video_factory.audio_sequencer import AudioClip, sequence_clips, mix_clips, get_voice_end
from book_video_factory.scene_contract import V4_SCENE_LINE_CONTRACT, V4_TIMELINE_SCENES
from book_video_factory.typography import (
    centered_text_x,
    fit_book_title,
    fit_single_line_font_size,
    text_box,
)


FACTORY = Path(__file__).resolve().parents[1]
STYLE_PATH = FACTORY / "config/video_style_v2.json"
FPS = 30
WIDTH = 720
HEIGHT = 960
OUTRO_SECONDS = 2.5
ROUNDED_FONT_CATEGORY = "chinese_rounded"
INTRO_FADE_SECONDS = 1.5
VACUUM_SECONDS = 0.3       # dead-air beat between the carousel settle and the title voice

# Scene-to-palette-index mapping for procedural backgrounds
SCENE_PALETTE_INDEX: dict[str, int] = {
    "HOOK": 0,        # morning_mist
    "BOOK": 2,        # golden_hour (brighter for the reveal)
    "THESIS": 1,      # forest_canopy
    "RELATION": 3,    # ocean_calm
    "SUPPORT": 7,     # spring_meadow
    "BOUNDARIES": 5,  # autumn_warm
    "FIRST_STEP": 1,  # forest_canopy
    "QUESTION": 6,    # rainy_window
    "SELF_CARE": 0,   # morning_mist
    "LOVE": 4,        # lavender_dusk
    "RAIN": 6,        # rainy_window
    "DAWN": 2,        # golden_hour
}


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise RuntimeError(
            f"command failed ({proc.returncode}): {command[0]} ...\n" + "\n".join(tail)
        )
    return proc


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# ASR alignment (adapted from build_batch_video_v3.py)
# ---------------------------------------------------------------------------

def proportional_lines(script: dict[str, Any], asr: dict[str, Any]) -> list[v2.TimedLine]:
    """Last-resort timing map when ASR wording is too lossy to align safely."""
    duration = max(
        (float(segment.get("end", 0.0)) for segment in asr.get("segments", [])),
        default=0.0,
    )
    if duration <= 0:
        raise RuntimeError("ASR does not contain a usable duration")
    weights = [max(1, len(v2.normalize(line["zh"]))) for line in script["lines"]]
    total_weight = sum(weights)
    cursor = 0.0
    aligned: list[v2.TimedLine] = []
    for line, weight in zip(script["lines"], weights, strict=True):
        end = cursor + duration * weight / total_weight
        aligned.append(v2.TimedLine(
            line_id=line["id"], role=line["role"], zh=line["zh"], en=line["en"],
            start=round(cursor, 3), end=round(end, 3),
        ))
        cursor = end
    return aligned



# Traditional → Simplified normalization for ASR alignment.
#
# Whisper sometimes transcribes Mandarin audio with traditional characters, so
# captions must be compared against a normalized transcript. The mapping used to
# be a hand-built table containing only the characters that appeared in one
# project's script, which silently failed to normalize any other book's text.
#
# Resolution order:
#   1. the opencc package, when installed — authoritative and complete;
#   2. config/t2s_charmap.json, a general 3757-pair single-character table
#      generated from OpenCC over the CJK blocks, so there is no hard dependency.
#
# Either way this is only an alignment aid: align_lines falls back to fuzzy
# ordered matching and then proportional distribution, so an unmapped character
# degrades placement accuracy rather than breaking the render.
def _load_t2s() -> "callable[[str], str]":
    try:
        import opencc  # type: ignore[import-not-found]

        converter = opencc.OpenCC("t2s")
        return converter.convert
    except Exception:  # noqa: BLE001 - any opencc problem falls back to the table
        pass

    table_path = FACTORY / "config" / "t2s_charmap.json"
    try:
        payload = read_json(table_path)
        traditional = str(payload["traditional"])
        simplified = str(payload["simplified"])
        if len(traditional) != len(simplified):
            raise ValueError("t2s charmap halves differ in length")
        table = str.maketrans(traditional, simplified)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        # No normalization available; fuzzy alignment still handles the text.
        return lambda text: text
    return lambda text: text.translate(table)


_t2s = _load_t2s()

def align_lines(script: dict[str, Any], asr: dict[str, Any]) -> list[v2.TimedLine]:
    """Align captions to Whisper, tolerating simplified/traditional output.

    Falls back to fuzzy ordered matching, then proportional distribution.
    No montage gap is inserted in broll mode.
    """
    # Try exact matching first (v2 logic without montage offset)
    words = [word for segment in asr["segments"] for word in segment.get("words", [])]
    characters: list[str] = []
    character_to_word: list[int] = []
    for index in range(len(words)):
        for character in v2.normalized_asr_word(words, index):
            characters.append(character)
            character_to_word.append(index)
    transcript = _t2s("".join(characters))

    # Attempt exact substring match
    cursor = 0
    aligned: list[v2.TimedLine] = []
    exact_ok = True
    for line in script["lines"]:
        target = v2.normalize(line["zh"])
        position = transcript.find(target, cursor)
        if position < 0:
            exact_ok = False
            break
        start_word = words[character_to_word[position]]
        end_word = words[character_to_word[position + len(target) - 1]]
        aligned.append(v2.TimedLine(
            line_id=line["id"], role=line["role"], zh=line["zh"], en=line["en"],
            start=round(float(start_word["start"]), 3),
            end=round(float(end_word["end"]), 3),
        ))
        cursor = position + len(target)

    if exact_ok:
        return aligned

    # Fuzzy ordered matching
    cursor = 0
    aligned = []
    for line in script["lines"]:
        target = v2.normalize(line["zh"])
        best: tuple[float, int, int] | None = None
        min_length = max(1, len(target) - 7)
        max_length = len(target) + 9
        for start in range(cursor, min(len(transcript), cursor + 40) + 1):
            for length in range(min_length, max_length + 1):
                end = start + length
                if end > len(transcript):
                    break
                ratio = difflib.SequenceMatcher(a=target, b=transcript[start:end], autojunk=False).ratio()
                score = ratio - abs(length - len(target)) * 0.006
                if best is None or score > best[0]:
                    best = (score, start, end)
        if best is None or best[0] < 0.45:
            return proportional_lines(script, asr)
        _, start, end = best
        start_word = words[character_to_word[start]]
        end_word = words[character_to_word[end - 1]]
        aligned.append(v2.TimedLine(
            line_id=line["id"], role=line["role"], zh=line["zh"], en=line["en"],
            start=round(float(start_word["start"]), 3),
            end=round(float(end_word["end"]), 3),
        ))
        cursor = end
    return aligned


# ---------------------------------------------------------------------------
# Title overlay (book title + author for intro)
# ---------------------------------------------------------------------------

def render_intro_title_layer(
    style: dict[str, Any], title: str, author: str, width: int, height: int
) -> Image.Image:
    """Render book title + author as a transparent overlay for the intro."""
    scale = width / WIDTH
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    config = style.get("title_layout", {})
    safe_margin = round(int(config.get("safe_margin_x_px", 56)) * scale)
    max_width = width - safe_margin * 2
    stroke = max(1, round(3 * scale))

    title_loader = lambda size: v2.font(style, "title", size)
    layout = fit_book_title(
        draw, title, title_loader,
        max_width=max_width,
        max_font_size=round(int(config.get("max_font_size_px", 70)) * scale),
        min_font_size=round(int(config.get("min_font_size_px", 34)) * scale),
        stroke_width=stroke,
    )
    title_font = title_loader(layout.font_size)
    # Center vertically in the text-safe area
    top_key = "top_px_tall" if height / width >= 2 else "top_px"
    y = round(int(config.get(top_key, 104 if height / width >= 2 else 62)) * scale)
    # Shift down to center in frame for broll
    y = round(height * 0.38)
    line_gap = round(int(config.get("line_gap_px", 8)) * scale)
    for label in layout.lines:
        box = text_box(draw, label, title_font, stroke)
        draw.text(
            (centered_text_x(draw, label, title_font, width, stroke), y - box[1]),
            label, font=title_font,
            fill=(250, 247, 240, 255),
            stroke_width=stroke, stroke_fill=(0, 0, 0, 230),
        )
        y += box[3] - box[1] + line_gap

    # Accent rule
    rule_y = y - line_gap + round(int(config.get("rule_gap_px", 14)) * scale)
    draw.rectangle(
        ((width - 88 * scale) / 2, rule_y, (width + 88 * scale) / 2, rule_y + max(4, round(5 * scale))),
        fill=(183, 96, 49, 245),
    )

    # Author byline
    byline = f"{author}／著"
    author_stroke = max(1, round(2 * scale))
    author_loader = lambda size: v2.chinese_font(style, size, title_weight=True)
    author_size = fit_single_line_font_size(
        draw, byline, author_loader,
        max_width=max_width,
        max_font_size=round(int(config.get("author_max_font_size_px", 31)) * scale),
        min_font_size=round(int(config.get("author_min_font_size_px", 20)) * scale),
        stroke_width=author_stroke,
    )
    author_font = author_loader(author_size)
    author_y = rule_y + round(int(config.get("author_gap_px", 20)) * scale)
    author_box = text_box(draw, byline, author_font, author_stroke)
    draw.text(
        (centered_text_x(draw, byline, author_font, width, author_stroke), author_y - author_box[1]),
        byline, font=author_font,
        fill=(234, 229, 220, 250),
        stroke_width=author_stroke, stroke_fill=(0, 0, 0, 215),
    )
    return canvas


def render_outro_layer(
    style: dict[str, Any], book_title: str, width: int, height: int
) -> Image.Image:
    """Render an outro card: book title + channel branding + thank you."""
    scale = width / WIDTH
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Semi-transparent dark overlay for readability
    draw.rectangle([0, 0, width, height], fill=(0, 0, 0, 120))

    # Book title (rounded font)
    title_size = round(36 * scale)
    try:
        title_font = resolve_font(
            ROUNDED_FONT_CATEGORY, factory_root=FACTORY
        ).load(title_size)
    except FontResolutionError:
        title_font = v2.chinese_font(style, title_size, title_weight=True)
    label = f"《{book_title}》"
    tw, th = v2.text_size(draw, label, title_font)
    title_y = round(height * 0.35)
    draw.text(
        ((width - tw) / 2, title_y), label, font=title_font,
        fill=(255, 255, 255, 255), stroke_width=max(1, round(scale)), stroke_fill=(0, 0, 0, 180),
    )

    # Thank you line
    thanks_size = round(24 * scale)
    try:
        thanks_font = resolve_font(
            ROUNDED_FONT_CATEGORY, factory_root=FACTORY
        ).load(thanks_size)
    except FontResolutionError:
        thanks_font = v2.chinese_font(style, thanks_size)
    thanks = "感谢观看 · 关注我读更多好书"
    tkw, _ = v2.text_size(draw, thanks, thanks_font)
    thanks_y = title_y + th + round(40 * scale)
    draw.text(
        ((width - tkw) / 2, thanks_y), thanks, font=thanks_font,
        fill=(220, 215, 205, 230), stroke_width=max(1, round(scale)), stroke_fill=(0, 0, 0, 150),
    )

    # Channel branding at bottom
    brand_size = round(16 * scale)
    brand_font = v2.font(style, "english", brand_size)
    brand = "JAXXMIND · BOOK NOTES"
    bw, _ = v2.text_size(draw, brand, brand_font)
    brand_y = round(height * 0.82)
    draw.text(
        ((width - bw) / 2, brand_y), brand, font=brand_font,
        fill=(200, 195, 185, 180), stroke_width=max(1, round(scale)), stroke_fill=(0, 0, 0, 120),
    )
    return canvas


def render_title_reveal_clip(
    style: dict[str, Any],
    book_title: str,
    book_author: str,
    broll_clip: Path,
    output: Path,
    duration: float = 3.0,
) -> Path:
    """Darkened B-roll with book title appearing INSTANTLY (no fade)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    overlay_path = output.with_suffix(".title.png")
    render_intro_title_layer(style, book_title, book_author, WIDTH, HEIGHT).save(overlay_path)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(broll_clip),
        "-loop", "1", "-framerate", str(FPS), "-i", str(overlay_path),
        "-filter_complex",
        f"[0:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
        f"eq=brightness=-0.15:saturation=0.6,"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
        f"fps={FPS}[bg];"
        f"[1:v]format=rgba[title];"
        f"[bg][title]overlay=0:0,"
        f"fade=t=out:st={duration - 0.5:.3f}:d=0.5,"
        f"format=yuv420p[out]",
        "-map", "[out]", "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-t", f"{duration:.3f}",
        str(output),
    ])
    return output


# ---------------------------------------------------------------------------
# Segment planning
# ---------------------------------------------------------------------------

def plan_segments(
    lines: list[v2.TimedLine],
    voice_duration: float,
    total_duration: float,
) -> list[dict[str, Any]]:
    """Plan background segments from the scene timeline.

    Each scene in V4_TIMELINE_SCENES gets one background segment.
    Returns list of {"scene_id", "scene_name", "palette_index", "start", "end", "duration"}.
    """
    line_by_id = {line.line_id: line for line in lines}
    segments: list[dict[str, Any]] = []

    for scene_name, scene_id in V4_TIMELINE_SCENES:
        line_ids = V4_SCENE_LINE_CONTRACT[scene_id]
        scene_lines = [line_by_id[lid] for lid in line_ids if lid in line_by_id]
        if not scene_lines:
            continue
        start = min(l.start for l in scene_lines)
        end = max(l.end for l in scene_lines)
        segments.append({
            "scene_id": scene_id,
            "scene_name": scene_name,
            "palette_index": SCENE_PALETTE_INDEX.get(scene_name, 0),
            "start": start,
            "end": end,
            "duration": end - start,
        })

    # Extend first segment to cover intro (from 0 to first line start)
    if segments:
        segments[0]["start"] = 0.0
        segments[0]["duration"] = segments[0]["end"]

    # Extend last segment to cover outro
    if segments:
        segments[-1]["end"] = total_duration
        segments[-1]["duration"] = total_duration - segments[-1]["start"]

    # Fill any gaps between segments
    for i in range(1, len(segments)):
        prev_end = segments[i - 1]["end"]
        if segments[i]["start"] < prev_end:
            segments[i]["start"] = prev_end
            segments[i]["duration"] = segments[i]["end"] - segments[i]["start"]

    return segments


# ---------------------------------------------------------------------------
# Audio mixing (adapted from v2.render_variant, no stinger/montage)
# ---------------------------------------------------------------------------

def mix_audio(
    base_video: Path,
    overlays: list[dict[str, Any]],
    voice: Path,
    bgm: Path,
    total_duration: float,
    output: Path,
    style: dict[str, Any],
    voice_delay: float = 0.0,
    opening_voice: Path | None = None,
    vacuum: tuple[float, float] | None = None,
) -> None:
    """Composite video with overlays and mix voice + BGM with sidechain ducking.

    *vacuum* is an optional (start, end) window where the BGM ducks to near
    silence — the dead-air beat right before the book title is read aloud.
    """
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(base_video)]

    # Inputs 1..N: caption/title overlays
    for overlay in overlays:
        command.extend(["-loop", "1", "-framerate", str(FPS), "-i", str(overlay["path"])])

    voice_index = len(overlays) + 1
    bgm_index = voice_index + 1
    command.extend(["-i", str(voice), "-i", str(bgm)])
    opening_index = None
    if opening_voice and opening_voice.is_file():
        opening_index = bgm_index + 1
        command.extend(["-i", str(opening_voice)])

    filters: list[str] = []

    # Base video with intro fade-in from black
    filters.append(f"[0:v]fade=t=in:st=0:d={INTRO_FADE_SECONDS:.3f}[vbase]")

    # Timed overlays (captions + title + outro)
    current = "vbase"
    for index, overlay in enumerate(overlays, start=1):
        output_label = f"ov{index}"
        filters.append(
            f"[{current}][{index}:v]overlay=0:0:enable='between(t,{overlay['start']:.3f},{overlay['end']:.3f})'[{output_label}]"
        )
        current = output_label

    # Outro fade to black (last OUTRO_SECONDS), then shrink onto a subtly
    # top-lit gradient mat — a floating "perspective frame" look. (The
    # `perspective` filter is broken on this ffmpeg build, so the depth cue
    # comes from the gradient instead of a keystone.)
    fade_start = total_duration - OUTRO_SECONDS
    filters.append(
        f"[{current}]fade=t=out:st={fade_start:.3f}:d={OUTRO_SECONDS:.3f},"
        f"scale=634:844[card]"
    )
    filters.append(
        f"gradients=size={WIDTH}x{HEIGHT}:c0=0x181818:c1=0x000000:"
        f"x0=0:y0=0:x1=0:y1={HEIGHT}:duration={total_duration:.3f}:speed=0[bg]"
    )
    filters.append("[bg][card]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]")

    # Audio: opening voice (from 0) + main voice (delayed) + BGM (from 0) with sidechain
    audio_cfg = style["audio"]
    fade_out = max(0.0, total_duration - OUTRO_SECONDS)

    delay_ms = round(voice_delay * 1000)

    # BGM from frame 0, continuous through entire video. When a vacuum window
    # is given, the bed ducks to near silence for that beat (60ms ramps).
    vacuum_filter = ""
    if vacuum is not None:
        v0, v1 = vacuum
        r = 0.06
        vacuum_filter = (
            f",volume='if(lt(t,{v0:.3f}),1,"
            f"if(lt(t,{v0 + r:.3f}),1-0.92*(t-{v0:.3f})/{r},"
            f"if(lt(t,{v1:.3f}),0.08,"
            f"if(lt(t,{v1 + r:.3f}),0.08+0.92*(t-{v1:.3f})/{r},1))))':eval=frame"
        )
    filters.append(
        f"[{bgm_index}:a]atrim=0:{total_duration:.3f},asetpts=PTS-STARTPTS,"
        f"loudnorm=I={audio_cfg['bgm_target_lufs']}:LRA=8:TP=-2,"
        f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out:.3f}:d={OUTRO_SECONDS:.3f}{vacuum_filter}[bed]"
    )

    # Main voice: delayed to match intro offset
    filters.append(
        f"[{voice_index}:a]adelay={delay_ms}|{delay_ms},apad=pad_dur={OUTRO_SECONDS:.3f},"
        f"atrim=0:{total_duration:.3f},"
        f"afade=t=in:st={voice_delay:.3f}:d=0.3,afade=t=out:st={total_duration - 0.8:.3f}:d=0.8[voice_main]"
    )

    # Opening voice (hook + whoosh + announce) plays from frame 0
    if opening_index is not None:
        filters.append(
            f"[{opening_index}:a]apad=pad_dur=0,atrim=0:{total_duration:.3f}[voice_opening]"
        )
        # Sidechain BGM against combined voice
        filters.append(
            f"[voice_main][voice_opening]amix=inputs=2:duration=first:normalize=0,"
            f"asplit=2[voice_sc][voice_mix]"
        )
    else:
        filters.append("[voice_main]asplit=2[voice_sc][voice_mix]")

    filters.extend([
        f"[bed][voice_sc]sidechaincompress=threshold=0.035:ratio=4:attack=10:release=320[ducked]",
        f"[ducked][voice_mix]amix=inputs=2:duration=first:normalize=0,"
        f"loudnorm=I={audio_cfg['final_target_lufs']}:LRA=8:TP={audio_cfg['true_peak_dbfs']},"
        f"atrim=0:{total_duration:.3f}[aout]",
    ])

    output.parent.mkdir(parents=True, exist_ok=True)
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output),
    ])
    run(command)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_background_clips(
    *,
    mode: str,
    project: Path,
    segments: list[dict[str, Any]],
    bg_dir: Path,
    force_fetch: bool,
    xfade: float,
) -> tuple[list[Path], list[dict[str, Any]], str]:
    """Return (clip paths, segment metadata for manifest, render_mode)."""
    bg_dir.mkdir(parents=True, exist_ok=True)

    # xfade shortens the concat by xfade*(n-1); extend every non-final clip so the
    # finished bed still matches the planned timeline duration.
    def clip_duration(index: int, duration: float) -> float:
        if len(segments) <= 1 or index >= len(segments) - 1:
            return duration
        return duration + xfade

    if mode == "nature":
        bg_paths: list[Path] = []
        meta: list[dict[str, Any]] = []
        for i, seg in enumerate(segments):
            bg_path = bg_dir / f"bg_{i:03d}_{seg['scene_name']}.mp4"
            generate_background(
                bg_path,
                clip_duration(i, seg["duration"]),
                palette_index=seg["palette_index"],
                width=WIDTH,
                height=HEIGHT,
                fps=FPS,
            )
            bg_paths.append(bg_path)
            meta.append(
                {
                    "scene_id": seg["scene_id"],
                    "scene_name": seg["scene_name"],
                    "source": "procedural_nature",
                    "palette": NATURE_PALETTES[seg["palette_index"]]["name"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                }
            )
        return bg_paths, meta, "broll_nature"

    if mode == "local":
        local_dir = project / "03_images_生成图片/broll-approved/normalized"
        local_clips = sorted(local_dir.glob("broll-*.mp4"))
        if not local_clips:
            raise BrollError(
                f"No normalized B-roll clips found in {local_dir}. "
                "Download and normalize clips first (720x960, 30fps, silent)."
            )
        bg_paths = []
        meta = []
        for i, seg in enumerate(segments):
            clip = local_clips[i % len(local_clips)]
            dur = clip_duration(i, seg["duration"])
            # Trim or loop the clip to match segment duration
            fitted = bg_dir / f"local_{i:03d}_{seg['scene_name']}.mp4"
            probe_dur = float(run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nk=1:nw=1", str(clip)],
                capture=True,
            ).stdout.strip())
            if probe_dur >= dur:
                run([
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(clip), "-t", f"{dur:.3f}",
                    "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p",
                    "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-r", str(FPS), str(fitted),
                ])
            else:
                # Loop clip to fill duration
                loops = int(dur / probe_dur) + 1
                run([
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-stream_loop", str(loops), "-i", str(clip),
                    "-t", f"{dur:.3f}",
                    "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p",
                    "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-r", str(FPS), str(fitted),
                ])
            bg_paths.append(fitted)
            meta.append(
                {
                    "scene_id": seg["scene_id"],
                    "scene_name": seg["scene_name"],
                    "source": "local_broll",
                    "clip": clip.name,
                    "provider": "pexels",
                    "license": "pexels-license-free-commercial-use",
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                }
            )
        return bg_paths, meta, "broll_local"

    stock_segments = [
        {
            **seg,
            "duration": clip_duration(i, float(seg["duration"])),
        }
        for i, seg in enumerate(segments)
    ]
    assignment = prepare_scene_stock(project, stock_segments, force=force_fetch)
    by_scene = {item["scene_name"]: item for item in assignment.get("scenes") or []}
    bg_paths = []
    meta = []
    for seg in segments:
        record = by_scene.get(seg["scene_name"])
        if not record:
            raise BrollError(f"Missing stock assignment for scene {seg['scene_name']}")
        fitted = Path(str(record["fitted_path"]))
        if not fitted.is_file():
            raise BrollError(f"Fitted stock clip missing: {fitted}")
        bg_paths.append(fitted)
        meta.append(
            {
                "scene_id": seg["scene_id"],
                "scene_name": seg["scene_name"],
                "source": "stock",
                "provider": record.get("provider"),
                "clip_id": record.get("clip_id"),
                "source_page": record.get("source_page"),
                "search_query": record.get("search_query"),
                "license": record.get("license"),
                "raw_path": record.get("raw_path"),
                "fitted_path": str(fitted),
                "start": seg["start"],
                "end": seg["end"],
                "duration": seg["duration"],
            }
        )
    return bg_paths, meta, "broll_stock"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a broll book video with stock footage (default) or procedural nature"
    )
    parser.add_argument("project", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--xfade", type=float, default=0.8)
    parser.add_argument(
        "--mode",
        choices=["stock", "nature", "local"],
        default="local",
        help="local = pre-downloaded clips in broll-approved/normalized; stock = API download; nature = procedural gradients",
    )
    parser.add_argument(
        "--force-fetch",
        action="store_true",
        help="Re-download stock clips even if scene-assignment already exists",
    )
    args = parser.parse_args()

    project = args.project.resolve()
    style = read_json(STYLE_PATH)
    script = read_json(project / "02_story_script_故事脚本/script.v2.bilingual.json")

    # Load voice — single explicit preference list, both files must exist:
    #   1. cosyvoice master + ASR (current production voice)
    #   2. per-sentence ChatTTS with known timeline
    #   3. ASR fallback chain (qwen3 > trimmed > voxcpm masters)
    def asr_lines(voice_wav: Path, asr_json: Path) -> list[v2.TimedLine]:
        asr = read_json(asr_json)
        script_for_align = dict(script)
        script_for_align["lines"] = [l for l in script["lines"] if l["id"] not in ("V01", "V02", "V03")]
        return align_lines(script_for_align, asr)

    voice_dir = project / "05_voice_人声"
    timeline_path = voice_dir / "chattts_timeline.json"
    lines: list[v2.TimedLine] = []

    voice = voice_dir / "v3-cosy-master.wav"
    asr_path = voice_dir / "asr-v3-cosy/v3-cosy-master.json"
    chattts_voice = voice_dir / "v3-chattts-master.wav"

    if voice.is_file() and asr_path.is_file():
        lines = asr_lines(voice, asr_path)
    elif timeline_path.is_file() and chattts_voice.is_file():
        voice = chattts_voice
        # Direct timeline: each sentence was generated individually, durations are exact
        tl = read_json(timeline_path)
        for item in tl["lines"]:
            lines.append(v2.TimedLine(
                line_id=item["id"], role=item["role"],
                zh=item["zh"], en=item["en"],
                start=item["start"], end=item["end"],
            ))
    else:
        # Fallback: ASR-based alignment
        for candidate, asr_candidate in (
            ("v3-qwen3-master.wav", "asr-v3-qwen3/v3-qwen3-master.json"),
            ("v3-trimmed-master.wav", "asr-v3-trimmed/v3-trimmed-master.json"),
            ("v3-b-locked-master-paused.wav", "asr-v3-paused/v3-b-locked-master-paused.json"),
            ("v3-b-locked-master.wav", "asr-v3/v3-b-locked-master.json"),
        ):
            if (voice_dir / candidate).is_file() and (voice_dir / asr_candidate).is_file():
                voice = voice_dir / candidate
                asr_path = voice_dir / asr_candidate
                break
        else:
            raise FileNotFoundError("Voice WAV and ASR/timeline must exist before rendering")
        lines = asr_lines(voice, asr_path)

    print(f"  voice: {voice.relative_to(project)} ({len(lines)} timed lines)")

    if not voice.is_file():
        raise FileNotFoundError("No voice WAV found")

    voice_duration = float(run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(voice)],
        capture=True,
    ).stdout.strip())
    content_duration = round(voice_duration + OUTRO_SECONDS, 3)

    # Plan background segments
    segments = plan_segments(lines, voice_duration, content_duration)

    render_dir = project / "08_render_合成/broll"
    bg_dir = render_dir / "backgrounds"
    bg_paths, segment_meta, render_mode = build_background_clips(
        mode=args.mode,
        project=project,
        segments=segments,
        bg_dir=bg_dir,
        force_fetch=args.force_fetch,
        xfade=args.xfade,
    )

    # Concatenate B-roll backgrounds with crossfades
    bg_concat = render_dir / "background_concat.mp4"
    concat_with_crossfades(bg_paths, bg_concat, xfade_duration=args.xfade, fps=FPS)

    # --- Build intro: split reveal → carousel → title reveal ---
    intro_clips: list[Path] = []
    intro_offset = 0.0

    # 1. Text intro: hook caption revealed character by character on black
    #    (no knockout text, no mask). Falls back to the legacy split reveal.
    split_reveal_path = project / "08_render_合成/text_intro/text_intro.mp4"
    if not split_reveal_path.is_file():
        split_reveal_path = project / "08_render_合成/split_reveal/split_reveal.mp4"
    if split_reveal_path.is_file():
        intro_clips.append(split_reveal_path)
        sr_dur = float(run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(split_reveal_path)],
            capture=True,
        ).stdout.strip())
        intro_offset += sr_dur

    # 2. Fast carousel: 0.5s per book flash
    carousel_path = project / "08_render_合成/carousel/carousel.mp4"
    if carousel_path.is_file():
        intro_clips.append(carousel_path)
        carousel_dur = float(run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(carousel_path)],
            capture=True,
        ).stdout.strip())
        intro_offset += carousel_dur

    # 3. Title reveal: darkened B-roll + book title fading in.
    # Skipped when the carousel already embeds the title on its hold frame —
    # the cover and the book name then appear together at the settle.
    carousel_timing: dict[str, Any] = {}
    carousel_timing_path = project / "08_render_合成/carousel/carousel_timing.json"
    if carousel_timing_path.is_file():
        carousel_timing = json.loads(carousel_timing_path.read_text(encoding="utf-8"))
    book_title = script["book"]["title"]
    book_author = script["book"]["author"]
    if not carousel_timing.get("title_embedded"):
        title_reveal_dur = 3.0
        title_reveal_path = render_dir / "title_reveal.mp4"
        render_title_reveal_clip(style, book_title, book_author, bg_concat, title_reveal_path, duration=title_reveal_dur)
        intro_clips.append(title_reveal_path)
        intro_offset += title_reveal_dur

    # Concat: intro clips + B-roll content
    all_video_clips = intro_clips + [bg_concat]
    full_video = render_dir / "full_video.mp4"
    if len(all_video_clips) > 1:
        concat_with_crossfades(all_video_clips, full_video, xfade_duration=0.6, fps=FPS)
        # Adjust intro_offset for xfade overlaps
        intro_offset -= 0.6 * (len(all_video_clips) - 1)
    else:
        shutil.copy2(all_video_clips[0], full_video)

    total_duration = round(intro_offset + content_duration, 3)

    # --- Audio sequencing: resolve overlaps between opening clips ---
    opening_dir = project / "05_voice_人声/opening"
    opening_clips: list[AudioClip] = []
    sr_start = sr_dur if split_reveal_path.is_file() else 0.0
    carousel_start = sr_start + (carousel_dur if carousel_path.is_file() else 0.0)

    # Carousel timing (accelerating cuts): the settle point is where the
    # selected book stops moving; the title voice lands one vacuum beat later.
    vacuum_window: tuple[float, float] | None = None
    title_land = carousel_start
    if carousel_timing:
        settle_abs = sr_start + float(carousel_timing["settle_time"])
        vacuum_window = (settle_abs, settle_abs + VACUUM_SECONDS)
        title_land = settle_abs + VACUUM_SECONDS

    # 1. Hook voice during the text intro (prefer spark > edge > chattts > qwen3 > piper)
    hook_path = opening_dir / "hook_line_cosy.wav"
    if not hook_path.is_file():
        hook_path = opening_dir / "hook_line_qwentts.wav"
    if not hook_path.is_file():
        hook_path = opening_dir / "hook_line_spark.wav"
    if not hook_path.is_file():
        hook_path = opening_dir / "hook_line_edge.wav"
    if not hook_path.is_file():
        hook_path = opening_dir / "hook_line_chattts.wav"
    if not hook_path.is_file():
        hook_path = opening_dir / "hook_line_qwen3.wav"
    if not hook_path.is_file():
        hook_path = opening_dir / "hook_line_piper.wav"
    if not hook_path.is_file():
        hook_path = opening_dir / "hook_line.mp3"
    if hook_path.is_file():
        opening_clips.append(AudioClip(path=hook_path, desired_start=0.1, volume=1.0))
    # 2. Thud SFX during carousel (crescendo baked into the track itself)
    thud_path = opening_dir / "carousel_thuds.wav"
    if thud_path.is_file():
        opening_clips.append(AudioClip(path=thud_path, desired_start=sr_start, volume=0.9, is_sfx=True))
    # 3. "今天分享" during carousel
    carousel_voice_path = opening_dir / "carousel_voice_cosy.wav"
    if not carousel_voice_path.is_file():
        carousel_voice_path = opening_dir / "carousel_voice_qwentts.wav"
    if not carousel_voice_path.is_file():
        carousel_voice_path = opening_dir / "carousel_voice_spark.wav"
    if not carousel_voice_path.is_file():
        carousel_voice_path = opening_dir / "carousel_voice_edge.wav"
    if not carousel_voice_path.is_file():
        carousel_voice_path = opening_dir / "carousel_voice_chattts.wav"
    if not carousel_voice_path.is_file():
        carousel_voice_path = opening_dir / "carousel_voice_qwen3.wav"
    if not carousel_voice_path.is_file():
        carousel_voice_path = opening_dir / "carousel_voice_piper.wav"
    if not carousel_voice_path.is_file():
        carousel_voice_path = opening_dir / "carousel_voice.mp3"
    if carousel_voice_path.is_file():
        opening_clips.append(AudioClip(path=carousel_voice_path, desired_start=sr_start + 0.2, volume=1.0))
    # 4. The book title read aloud, landing one vacuum beat after the carousel
    # stops (falls back to the carousel end when no timing file exists).
    title_clip: AudioClip | None = None
    title_voice_path = opening_dir / "title_only_voice_cosy.wav"
    if not title_voice_path.is_file():
        title_voice_path = opening_dir / "title_only_voice_qwentts.wav"
    if not title_voice_path.is_file():
        title_voice_path = opening_dir / "title_only_voice_spark.wav"
    if not title_voice_path.is_file():
        title_voice_path = opening_dir / "title_only_voice_edge.wav"
    if not title_voice_path.is_file():
        title_voice_path = opening_dir / "title_only_voice_chattts.wav"
    if not title_voice_path.is_file():
        title_voice_path = opening_dir / "title_only_voice_qwen3.wav"
    if not title_voice_path.is_file():
        title_voice_path = opening_dir / "title_only_voice_piper.wav"
    if not title_voice_path.is_file():
        title_voice_path = opening_dir / "title_only_voice.mp3"
    if title_voice_path.is_file():
        title_clip = AudioClip(path=title_voice_path, desired_start=title_land, volume=1.0)
        opening_clips.append(title_clip)

    opening_voice = None
    voice_delay_val = intro_offset
    if opening_clips:
        sequence_clips(opening_clips, gap=0.15)
        # The vacuum hugs the title voice wherever it actually landed, so the
        # dead-air beat always leads straight into the book name being read.
        if title_clip is not None:
            vacuum_window = (title_clip.actual_start - VACUUM_SECONDS, title_clip.actual_start)
        opening_voice = render_dir / "opening_mixed.wav"
        voice_end = get_voice_end(opening_clips)
        mix_clips(opening_clips, opening_voice, total_duration=voice_end + 2.0)
        # 1.2s pause after title voice before main narration starts
        voice_delay_val = voice_end + 1.2
        print(f"  audio sequencer: voice_delay={voice_delay_val:.2f}s (opening ends at {voice_end:.2f}s)")

    # total_duration must cover the actual narration slot: the opening audio
    # can run longer than the visual intro (intro_offset), so recompute now
    # that voice_delay_val is final. Without this the narration tail (and the
    # outro card) gets clipped by the `-t total_duration` in the final mix.
    total_duration = round(max(total_duration, voice_delay_val + content_duration), 3)

    # --- Render overlays with shifted timings ---
    overlay_dir = render_dir / "overlays"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlays: list[dict[str, Any]] = []

    # Caption overlays: shift by voice_delay_val (matches actual narration start)
    # V02/V03 skipped (opening already introduces book name)
    skip_lines = {"V02", "V03"}
    for line in lines:
        if line.line_id in skip_lines:
            continue
        caption_path = overlay_dir / f"{line.line_id}.png"
        v2.render_caption_layer(style, line, WIDTH, HEIGHT, "bilingual").save(caption_path)
        overlays.append({
            "path": caption_path,
            "start": round(line.start + voice_delay_val, 3),
            "end": round(line.end + voice_delay_val, 3),
            "kind": "caption",
            "line_id": line.line_id,
        })

    # Outro content overlay — channel branding + thank you
    outro_path = overlay_dir / "outro_card.png"
    render_outro_layer(style, book_title, WIDTH, HEIGHT).save(outro_path)
    overlays.append({
        "path": outro_path,
        "start": round(voice_delay_val + voice_duration, 3),
        "end": total_duration,
        "kind": "outro",
    })

    # BGM. Resolve by pattern, not by one project's filename: the release gate
    # already requires exactly one v4-*-original-bgm.mp3 per project, so a
    # hardcoded name would only ever find the project it was written for.
    music_dir = project / "06_music_音乐"
    bgm_candidates = sorted(music_dir.glob("v4-*-original-bgm.mp3"))
    if not bgm_candidates:
        raise FileNotFoundError(
            f"No BGM found: expected exactly one v4-*-original-bgm.mp3 in {music_dir}. "
            "Run generate_original_bgm.py for this project first."
        )
    if len(bgm_candidates) > 1:
        raise FileNotFoundError(
            f"Ambiguous BGM: {music_dir} holds {len(bgm_candidates)} v4-*-original-bgm.mp3 "
            f"files ({', '.join(path.name for path in bgm_candidates)}). "
            "Each project owns exactly one original BGM; remove the extras."
        )
    bgm = bgm_candidates[0]

    # Composite final video
    slug = read_json(project / "project.json")["project_id"]
    output_dir = args.output or (project / "10_delivery_交付/broll")
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = {"broll_stock": "stock", "broll_local": "local", "broll_nature": "nature"}.get(render_mode, "nature")
    output_path = output_dir / f"{slug}-broll-{suffix}-3x4.mp4"

    mix_audio(full_video, overlays, voice, bgm, total_duration, output_path, style, voice_delay=voice_delay_val, opening_voice=opening_voice, vacuum=vacuum_window)

    # Write render manifest
    manifest = {
        "schema_version": "1.1",
        "render_mode": render_mode,
        "project_id": slug,
        "script_version": script["version"],
        "voice": str(voice.relative_to(project)),
        "voice_duration_seconds": voice_duration,
        "total_duration_seconds": total_duration,
        "xfade_duration": args.xfade,
        "segments": segment_meta,
        "lines": [line.__dict__ for line in lines],
        "output": str(output_path.relative_to(project)),
    }
    write_json(output_dir / "render_manifest.broll.json", manifest)

    # QC probe
    media = json.loads(run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output_path)],
        capture=True,
    ).stdout)
    video_stream = next(s for s in media["streams"] if s["codec_type"] == "video")
    audio_stream = next(s for s in media["streams"] if s["codec_type"] == "audio")
    qc_pass = (
        video_stream["width"] == WIDTH
        and video_stream["height"] == HEIGHT
        and video_stream["codec_name"] == "h264"
        and audio_stream["codec_name"] == "aac"
    )

    print(json.dumps({
        "project": slug,
        "render_mode": render_mode,
        "output": str(output_path),
        "qc": "pass" if qc_pass else "fail",
        "dimensions": [video_stream["width"], video_stream["height"]],
        "duration": total_duration,
        "segments": len(segment_meta),
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except BrollError as exc:
        raise SystemExit(f"B-roll render failed: {exc}") from exc
