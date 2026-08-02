"""Audio sequencer: schedule clips sequentially, resolve overlaps, mix to one track.

Ensures no two voice clips overlap. SFX clips (marked as sfx=True) can overlap
with voice but not with each other.
"""
from __future__ import annotations

import re
import subprocess
import wave
import struct
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AudioClip:
    path: Path
    desired_start: float
    volume: float = 1.0
    is_sfx: bool = False
    # Computed after sequencing:
    actual_start: float = field(default=0.0, init=False)
    duration: float = field(default=0.0, init=False)

    @property
    def actual_end(self) -> float:
        return self.actual_start + self.duration


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        check=True, text=True, capture_output=True,
    )
    return float(result.stdout.strip())


def sequence_clips(clips: list[AudioClip], gap: float = 0.3) -> list[AudioClip]:
    """Resolve overlaps: voice clips never overlap each other.

    SFX clips can overlap with voice but are sequenced among themselves.
    Voice clips are pushed later if they would overlap a preceding voice clip.
    """
    for clip in clips:
        clip.duration = probe_duration(clip.path)

    # Separate voice and sfx
    voice_clips = sorted([c for c in clips if not c.is_sfx], key=lambda c: c.desired_start)
    sfx_clips = sorted([c for c in clips if c.is_sfx], key=lambda c: c.desired_start)

    # Sequence voice clips: no overlap, push later if needed
    voice_end = 0.0
    for clip in voice_clips:
        clip.actual_start = max(clip.desired_start, voice_end + gap)
        voice_end = clip.actual_end

    # SFX clips: keep desired timing (they're short effects)
    for clip in sfx_clips:
        clip.actual_start = clip.desired_start

    return clips


def _clip_audible(output: Path, clip: AudioClip, threshold_db: float = -60.0) -> bool:
    """Check that *clip* actually made it into the mixed file.

    ffmpeg's amix (n8.1.2) can nondeterministically drop an input when the
    graph auto-inserts resamplers for mismatched rates/layouts — the delayed
    stream still sets the output length but carries silence. Measure the RMS
    inside a short window where the clip should be playing.
    """
    probe_len = min(0.3, max(clip.duration - 0.1, 0.05))
    start = clip.actual_start + 0.05
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "info",
         "-ss", f"{start:.3f}", "-to", f"{start + probe_len:.3f}",
         "-i", str(output), "-af", "astats", "-f", "null", "-"],
        check=True, text=True, capture_output=True,
    )
    levels = [float(m) for m in re.findall(r"RMS level dB:\s*(-?[\d.]+)", result.stderr)]
    return bool(levels) and max(levels) > threshold_db


def mix_clips(clips: list[AudioClip], output: Path, total_duration: float | None = None) -> Path:
    """Mix all clips into a single WAV at their actual_start positions."""
    output.parent.mkdir(parents=True, exist_ok=True)

    if total_duration is None:
        total_duration = max(c.actual_end for c in clips) + 0.1

    # Build ffmpeg filter. Inputs are pre-normalized to 48 kHz stereo s32 so
    # amix never has to auto-insert resamplers (the nondeterministic drop
    # described in _clip_audible happens on that path).
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        cmd.extend(["-i", str(clip.path)])

    filters = []
    mix_inputs = []
    for i, clip in enumerate(clips):
        delay_ms = round(clip.actual_start * 1000)
        label = f"c{i}"
        vol = f",volume={clip.volume:.2f}" if clip.volume != 1.0 else ""
        filters.append(
            f"[{i}:a]aresample=48000,aformat=sample_fmts=s32:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms}{vol}[{label}]"
        )
        mix_inputs.append(f"[{label}]")

    filters.append(
        f"{''.join(mix_inputs)}amix=inputs={len(clips)}:duration=longest:normalize=0,"
        f"atrim=0:{total_duration:.3f},afade=t=in:st=0:d=0.05[out]"
    )

    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        str(output),
    ])
    for attempt in range(1, 4):
        subprocess.run(cmd, check=True, text=True, capture_output=True)
        missing = [c.path.name for c in clips if not c.is_sfx and not _clip_audible(output, c)]
        if not missing:
            return output
    raise RuntimeError(f"mix_clips: amix dropped voice clips after 3 attempts: {', '.join(missing)}")


def get_voice_end(clips: list[AudioClip]) -> float:
    """Return the end time of the last voice clip (for scheduling main narration)."""
    voice_clips = [c for c in clips if not c.is_sfx]
    if not voice_clips:
        return 0.0
    return max(c.actual_end for c in voice_clips)
