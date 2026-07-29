from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .project import write_json
from .weread import trusted_ssl_context


PEXELS_API_URL = "https://api.pexels.com/videos/search"
PIXABAY_API_URL = "https://pixabay.com/api/videos/"
COVERR_API_URL = "https://coverr.co/api/videos"

FACTORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES_PATH = FACTORY_ROOT / "config" / "broll_sources.json"
DEFAULT_SCENE_QUERIES_PATH = FACTORY_ROOT / "config" / "broll_scene_queries.json"

COVERR_LICENSE = {
    "code": "Coverr License",
    "name": "Coverr License (CC0-like)",
    "url": "https://coverr.co/license",
    "commercial_use": True,
    "attribution_required": False,
}

PEXELS_LICENSE = {
    "code": "Pexels License",
    "name": "Pexels License",
    "url": "https://www.pexels.com/license/",
    "commercial_use": True,
    "attribution_required": False,
}

PIXABAY_LICENSE = {
    "code": "Pixabay Content License",
    "name": "Pixabay Content License",
    "url": "https://pixabay.com/service/license-summary/",
    "commercial_use": True,
    "attribution_required": False,
}

LICENSE_BY_PROVIDER = {
    "coverr": COVERR_LICENSE,
    "pexels": PEXELS_LICENSE,
    "pixabay": PIXABAY_LICENSE,
}


class BrollError(RuntimeError):
    pass


class BrollRateLimitError(BrollError):
    """The provider refused to answer because we asked too often.

    This is deliberately distinct from "the provider has no matching clip". A
    rate limit must never be reported as an absence of licensed footage, or the
    operator goes looking for a content problem that does not exist.
    """


#: Minimum gap between requests to one provider. Free stock APIs are a shared
#: resource and Coverr rate-limits aggressively; pacing costs a few seconds per
#: project and avoids being locked out mid-run.
MIN_REQUEST_INTERVAL_SECONDS = 0.35

#: How many times a rate-limited or transiently failing request is retried.
MAX_REQUEST_RETRIES = 4

#: Longest cool-off this client will sit through inside a single run. Coverr's
#: edge can ask for tens of minutes after a request burst; waiting that long
#: inside a render is worse than failing with the real wait time reported.
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_sources_config(path: Path | None = None) -> dict[str, Any]:
    return _load_json(path or DEFAULT_SOURCES_PATH)


def load_scene_queries(
    path: Path | None = None,
    *,
    project: Path | None = None,
) -> dict[str, Any]:
    """Load default scene queries, optionally overlaid by a project override file."""
    config = _load_json(path or DEFAULT_SCENE_QUERIES_PATH)
    if project is not None:
        override = (
            project.resolve()
            / "03_images_生成图片"
            / "broll-approved"
            / "scene-queries.override.json"
        )
        if override.is_file():
            overlay = _load_json(override)
            scenes = dict(config.get("scenes") or {})
            for name, payload in (overlay.get("scenes") or {}).items():
                if isinstance(payload, dict):
                    scenes[name] = {**(scenes.get(name) or {}), **payload}
            config = {**config, **{k: v for k, v in overlay.items() if k != "scenes"}}
            config["scenes"] = scenes
    return config


def provider_priority(sources: dict[str, Any] | None = None) -> list[str]:
    cfg = sources or load_sources_config()
    priority = cfg.get("provider_priority")
    if isinstance(priority, list) and priority:
        return [str(item) for item in priority]
    return ["coverr", "pexels", "pixabay"]


class BrollClient:
    def __init__(
        self,
        pexels_api_key: str | None = None,
        pixabay_api_key: str | None = None,
        timeout: int = 30,
        user_agent: str = "book-video-factory/0.2",
    ) -> None:
        self._pexels_key = pexels_api_key or os.environ.get("PEXELS_API_KEY", "").strip()
        self._pixabay_key = pixabay_api_key or os.environ.get("PIXABAY_API_KEY", "").strip()
        self._timeout = timeout
        self._user_agent = user_agent
        self._ssl_context = trusted_ssl_context()
        self._last_request_at = 0.0

    def available_providers(self, sources: dict[str, Any] | None = None) -> list[str]:
        available: list[str] = []
        for name in provider_priority(sources):
            if name == "coverr":
                available.append(name)
            elif name == "pexels" and self._pexels_key:
                available.append(name)
            elif name == "pixabay" and self._pixabay_key:
                available.append(name)
        return available

    def _throttle(self) -> None:
        """Pace requests so a run does not trip the provider's rate limiter."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _requested_wait_seconds(error: urllib.error.HTTPError) -> float | None:
        """Return the provider's requested cool-off, if it stated one."""
        header = error.headers.get("Retry-After") if error.headers else None
        if not header:
            return None
        try:
            return max(0.0, float(header))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _retry_after_seconds(cls, error: urllib.error.HTTPError, attempt: int) -> float:
        """Honour Retry-After when present, else back off exponentially.

        The wait is capped so one unlucky request cannot stall a render for the
        provider's full cool-off window; a longer request is surfaced to the
        operator as a rate-limit error instead.
        """
        requested = cls._requested_wait_seconds(error)
        if requested is not None:
            return max(0.5, min(requested, MAX_RATE_LIMIT_WAIT_SECONDS))
        return min(2.0**attempt, 16.0)

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self._user_agent,
                **(headers or {}),
            },
            method="GET",
        )
        last_rate_limit: urllib.error.HTTPError | None = None
        for attempt in range(MAX_REQUEST_RETRIES):
            self._throttle()
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout, context=self._ssl_context
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 429 is a rate limit; 5xx is the provider having a bad moment.
                # Both are worth retrying, and neither means "no such clip".
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_rate_limit = exc if exc.code == 429 else last_rate_limit
                    requested_wait = self._requested_wait_seconds(exc)
                    # A provider asking for a long cool-off means retrying inside
                    # this run is pointless; fail fast and report the real wait
                    # rather than stalling the operator for an unknown time.
                    if (
                        exc.code == 429
                        and requested_wait is not None
                        and requested_wait > MAX_RATE_LIMIT_WAIT_SECONDS
                    ):
                        raise BrollRateLimitError(
                            f"B-roll provider rate-limited this client and asked to wait "
                            f"{int(requested_wait)}s (~{max(1, round(requested_wait / 60))} min). "
                            f"Re-run after that window. This is not a shortage of licensed "
                            f"footage, and re-running sooner only extends the block."
                        ) from exc
                    if attempt < MAX_REQUEST_RETRIES - 1:
                        time.sleep(self._retry_after_seconds(exc, attempt))
                        continue
                    if exc.code == 429:
                        hint = (
                            f" The provider asked to wait {int(requested_wait)}s."
                            if requested_wait is not None
                            else ""
                        )
                        raise BrollRateLimitError(
                            f"B-roll provider rate-limited this run after "
                            f"{MAX_REQUEST_RETRIES} attempts (HTTP 429).{hint} "
                            f"Wait for that window and re-run; this is not a shortage "
                            f"of licensed footage."
                        ) from exc
                    raise BrollError(
                        f"B-roll provider failed with HTTP {exc.code} after "
                        f"{MAX_REQUEST_RETRIES} attempts"
                    ) from exc
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise BrollError(f"B-roll HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < MAX_REQUEST_RETRIES - 1:
                    time.sleep(min(2.0**attempt, 16.0))
                    continue
                raise BrollError(f"B-roll request failed: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise BrollError(f"B-roll response was not JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise BrollError("B-roll provider returned an invalid payload")
            return payload
        # Unreachable: every path above either returns or raises.
        raise BrollError("B-roll request exhausted its retries") from last_rate_limit

    def search_coverr(
        self,
        query: str,
        *,
        per_page: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        if not query.strip():
            raise BrollError("Coverr search query cannot be empty")
        # Coverr's public search often returns zero hits for long phrases.
        # Keep the query short and do not pass unused filter flags.
        params = urllib.parse.urlencode(
            {
                "query": query.strip(),
                "page": max(int(page), 1),
                "page_size": min(max(int(per_page), 1), 50),
            }
        )
        payload = self._get_json(f"{COVERR_API_URL}?{params}")
        if not isinstance(payload.get("hits"), list):
            raise BrollError("Coverr returned an invalid search payload")
        return payload

    def coverr_detail(self, clip_id: str) -> dict[str, Any]:
        if not str(clip_id).strip():
            raise BrollError("Coverr clip id cannot be empty")
        payload = self._get_json(f"{COVERR_API_URL}/{urllib.parse.quote(str(clip_id))}")
        if not isinstance(payload, dict) or not payload.get("id"):
            raise BrollError("Coverr returned an invalid detail payload")
        return payload

    def search_pexels(
        self,
        query: str,
        *,
        min_duration: float = 5.0,
        max_duration: float = 30.0,
        orientation: str = "portrait",
        per_page: int = 20,
    ) -> dict[str, Any]:
        if not self._pexels_key:
            raise BrollError("PEXELS_API_KEY not set")
        if not query.strip():
            raise BrollError("Pexels search query cannot be empty")
        params = urllib.parse.urlencode(
            {
                "query": query,
                "orientation": orientation,
                "per_page": min(max(int(per_page), 1), 80),
            }
        )
        payload = self._get_json(
            f"{PEXELS_API_URL}?{params}",
            headers={"Authorization": self._pexels_key},
        )
        if not isinstance(payload.get("videos"), list):
            raise BrollError("Pexels returned an invalid search payload")
        return payload

    def search_pixabay(
        self,
        query: str,
        *,
        min_duration: float = 5.0,
        max_duration: float = 30.0,
        per_page: int = 20,
    ) -> dict[str, Any]:
        if not self._pixabay_key:
            raise BrollError("PIXABAY_API_KEY not set")
        if not query.strip():
            raise BrollError("Pixabay search query cannot be empty")
        params = urllib.parse.urlencode(
            {
                "key": self._pixabay_key,
                "q": query,
                "per_page": min(max(int(per_page), 3), 200),
            }
        )
        payload = self._get_json(f"{PIXABAY_API_URL}?{params}")
        if not isinstance(payload.get("hits"), list):
            raise BrollError("Pixabay returned an invalid search payload")
        return payload

    def search(
        self,
        provider: str,
        query: str,
        *,
        min_duration: float = 5.0,
        max_duration: float = 30.0,
        per_page: int = 20,
        orientation: str = "portrait",
    ) -> dict[str, Any]:
        if provider == "coverr":
            return self.search_coverr(query, per_page=per_page)
        if provider == "pexels":
            return self.search_pexels(
                query,
                min_duration=min_duration,
                max_duration=max_duration,
                orientation=orientation,
                per_page=per_page,
            )
        if provider == "pixabay":
            return self.search_pixabay(
                query,
                min_duration=min_duration,
                max_duration=max_duration,
                per_page=per_page,
            )
        raise BrollError(f"Unsupported automated B-roll provider: {provider}")


def _best_pexels_video_file(video: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the best quality video file from a Pexels video entry."""
    files = video.get("video_files")
    if not isinstance(files, list):
        return None
    best = None
    best_height = -1
    for f in files:
        if not isinstance(f, dict):
            continue
        height = int(_number(f.get("height")))
        if height > best_height and f.get("link"):
            best = f
            best_height = height
    return best


def _best_pixabay_video(hit: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the best quality video from a Pixabay hit."""
    videos = hit.get("videos")
    if not isinstance(videos, dict):
        return None
    for quality in ("large", "medium", "small", "tiny"):
        entry = videos.get(quality)
        if isinstance(entry, dict) and entry.get("url"):
            return entry
    return None


def _coverr_download_url(hit: dict[str, Any], client: BrollClient | None = None) -> str:
    """Resolve a Coverr clip's mp4 URL, preferring answers that need no request.

    Order matters for rate limiting. A search response carries ``base_filename``
    but no ``urls`` block, and the CDN path derived from ``base_filename`` is
    byte-identical to the ``urls.mp4`` the detail endpoint returns. Calling the
    detail endpoint for every hit meant roughly one extra request per candidate
    — hundreds per project — which is what tripped Coverr's rate limiter. The
    detail endpoint is now a last resort for hits with no usable filename.
    """
    urls = hit.get("urls")
    if isinstance(urls, dict):
        for key in ("mp4_download", "mp4", "mp4_preview"):
            value = urls.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    base = hit.get("base_filename")
    if isinstance(base, str) and base:
        return f"https://cdn.coverr.co/videos/{base}/1080p.mp4"
    clip_id = str(hit.get("id") or hit.get("video_id") or "")
    if client is not None and clip_id:
        detail = client.coverr_detail(clip_id)
        return _coverr_download_url(detail, client=None)
    return ""


def normalize_candidates(
    results: Iterable[dict[str, Any]],
    *,
    provider: str,
    intent: str,
    min_duration: float,
    max_duration: float,
    limit: int,
    client: BrollClient | None = None,
    reject_premium: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Filter raw API results into auditable B-roll candidates."""
    if limit <= 0:
        raise BrollError("Candidate limit must be positive")
    candidates: list[dict[str, Any]] = []
    rejected = 0

    for result in results:
        if provider == "coverr":
            if reject_premium and bool(result.get("is_premium")):
                rejected += 1
                continue
            duration = _number(result.get("duration"))
            if duration < min_duration or duration > max_duration:
                rejected += 1
                continue
            clip_id = result.get("id") or result.get("video_id")
            try:
                url = _coverr_download_url(result, client=client)
            except BrollError:
                rejected += 1
                continue
            thumbnail = str(result.get("thumbnail") or result.get("poster") or "")
            license_info = dict(COVERR_LICENSE)
            slug = result.get("slug") or clip_id
            source_page = f"https://coverr.co/videos/{slug}"
            width = int(_number(result.get("max_width")))
            height = int(_number(result.get("max_height")))
            is_vertical = bool(result.get("is_vertical")) or (
                width > 0 and height > 0 and height >= width
            )
            title = str(result.get("title") or "")
        elif provider == "pexels":
            duration = _number(result.get("duration"))
            video_file = _best_pexels_video_file(result)
            if not video_file or duration < min_duration or duration > max_duration:
                rejected += 1
                continue
            clip_id = result.get("id")
            url = video_file.get("link", "")
            thumbnail = ""
            image = result.get("image")
            if isinstance(image, str):
                thumbnail = image
            license_info = dict(PEXELS_LICENSE)
            source_page = str(result.get("url") or f"https://www.pexels.com/video/{clip_id}/")
            width = int(_number(video_file.get("width")))
            height = int(_number(video_file.get("height")))
            is_vertical = height >= width if width and height else False
            title = ""
        elif provider == "pixabay":
            duration = _number(result.get("duration"))
            video_entry = _best_pixabay_video(result)
            if not video_entry or duration < min_duration or duration > max_duration:
                rejected += 1
                continue
            clip_id = result.get("id")
            url = video_entry.get("url", "")
            thumbnail = ""
            user_image = result.get("userImageURL")
            if isinstance(user_image, str):
                thumbnail = user_image
            license_info = dict(PIXABAY_LICENSE)
            source_page = str(result.get("pageURL") or f"https://pixabay.com/videos/id-{clip_id}/")
            width = int(_number(video_entry.get("width")))
            height = int(_number(video_entry.get("height")))
            is_vertical = height >= width if width and height else False
            title = str(result.get("tags") or "")
        else:
            rejected += 1
            continue

        if not clip_id or not url:
            rejected += 1
            continue

        candidates.append(
            {
                "provider": provider,
                "clip_id": clip_id,
                "title": title,
                "source_page": source_page,
                "duration_seconds": round(duration, 3),
                "width": width,
                "height": height,
                "is_vertical": is_vertical,
                "license": license_info,
                "download_url": url,
                "thumbnail_url": thumbnail,
                "selection_intent": intent,
            }
        )

    candidates.sort(
        key=lambda item: (
            0 if item.get("is_vertical") else 1,
            -item["duration_seconds"],
        )
    )
    return candidates[:limit], rejected


def write_candidate_manifest(
    project: Path,
    *,
    intent: str,
    search_query: str,
    provider: str,
    raw_payload: dict[str, Any],
    candidates: list[dict[str, Any]],
    rejected_count: int,
    min_duration: float,
    max_duration: float,
) -> Path:
    """Persist B-roll search evidence with provenance metadata."""
    source_count = (
        raw_payload.get("total_results")
        or raw_payload.get("totalHits")
        or raw_payload.get("total")
    )
    manifest = {
        "schema_version": "1.0",
        "provider": provider,
        "generated_at": utc_now(),
        "purpose": "B-roll candidate research",
        "query": intent,
        "search_query": search_query,
        "duration_range_seconds": {"min": min_duration, "max": max_duration},
        "source_result_count": source_count,
        "eligible_candidate_count": len(candidates),
        "rejected_candidate_count": rejected_count,
        "license_policy": {
            "accepted": [
                "Coverr License",
                "Pexels License",
                "Pixabay Content License",
            ],
            "rejected": [],
            "commercial_use": True,
            "attribution_required": False,
        },
        "candidates": candidates,
    }
    output = project.resolve() / "03_images_生成图片" / "broll-candidates" / "broll-candidates.json"
    write_json(output, manifest)
    return output


def download_clip(
    url: str,
    dest_path: Path,
    *,
    provider: str,
    clip_id: object,
    referer: str | None = None,
) -> dict[str, Any]:
    """Download a video clip and return a provenance manifest entry."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "book-video-factory/0.2"}
    if referer:
        headers["Referer"] = referer
    elif provider == "coverr":
        headers["Referer"] = "https://coverr.co/"
    request = urllib.request.Request(url, headers=headers, method="GET")
    ssl_context = trusted_ssl_context()
    sha256 = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl_context) as response:
            with open(dest_path, "wb") as fh:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    sha256.update(chunk)
                    fh.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BrollError(f"Download failed for clip {clip_id}: {exc}") from exc

    return {
        "provider": provider,
        "clip_id": clip_id,
        "source_url": url,
        "local_path": str(dest_path),
        "sha256": sha256.hexdigest(),
        "license": dict(LICENSE_BY_PROVIDER.get(provider, COVERR_LICENSE)),
        "downloaded_at": utc_now(),
    }


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def fit_clip_to_canvas(
    source: Path,
    output: Path,
    *,
    duration: float,
    width: int = 720,
    height: int = 960,
    fps: int = 30,
) -> Path:
    """Scale/crop a stock clip to the delivery canvas and trim/loop to duration."""
    if duration <= 0:
        raise BrollError("Target duration must be positive")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    source_duration = probe_duration(source)
    if source_duration <= 0:
        raise BrollError(f"Invalid source duration for {source}")

    # Cover short clips by looping before trim.
    loop_count = max(0, int(duration / source_duration) + 1)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},format=yuv420p"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stream_loop",
        str(loop_count),
        "-i",
        str(source),
        "-vf",
        vf,
        "-an",
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "")[:500]
        raise BrollError(f"ffmpeg fit failed for {source.name}: {detail}") from exc
    return output


def score_candidate(candidate: dict[str, Any], needed_duration: float) -> tuple[float, ...]:
    duration = float(candidate.get("duration_seconds") or 0.0)
    coverage = min(duration / max(needed_duration, 0.1), 1.5)
    vertical_bonus = 1.0 if candidate.get("is_vertical") else 0.0
    return (vertical_bonus, coverage, duration)


#: Words carrying no visual meaning. They are kept in the full phrase but never
#: allowed to become a narrowed query on their own, so narrowing cannot degrade
#: into a meaningless search that matches arbitrary footage.
QUERY_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and",
        "or", "about", "into", "over", "under", "from", "by", "as", "is", "are",
        "very", "some", "that", "this", "it", "its",
    }
)


def query_variants(query: str) -> list[str]:
    """Return a phrase and its progressively narrower, still-meaningful forms.

    Coverr's public search matches every term, so a descriptive phrase such as
    "misty mountain sunrise" returns zero hits while "misty mountain" returns
    dozens. Narrowing keeps the most specific wording that still matches
    something, instead of failing the whole scene because the operator wrote one
    word too many.

    Narrowed variants are built from content terms only. Prefix-narrowing a
    phrase that starts with filler would otherwise end at a query like
    "a very long", which matches arbitrary footage rather than the intended
    scene. The query that actually matched is recorded in each candidate's
    ``search_query`` so the provenance manifest never implies the operator's
    original wording was the one used.
    """
    terms = [term for term in str(query).split() if term]
    if not terms:
        return []
    content = [term for term in terms if term.casefold() not in QUERY_STOPWORDS]
    variants: list[str] = []
    full = " ".join(terms)
    variants.append(full)
    # Narrow over content terms, widest first, always keeping at least one.
    for count in range(len(content), 0, -1):
        variant = " ".join(content[:count])
        if variant and variant not in variants:
            variants.append(variant)
    return variants


def search_candidates_for_query(
    client: BrollClient,
    *,
    query: str,
    intent: str,
    providers: list[str],
    min_duration: float,
    max_duration: float,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Search each provider in priority order, narrowing the phrase as needed.

    Every returned candidate records the query that actually matched it in
    ``search_query`` so the provenance manifest reflects the real search rather
    than the wording the operator originally configured.
    """
    for provider in providers:
        for variant in query_variants(query):
            try:
                raw = client.search(
                    provider,
                    variant,
                    min_duration=min_duration,
                    max_duration=max_duration,
                    per_page=max(limit * 3, 12),
                )
            except BrollRateLimitError:
                # Never degrade a rate limit into "no matching clip": retrying
                # more queries would only dig deeper, and the operator needs to
                # know the provider stopped answering.
                raise
            except BrollError:
                # A provider-level failure applies to every variant, so stop
                # narrowing and move on to the next provider.
                break
            if provider == "pexels":
                results = raw.get("videos") or []
            else:
                results = raw.get("hits") or []
            if not results:
                continue
            candidates, _ = normalize_candidates(
                results,
                provider=provider,
                intent=intent,
                min_duration=min_duration,
                max_duration=max_duration,
                limit=limit,
                client=client if provider == "coverr" else None,
            )
            if candidates:
                return [{**candidate, "search_query": variant} for candidate in candidates]
    return []


def resolve_scene_candidate(
    client: BrollClient,
    *,
    scene_name: str,
    scene_cfg: dict[str, Any],
    needed_duration: float,
    providers: list[str],
    selection: dict[str, Any],
) -> dict[str, Any]:
    min_duration = float(selection.get("min_duration_seconds", 4.0))
    max_duration = float(selection.get("max_duration_seconds", 45.0))
    intent = str(scene_cfg.get("intent") or scene_name)
    queries = scene_cfg.get("queries") or []
    if not isinstance(queries, list) or not queries:
        raise BrollError(f"No search queries configured for scene {scene_name}")

    best: dict[str, Any] | None = None
    best_score: tuple[float, ...] | None = None
    tried: list[str] = []
    for query in queries:
        query_text = str(query).strip()
        if not query_text:
            continue
        tried.append(query_text)
        candidates = search_candidates_for_query(
            client,
            query=query_text,
            intent=intent,
            providers=providers,
            min_duration=min_duration,
            max_duration=max_duration,
        )
        for candidate in candidates:
            score = score_candidate(candidate, needed_duration)
            if best is None or score > best_score:
                # Keep the query that actually matched, which may be a narrowed
                # variant of the configured wording.
                best = {
                    **candidate,
                    "search_query": candidate.get("search_query") or query_text,
                    "configured_query": query_text,
                }
                best_score = score
        if best is not None and best.get("is_vertical"):
            break

    if best is None:
        raise BrollError(
            f"No commercial-capable stock clip found for scene {scene_name}; tried {tried}"
        )
    return best


def _provenance_for_assignment(
    scene_records: list[dict[str, Any]],
    *,
    download_details: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build one rights record per assigned clip, derived from the assignment.

    Provenance used to be collected only for clips downloaded during the current
    run and then written over the whole file. A resumed run therefore reused most
    clips, recorded nothing for them, and **erased** the rights evidence for
    every clip it did not re-download — leaving the broll_rights gate with almost
    no basis to review. Deriving the records from the finished assignment makes
    it impossible for the two to drift: every clip that ships is recorded.
    """
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scene in scene_records:
        if not isinstance(scene, dict):
            continue
        clip_id = scene.get("clip_id")
        if not isinstance(clip_id, str) or clip_id in seen:
            continue
        seen.add(clip_id)
        base = dict(previous.get(clip_id) or {})
        base.update(download_details.get(clip_id) or {})
        raw_path = scene.get("raw_path")
        if not base.get("sha256") and isinstance(raw_path, str) and Path(raw_path).is_file():
            base["sha256"] = hashlib.sha256(Path(raw_path).read_bytes()).hexdigest()
        # Assignment facts win: they describe the clip that actually ships.
        base.update(
            {
                "clip_id": clip_id,
                "provider": scene.get("provider"),
                "license": scene.get("license"),
                "source_page": scene.get("source_page"),
                "search_query": scene.get("search_query"),
                "local_path": raw_path,
                "used_for_scene": scene.get("scene_name"),
            }
        )
        records.append(base)
    return records


def prepare_scene_stock(
    project: Path,
    segments: list[dict[str, Any]],
    *,
    client: BrollClient | None = None,
    scene_queries: dict[str, Any] | None = None,
    sources: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Download one stock clip per scene and write the assignment manifest."""
    project = project.resolve()
    client = client or BrollClient()
    sources = sources or load_sources_config()
    scene_queries = scene_queries or load_scene_queries(project=project)
    scenes_cfg = scene_queries.get("scenes") or {}
    selection = scene_queries.get("selection") or {}
    canvas = scene_queries.get("canvas") or {}
    width = int(canvas.get("width", 720))
    height = int(canvas.get("height", 960))
    fps = int(canvas.get("fps", 30))

    providers = client.available_providers(sources)
    if not providers:
        raise BrollError(
            "No automated B-roll providers available. "
            "Coverr should always work; check network, or set PEXELS_API_KEY / PIXABAY_API_KEY."
        )

    approved_dir = project / "03_images_生成图片" / "broll-approved"
    raw_dir = approved_dir / "raw"
    fitted_dir = approved_dir / "fitted"
    approved_dir.mkdir(parents=True, exist_ok=True)

    assignment_path = approved_dir / "scene-assignment.json"
    existing: dict[str, Any] = {}
    if assignment_path.is_file() and not force:
        existing = _load_json(assignment_path)

    existing_by_scene = {
        item["scene_name"]: item
        for item in existing.get("scenes", [])
        if isinstance(item, dict) and item.get("scene_name")
    }

    scene_records: list[dict[str, Any]] = []
    # Download-time facts for clips fetched during *this* run, keyed by clip id.
    # Provenance itself is derived from the finished assignment below, so a clip
    # reused from a previous run keeps its rights record instead of losing it.
    download_details: dict[str, dict[str, Any]] = {}
    previous_provenance: dict[str, dict[str, Any]] = {}
    provenance_path = approved_dir / "broll-provenance.json"
    if provenance_path.is_file():
        try:
            for entry in _load_json(provenance_path).get("clips") or []:
                if isinstance(entry, dict) and isinstance(entry.get("clip_id"), str):
                    previous_provenance[entry["clip_id"]] = entry
        except (OSError, json.JSONDecodeError):
            previous_provenance = {}

    for segment in segments:
        scene_name = str(segment["scene_name"])
        scene_id = str(segment["scene_id"])
        needed = float(segment["duration"])
        reused = existing_by_scene.get(scene_name)
        if (
            reused
            and not force
            and Path(str(reused.get("fitted_path", ""))).is_file()
            and Path(str(reused.get("raw_path", ""))).is_file()
        ):
            # Re-fit if duration changed materially.
            fitted_path = Path(str(reused["fitted_path"]))
            fitted_duration = probe_duration(fitted_path)
            if abs(fitted_duration - needed) <= 0.35:
                scene_records.append(reused)
                continue
            fitted_path = fit_clip_to_canvas(
                Path(str(reused["raw_path"])),
                fitted_dir / f"{scene_id}_{scene_name}.mp4",
                duration=needed,
                width=width,
                height=height,
                fps=fps,
            )
            reused = {
                **reused,
                "fitted_path": str(fitted_path),
                "needed_duration_seconds": round(needed, 3),
                "refit_at": utc_now(),
            }
            scene_records.append(reused)
            continue

        scene_cfg = scenes_cfg.get(scene_name)
        if not isinstance(scene_cfg, dict):
            raise BrollError(f"Missing scene query config for {scene_name}")

        candidate = resolve_scene_candidate(
            client,
            scene_name=scene_name,
            scene_cfg=scene_cfg,
            needed_duration=needed,
            providers=providers,
            selection=selection,
        )
        raw_path = raw_dir / f"{candidate['provider']}-{candidate['clip_id']}.mp4"
        if force or not raw_path.is_file():
            record = download_clip(
                candidate["download_url"],
                raw_path,
                provider=str(candidate["provider"]),
                clip_id=candidate["clip_id"],
                referer=str(candidate.get("source_page") or None),
            )
        else:
            record = {
                "provider": candidate["provider"],
                "clip_id": candidate["clip_id"],
                "source_url": candidate["download_url"],
                "local_path": str(raw_path),
                "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "license": candidate.get("license"),
                "downloaded_at": utc_now(),
                "reused_existing": True,
            }
        record["selection_intent"] = candidate.get("selection_intent")
        record["search_query"] = candidate.get("search_query")
        download_details[str(candidate["clip_id"])] = record

        fitted_path = fit_clip_to_canvas(
            raw_path,
            fitted_dir / f"{scene_id}_{scene_name}.mp4",
            duration=needed,
            width=width,
            height=height,
            fps=fps,
        )
        scene_records.append(
            {
                "scene_id": scene_id,
                "scene_name": scene_name,
                "intent": scene_cfg.get("intent"),
                "search_query": candidate.get("search_query"),
                "provider": candidate["provider"],
                "clip_id": candidate["clip_id"],
                "source_page": candidate.get("source_page"),
                "license": candidate.get("license"),
                "raw_path": str(raw_path),
                "fitted_path": str(fitted_path),
                "needed_duration_seconds": round(needed, 3),
                "source_duration_seconds": candidate.get("duration_seconds"),
                "is_vertical": candidate.get("is_vertical"),
                "assigned_at": utc_now(),
            }
        )

    assignment = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "render_mode": "broll_stock",
        "providers_used": providers,
        "canvas": {"width": width, "height": height, "fps": fps},
        "scenes": scene_records,
    }
    write_json(assignment_path, assignment)
    write_json(
        provenance_path,
        {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "clips": _provenance_for_assignment(
                scene_records,
                download_details=download_details,
                previous=previous_provenance,
            ),
        },
    )
    return assignment
