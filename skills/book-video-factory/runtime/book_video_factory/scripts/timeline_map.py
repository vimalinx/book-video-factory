#!/usr/bin/env python3
"""Print the picture timeline and the audio timeline of a broll render side by
side, so every landing point is computed from real asset durations instead of
guessed.

画面轴 (video axis): split reveal → carousel (with every cut, the settle point
and the hold end) → content start, including the 0.6s intro crossfades.

音轨轴 (audio axis): hook → guide → title voices sequenced with the same
no-overlap rules the renderer uses, the thud SFX track, the vacuum window and
the main narration start.

The alignment check compares where the title voice actually lands against the
frames it is supposed to land on, and prints the exact knob (hold length,
voice rate, gap) to fix any miss.

Usage:
    python3 scripts/timeline_map.py <project> [--hold SECONDS] [--gap SECONDS]
                                              [--hook-rate PCT] [--guide-rate PCT]

Options simulate changes without re-rendering: --hold overrides the carousel
hold length, --gap the voice gap, --hook-rate/--guide-rate a TTS speed-up in
percent (e.g. 30 means 30%% faster).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

INTRO_XFADE = 0.6        # crossfade between intro clips in the final concat
VACUUM_SECONDS = 0.3     # dead-air beat before the title voice
HOOK_DELAY = 0.1         # hook voice start
GUIDE_DELAY = 0.2        # guide voice offset after the carousel start
NARRATION_GAP = 1.2      # pause after the title voice before main narration


def probe(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        check=True, text=True, capture_output=True,
    )
    return float(result.stdout.strip())


def fmt(seconds: float) -> str:
    return f"{seconds:6.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Picture/audio timeline map for a broll project")
    parser.add_argument("project", type=Path)
    parser.add_argument("--hold", type=float, default=None, help="simulate a different carousel hold (s)")
    parser.add_argument("--gap", type=float, default=0.15, help="voice gap used by the sequencer (s)")
    parser.add_argument("--hook-rate", type=float, default=0.0, help="simulate hook TTS faster by PCT")
    parser.add_argument("--guide-rate", type=float, default=0.0, help="simulate guide TTS faster by PCT")
    args = parser.parse_args()

    project = args.project.resolve()
    opening = project / "05_voice_人声/opening"
    timing_path = project / "08_render_合成/carousel/carousel_timing.json"
    split_reveal = project / "08_render_合成/split_reveal/split_reveal.mp4"
    carousel = project / "08_render_合成/carousel/carousel.mp4"
    manifest_path = project / "10_delivery_交付/broll/render_manifest.broll.json"

    if not timing_path.is_file():
        raise SystemExit(f"missing {timing_path} — run render_book_carousel.py first")
    timing = json.loads(timing_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ video
    sr_dur = probe(split_reveal) if split_reveal.is_file() else 0.0
    carousel_dur = timing["duration"]
    settle_rel = timing["settle_time"]
    cut_times = timing.get("cut_times", [])
    hold = args.hold if args.hold is not None else round(carousel_dur - settle_rel, 3)

    carousel_vstart = sr_dur - INTRO_XFADE if sr_dur else 0.0
    settle_v = carousel_vstart + settle_rel
    carousel_vend = carousel_vstart + carousel_dur + (hold - round(carousel_dur - settle_rel, 3))
    content_start = carousel_vend - INTRO_XFADE

    print("画面轴 (video)")
    print(f"  {fmt(0)} – {fmt(sr_dur)}  split reveal (拆字)")
    print(f"  {fmt(carousel_vstart)} – {fmt(carousel_vend)}  carousel (快切书, xfade {INTRO_XFADE}s)")
    for i, cut in enumerate(cut_times, start=1):
        print(f"        {fmt(carousel_vstart + cut)}  第 {i} 次切换")
    print(f"        {fmt(settle_v)}  ★ 停帧 (最后一本书锁定, 书名+封面同帧)")
    print(f"        {fmt(carousel_vend)}  carousel 结束")
    print(f"  {fmt(content_start)} – …  正片内容")

    # ------------------------------------------------------------------ audio
    def voice(name: str) -> float | None:
        for suffix in ("_cosy.wav", "_qwentts.wav", "_spark.wav", "_edge.wav", "_chattts.wav", "_qwen3.wav", "_piper.wav", ".mp3"):
            path = opening / f"{name}{suffix}"
            if path.is_file():
                return probe(path)
        return None

    hook_dur = voice("hook_line")
    guide_dur = voice("carousel_voice")
    title_dur = voice("title_only_voice")
    thuds_dur = probe(opening / "carousel_thuds.wav") if (opening / "carousel_thuds.wav").is_file() else None

    if args.hook_rate and hook_dur:
        hook_dur = hook_dur / (1 + args.hook_rate / 100)
    if args.guide_rate and guide_dur:
        guide_dur = guide_dur / (1 + args.guide_rate / 100)

    # Same sequencing rules as render_broll_video (voice clips never overlap,
    # each starts at max(desired, previous voice end + gap); SFX keeps desired).
    settle_a = sr_dur + settle_rel
    events: list[tuple[str, float, float, bool]] = []  # name, desired, dur, is_sfx
    if hook_dur:
        events.append(("hook 语音", HOOK_DELAY, hook_dur, False))
    if guide_dur:
        events.append(("引导语音", sr_dur + GUIDE_DELAY, guide_dur, False))
    if title_dur:
        events.append(("书名语音", settle_a + VACUUM_SECONDS, title_dur, False))

    voice_end = 0.0
    placed: list[tuple[str, float, float]] = []  # name, start, end
    for name, desired, dur, _ in sorted(events, key=lambda e: e[1]):
        start = max(desired, voice_end + (args.gap if voice_end else 0.0))
        placed.append((name, start, start + dur))
        voice_end = start + dur

    print()
    print("音轨轴 (audio)")
    for name, start, end in placed:
        print(f"  {fmt(start)} – {fmt(end)}  {name}")
    if thuds_dur:
        print(f"  {fmt(sr_dur)} – {fmt(sr_dur + thuds_dur)}  thud 鼓点 (SFX, 末击 {fmt(sr_dur + settle_rel)})")
    title_start = next((s for n, s, _ in placed if n == "书名语音"), None)
    if title_start is not None:
        print(f"  {fmt(title_start - VACUUM_SECONDS)} – {fmt(title_start)}  真空 (BGM 抽空)")
        print(f"  {fmt(voice_end + NARRATION_GAP)} – …  正片旁白 (voice_delay)")

    # ------------------------------------------------------------- alignment
    print()
    print("对轨检查")
    problems = 0
    if title_start is not None:
        on_card = settle_v <= title_start <= carousel_vend
        delta_end = title_start - carousel_vend
        delta_settle = title_start - settle_v
        status = "✓ 落在封面驻留段内" if on_card else "✗ 落在封面消失之后"
        print(f"  书名语音起点 {fmt(title_start)}  vs  停帧 {fmt(settle_v)} / 驻留结束 {fmt(carousel_vend)}  → {status}")
        if not on_card:
            problems += 1
            need_hold = hold + delta_end + 0.2
            need_rate = 0.0
            if hook_dur and guide_dur:
                # voice chain must free delta_end+0.2 seconds before the title
                need_rate = (hook_dur + guide_dur) / max((hook_dur + guide_dur) - (delta_end + 0.2), 0.1) - 1
            print(f"      晚了 {delta_end:.2f}s。修法二选一:")
            print(f"      1) 加长驻留: --hold {need_hold:.2f}  (当前 {hold:.2f}s)")
            print(f"      2) 语音提速: hook+引导再快约 {need_rate * 100:.0f}%")
        elif delta_settle < VACUUM_SECONDS:
            print(f"      注意: 书名距停帧只有 {delta_settle:.2f}s, 真空被压缩")
    print()
    print("结论:", "全部落点到位" if problems == 0 else f"{problems} 处错位, 按上面建议调整")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
