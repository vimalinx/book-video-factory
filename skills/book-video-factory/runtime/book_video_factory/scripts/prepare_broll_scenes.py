#!/usr/bin/env python3
"""Fetch and fit one commercial-capable stock clip per V4 scene."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
from book_video_factory.broll import (
    BrollClient,
    BrollError,
    load_scene_queries,
    load_sources_config,
    prepare_scene_stock,
)
from book_video_factory.scene_contract import V4_SCENE_LINE_CONTRACT, V4_TIMELINE_SCENES


def default_segments(duration_hint: float = 5.0) -> list[dict]:
    segments = []
    for scene_name, scene_id in V4_TIMELINE_SCENES:
        if scene_id not in V4_SCENE_LINE_CONTRACT:
            continue
        segments.append(
            {
                "scene_id": scene_id,
                "scene_name": scene_name,
                "duration": duration_hint,
            }
        )
    return segments


def segments_from_manifest(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    segments = []
    for item in payload.get("segments") or []:
        segments.append(
            {
                "scene_id": item["scene_id"],
                "scene_name": item["scene_name"],
                "duration": float(item.get("duration") or item.get("end", 0) - item.get("start", 0)),
            }
        )
    if not segments:
        raise BrollError(f"No segments found in {path}")
    return segments


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare per-scene stock B-roll clips (Coverr first, then Pexels/Pixabay)"
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument(
        "--from-manifest",
        type=Path,
        help="Reuse segment durations from an existing render_manifest.broll.json",
    )
    parser.add_argument(
        "--duration-hint",
        type=float,
        default=5.0,
        help="Per-scene duration when --from-manifest is not set",
    )
    parser.add_argument("--force", action="store_true", help="Re-download and re-fit all scenes")
    args = parser.parse_args()

    project = args.project.resolve()
    if not (project / "project.json").is_file():
        raise FileNotFoundError(f"Project manifest not found: {project / 'project.json'}")

    if args.from_manifest:
        segments = segments_from_manifest(args.from_manifest.resolve())
    else:
        segments = default_segments(args.duration_hint)

    client = BrollClient()
    sources = load_sources_config()
    queries = load_scene_queries(project=project)
    assignment = prepare_scene_stock(
        project,
        segments,
        client=client,
        scene_queries=queries,
        sources=sources,
        force=args.force,
    )
    print(
        json.dumps(
            {
                "project": project.name,
                "providers": client.available_providers(sources),
                "scenes": len(assignment.get("scenes") or []),
                "assignment": str(
                    project / "03_images_生成图片" / "broll-approved" / "scene-assignment.json"
                ),
                "status": "commercial_capable",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrollError as exc:
        raise SystemExit(f"B-roll prepare failed: {exc}")
