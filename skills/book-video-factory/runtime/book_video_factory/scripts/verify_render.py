#!/usr/bin/env python3
"""Render-product audit: verify the *actual* audio content of a rendered video.

Checks, in order:
  1. WAV header sanity for every active voice artifact (catches the
     streaming-placeholder headers that silently break amix).
  2. opening_mixed.wav actually contains each opening clip (RMS per window).
  3. Final video audio contains every expected sentence, in order:
     opening lines (opening_lines.json) + narration lines V04..V15
     (script.v2.bilingual.json minus V01-V03, which the opening covers).

Run with the whisper-capable venv, e.g.:
    ../../.local-batch/sparktts-venv/bin/python \
        book_video_factory/scripts/verify_render.py \
        book_video_warehouse/projects/nonviolent-communication \
        path/to/final.mp4
"""
from __future__ import annotations

import json
import math
import re
import struct
import subprocess
import sys
import wave
from pathlib import Path

SKIP_IDS = ("V01", "V02", "V03")  # covered by the opening, not the narration


def norm(text: str) -> str:
    return re.sub(r"[\s，。、？！—…·《》：:,.?!\-\"']", "", text)


def wav_rms(path: Path, t0: float, t1: float) -> float:
    with wave.open(str(path)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        w.setpos(int(t0 * sr))
        data = w.readframes(int((t1 - t0) * sr))
    seg = struct.unpack(f"<{len(data) // 2}h", data)
    if not seg:
        return -180.0
    return 20 * math.log10(max(1e-9, math.sqrt(sum(x * x for x in seg) / len(seg)) / 32768))


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        check=True, text=True, capture_output=True,
    )
    return float(out.stdout.strip())


def check_headers(files: list[Path]) -> list[str]:
    problems = []
    for f in files:
        if not f.is_file():
            problems.append(f"缺失: {f.name}")
            continue
        try:
            with wave.open(str(f)) as w:
                dur_wave = w.getnframes() / w.getframerate()
        except Exception as e:  # noqa: BLE001
            problems.append(f"坏头: {f.name} ({e})")
            continue
        dur_probe = ffprobe_duration(f)
        if abs(dur_wave - dur_probe) > 0.5:
            problems.append(f"头长不一: {f.name} wave={dur_wave:.2f}s probe={dur_probe:.2f}s")
    return problems


def main() -> int:
    project, video = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    voice_dir = project / "05_voice_人声"
    opening_dir = voice_dir / "opening"
    opening = json.loads((opening_dir / "opening_lines.json").read_text())["lines"]
    script = json.loads(
        (project / "02_story_script_故事脚本/script.v2.bilingual.json").read_text()
    )
    narration_lines = [l for l in script["lines"] if l["id"] not in SKIP_IDS]
    failures: list[str] = []

    # 1. Header sanity
    artifacts = [opening_dir / l["file"] for l in opening]
    artifacts += [opening_dir / "carousel_thuds.wav", voice_dir / "v3-cosy-master.wav"]
    problems = check_headers(artifacts)
    failures += problems
    print(f"1. WAV 头检查: {len(artifacts)} 个文件, {len(problems)} 个问题")
    for p in problems:
        print(f"   ✗ {p}")

    # 2. opening_mixed content
    mixed = project / "08_render_合成/broll/opening_mixed.wav"
    if mixed.is_file():
        import _bootstrap  # noqa: F401
        from book_video_factory.audio_sequencer import AudioClip, sequence_clips

        clips = []
        pos = 0.0
        for l in opening:
            clips.append(AudioClip(path=opening_dir / l["file"], desired_start=pos))
            pos += 0.2
        sequence_clips(clips, gap=0.15)
        print("2. opening_mixed 内容检查:")
        for clip, l in zip(clips, opening):
            rms = wav_rms(mixed, clip.actual_start + 0.05, clip.actual_end - 0.05)
            ok = rms > -60
            print(f"   {'✓' if ok else '✗'} {l['role']}: {rms:.1f} dB @ {clip.actual_start:.2f}s")
            if not ok:
                failures.append(f"opening_mixed 缺少 {l['role']} ({l['text'][:10]}…)")
    else:
        failures.append("opening_mixed.wav 不存在")
        print("2. opening_mixed.wav 不存在 ✗")

    # 3. Final audio sentence inventory
    print("3. 成片音轨逐句清单:")
    import whisper  # deferred: heavy

    model = whisper.load_model("turbo")
    result = model.transcribe(str(video), language="zh")
    full_text = norm("".join(s["text"] for s in result["segments"]))
    cursor = 0
    prev_end = -1.0
    expected = [(l["role"], l["text"]) for l in opening]
    expected += [(l["id"], l.get("zh") or l.get("text", "")) for l in narration_lines]
    for tag, text in expected:
        target = norm(text)
        idx = full_text.find(target, max(cursor - 4, 0))
        if idx < 0:
            idx = full_text.find(target)  # out-of-order fallback
        found = idx >= 0
        # rough order check via character offset progression
        ordered = found and idx >= cursor - 4
        if found and ordered:
            cursor = idx + len(target)
        mark = "✓" if ordered else ("乱序" if found else "✗ 缺失")
        print(f"   {mark} {tag}: {text[:24]}")
        if not ordered:
            failures.append(f"成片缺失/乱序: {tag} {text[:16]}")
    print(f"\n{'全部通过' if not failures else f'{len(failures)} 项失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
