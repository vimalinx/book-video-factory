#!/usr/bin/env python3
"""Search free stock video providers for license-safe B-roll candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
from book_video_factory.broll import (
    BrollClient,
    BrollError,
    download_clip,
    load_sources_config,
    normalize_candidates,
    provider_priority,
    write_candidate_manifest,
)
from book_video_factory.project import write_json


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search Coverr/Pexels/Pixabay for free B-roll video candidates"
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument(
        "--query",
        help="Concise English search keywords. Defaults to --intent.",
    )
    parser.add_argument(
        "--provider",
        choices=["auto", "coverr", "pexels", "pixabay"],
        default="auto",
    )
    parser.add_argument("--min-duration", type=float, default=5.0)
    parser.add_argument("--max-duration", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download top candidates to broll-approved/",
    )
    args = parser.parse_args()
    if not (args.project / "project.json").is_file():
        raise FileNotFoundError(f"Project manifest not found: {args.project / 'project.json'}")
    if args.limit <= 0:
        raise BrollError("--limit must be positive")
    if args.min_duration <= 0 or args.max_duration <= args.min_duration:
        raise BrollError("Duration range must be positive and ascending")

    search_query = args.query or args.intent
    client = BrollClient()
    sources = load_sources_config()
    available = client.available_providers(sources)
    if not available:
        raise BrollError("No automated providers available")

    if args.provider == "auto":
        providers = [name for name in provider_priority(sources) if name in available]
    else:
        if args.provider not in available:
            raise BrollError(
                f"Provider {args.provider} is not available "
                f"(need API key or Coverr network access). Available: {available}"
            )
        providers = [args.provider]

    candidates: list[dict] = []
    rejected_count = 0
    raw_payload: dict = {}
    used_provider = providers[0]
    for provider in providers:
        used_provider = provider
        raw_payload = client.search(
            provider,
            search_query,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            per_page=max(args.limit * 4, 24),
        )
        if provider == "coverr":
            results = raw_payload.get("hits", [])
        elif provider == "pexels":
            results = raw_payload.get("videos", [])
        else:
            results = raw_payload.get("hits", [])
        candidates, rejected_count = normalize_candidates(
            results,
            provider=provider,
            intent=args.intent,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            limit=args.limit,
            client=client if provider == "coverr" else None,
        )
        if candidates:
            break

    manifest_path = write_candidate_manifest(
        args.project,
        intent=args.intent,
        search_query=search_query,
        provider=used_provider,
        raw_payload=raw_payload,
        candidates=candidates,
        rejected_count=rejected_count,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
    )

    downloaded: list[dict] = []
    if args.download and candidates:
        approved_dir = args.project.resolve() / "03_images_生成图片" / "broll-approved"
        for candidate in candidates:
            clip_id = candidate["clip_id"]
            dest = approved_dir / f"{used_provider}-{clip_id}.mp4"
            record = download_clip(
                candidate["download_url"],
                dest,
                provider=used_provider,
                clip_id=clip_id,
                referer=candidate.get("source_page"),
            )
            record["selection_intent"] = args.intent
            downloaded.append(record)
        provenance_path = approved_dir / "broll-provenance.json"
        write_json(
            provenance_path,
            {
                "schema_version": "1.0",
                "generated_at": downloaded[0]["downloaded_at"] if downloaded else "",
                "provider": used_provider,
                "clips": downloaded,
            },
        )

    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "provider": used_provider,
                "candidates": len(candidates),
                "rejected": rejected_count,
                "downloaded": len(downloaded),
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
        raise SystemExit(f"B-roll search failed: {exc}")
