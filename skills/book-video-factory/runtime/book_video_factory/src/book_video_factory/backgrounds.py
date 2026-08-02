"""Procedural nature-inspired animated background generator.

Generates soft gradient backgrounds with subtle animation using ffmpeg's geq
filter, plus a text-safe frame overlay rendered via PIL.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Nature-inspired color palettes (dark-to-light vertical gradients)
# ---------------------------------------------------------------------------

NATURE_PALETTES: list[dict[str, object]] = [
    {"name": "morning_mist", "colors": ["#1a2a3a", "#2d4a5a", "#4a7a8a", "#8ab4c4"]},
    {"name": "forest_canopy", "colors": ["#0a1f0a", "#1a3a1a", "#2a5a2a", "#4a8a4a"]},
    {"name": "golden_hour", "colors": ["#2a1a0a", "#5a3a1a", "#8a6a3a", "#c4a46a"]},
    {"name": "ocean_calm", "colors": ["#0a1a2a", "#1a3a5a", "#2a5a8a", "#5a9ac4"]},
    {"name": "lavender_dusk", "colors": ["#1a1a2a", "#2a2a4a", "#4a3a6a", "#7a6a9a"]},
    {"name": "autumn_warm", "colors": ["#2a1a0a", "#4a2a1a", "#7a4a2a", "#b47a4a"]},
    {"name": "rainy_window", "colors": ["#1a1a1a", "#2a2a3a", "#3a3a4a", "#5a5a6a"]},
    {"name": "spring_meadow", "colors": ["#1a2a1a", "#2a4a2a", "#4a7a3a", "#7ab45a"]},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert '#rrggbb' to an (r, g, b) tuple."""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# ---------------------------------------------------------------------------
# Background generation
# ---------------------------------------------------------------------------


def generate_background(
    output: Path,
    duration: float,
    palette_index: int,
    width: int = 720,
    height: int = 960,
    fps: int = 30,
) -> Path:
    """Generate a single animated background clip using ffmpeg geq filter.

    The gradient slowly cycles through the palette colors over *duration*
    seconds, with a gentle vertical drift and gaussian softening to produce
    a calm, nature-inspired backdrop.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    palette = NATURE_PALETTES[palette_index % len(NATURE_PALETTES)]
    colors = palette["colors"]  # type: ignore[index]
    c0, c1, c2, c3 = (hex_to_rgb(c) for c in colors)  # type: ignore[arg-type]

    # Animated vertical gradient using geq.
    # geq has no lerp(); express as a+(b-a)*t manually.
    # Inner: vertical gradient between c0->c1 (top) and c2->c3 (bottom)
    # Outer: time-modulated blend between the two gradients.
    def _geq_channel(ch: int, phase: float) -> str:
        top = f"{c0[ch]}+({c1[ch]}-{c0[ch]})*(Y/H)"
        bot = f"{c2[ch]}+({c3[ch]}-{c2[ch]})*(Y/H)"
        blend = f"0.5+0.5*sin(T*0.3+{phase})"
        return f"({top})+(({bot})-({top}))*({blend})"

    geq_r = _geq_channel(0, 0.0)
    geq_g = _geq_channel(1, 1.0)
    geq_b = _geq_channel(2, 2.0)

    vf = (
        f"geq=r='{geq_r}':g='{geq_g}':b='{geq_b}',"
        f"gblur=sigma=40,"
        f"eq=brightness=0.02:saturation=0.8,"
        f"fps={fps}"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"color=black:s={width}x{height}:d={duration:.3f}:r={fps}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-t", f"{duration:.3f}",
        str(output),
    ]

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output


def generate_background_sequence(
    output_dir: Path,
    segment_durations: list[float],
    width: int = 720,
    height: int = 960,
    fps: int = 30,
) -> list[Path]:
    """Generate one background clip per segment, cycling through palettes.

    Returns the list of generated clip paths in order.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    clips: list[Path] = []
    for i, dur in enumerate(segment_durations):
        clip_path = output_dir / f"bg_{i:03d}.mp4"
        generate_background(
            output=clip_path,
            duration=dur,
            palette_index=i,
            width=width,
            height=height,
            fps=fps,
        )
        clips.append(clip_path)
    return clips


# ---------------------------------------------------------------------------
# Crossfade concatenation
# ---------------------------------------------------------------------------


def concat_with_crossfades(
    clips: list[Path],
    output: Path,
    xfade_duration: float = 0.8,
    fps: int = 30,
) -> Path:
    """Concatenate background clips with xfade crossfade transitions.

    Uses ffmpeg's xfade filter with 'fade' transition type, chaining each
    clip pair sequentially.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if not clips:
        raise ValueError("No clips to concatenate")

    if len(clips) == 1:
        # Single clip — just re-encode to output
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(clips[0]),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            str(output),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return output

    # Probe each clip duration for offset calculation
    durations: list[float] = []
    for clip in clips:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(clip),
        ]
        result = subprocess.run(probe_cmd, check=True, capture_output=True, text=True)
        durations.append(float(result.stdout.strip()))

    # Build xfade filter chain
    # Each xfade shortens total by xfade_duration
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        command.extend(["-i", str(clip)])

    filters: list[str] = []
    offsets: list[float] = []
    cumulative = 0.0
    for i, dur in enumerate(durations):
        if i == 0:
            cumulative = dur
        else:
            offsets.append(cumulative - xfade_duration)
            cumulative = cumulative - xfade_duration + dur

    # Chain: [0:v][1:v]xfade=...[xf1]; [xf1][2:v]xfade=...[xf2]; ...
    prev_label = "0:v"
    for i in range(1, len(clips)):
        out_label = f"xf{i}"
        offset = offsets[i - 1]
        filters.append(
            f"[{prev_label}][{i}:v]xfade=transition=fade:"
            f"duration={xfade_duration:.3f}:offset={offset:.3f}[{out_label}]"
        )
        prev_label = out_label

    filters.append(f"[{prev_label}]fps={fps},format=yuv420p[out]")

    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(output),
    ])

    subprocess.run(command, check=True, capture_output=True, text=True)
    return output


# ---------------------------------------------------------------------------
# Text-safe frame overlay
# ---------------------------------------------------------------------------


def render_text_safe_frame(
    output: Path,
    width: int = 720,
    height: int = 960,
) -> Path:
    """Render a text-safe frame overlay as a transparent PNG.

    Draws a rounded rectangle covering ~80% width and ~65% height, centered
    slightly below the vertical midpoint (55% from top). The fill is a very
    subtle semi-transparent black with a barely-visible white border.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Panel dimensions: 80% width, 65% height
    panel_w = int(width * 0.80)
    panel_h = int(height * 0.65)

    # Center horizontally, position at 55% from top
    x0 = (width - panel_w) // 2
    y_center = int(height * 0.55)
    y0 = y_center - panel_h // 2
    x1 = x0 + panel_w
    y1 = y0 + panel_h

    corner_radius = 24

    # Fill: subtle semi-transparent black
    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=corner_radius,
        fill=(0, 0, 0, 90),
    )

    # Border: barely visible white edge (1px)
    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=corner_radius,
        outline=(255, 255, 255, 40),
        width=1,
    )

    img.save(str(output), "PNG")
    return output
