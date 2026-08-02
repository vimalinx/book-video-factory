"""Portable, policy-driven font resolution for the book-video factory.

Every renderer resolves fonts through this module instead of hardcoding paths.
Resolution is driven by ``config/font_policy.json`` so a workspace bootstrapped
on one operating system still renders on another.

Two properties matter for correctness, not just convenience:

* **Face selection is by family name, not index.** A ``.ttc`` collection holds
  several families. ``NotoSansCJK-Regular.ttc`` index 0 is *Noto Sans CJK JP*,
  which renders Japanese glyph variants for Simplified Chinese text. Candidates
  declare the family they need and this module enumerates the collection.
* **Bundled fonts carry provenance.** ``resources/fonts/BUNDLED_FONTS.json``
  records the license and upstream source of every redistributed binary. A
  bundled font whose license text is not archived is reported as a rights gap.

Only the standard library and Pillow are required.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:  # Pillow is required for face enumeration but not for policy inspection.
    from PIL import ImageFont

    _PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on planning-only installs
    ImageFont = None  # type: ignore[assignment]
    _PILLOW_AVAILABLE = False


FACTORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = FACTORY_ROOT / "config" / "font_policy.json"
BUNDLED_MANIFEST_NAME = "BUNDLED_FONTS.json"
MAX_COLLECTION_FACES = 64

ORIGIN_OPERATOR_OVERRIDE = "operator_override"
ORIGIN_STYLE_CONFIG = "style_config"
ORIGIN_PLATFORM = "platform"
ORIGIN_BUNDLED = "bundled"


class FontResolutionError(ValueError):
    """Raised when no usable font can be found for a category."""


class FontPolicyError(ValueError):
    """Raised when the font policy document itself is invalid."""


# ---------------------------------------------------------------------------
# Policy documents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FontCandidate:
    """One preference inside a category, in descending order of preference."""

    file: str
    family: str | None = None

    @classmethod
    def parse(cls, payload: Any, *, category: str) -> "FontCandidate":
        if not isinstance(payload, dict):
            raise FontPolicyError(
                f"font policy category {category!r} contains a non-object candidate"
            )
        file = payload.get("file")
        if not isinstance(file, str) or not file.strip():
            raise FontPolicyError(
                f"font policy category {category!r} has a candidate without a file name"
            )
        if Path(file).is_absolute() or ".." in Path(file).parts or "/" in file or "\\" in file:
            raise FontPolicyError(
                f"font policy candidate {file!r} in category {category!r} must be a bare "
                "file name; absolute or machine-specific paths are not portable"
            )
        family = payload.get("family")
        if family is not None and (not isinstance(family, str) or not family.strip()):
            raise FontPolicyError(
                f"font policy candidate {file!r} in category {category!r} has an empty family"
            )
        return cls(file=file, family=family)


@dataclass(frozen=True)
class FontCategory:
    name: str
    description: str
    candidates: tuple[FontCandidate, ...]
    required_script: str | None = None


@dataclass(frozen=True)
class FontPolicy:
    path: Path
    payload: dict[str, Any]
    categories: dict[str, FontCategory]

    @classmethod
    def load(cls, path: Path | None = None) -> "FontPolicy":
        resolved = (path or DEFAULT_POLICY_PATH).expanduser().resolve()
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FontPolicyError(f"cannot load font policy {resolved}: {error}") from error
        if not isinstance(payload, dict):
            raise FontPolicyError("font policy root must be an object")
        if payload.get("schema_version") != "1.0":
            raise FontPolicyError("unsupported font policy schema_version")
        raw_categories = payload.get("categories")
        if not isinstance(raw_categories, dict) or not raw_categories:
            raise FontPolicyError("font policy requires a non-empty categories object")
        categories: dict[str, FontCategory] = {}
        for name, spec in raw_categories.items():
            if not isinstance(spec, dict):
                raise FontPolicyError(f"font policy category {name!r} must be an object")
            raw_candidates = spec.get("candidates")
            if not isinstance(raw_candidates, list) or not raw_candidates:
                raise FontPolicyError(
                    f"font policy category {name!r} requires a non-empty candidates list"
                )
            categories[name] = FontCategory(
                name=name,
                description=str(spec.get("description", "")),
                candidates=tuple(
                    FontCandidate.parse(item, category=name) for item in raw_candidates
                ),
                required_script=(
                    str(spec["required_script"])
                    if isinstance(spec.get("required_script"), str)
                    else None
                ),
            )
        return cls(resolved, payload, categories)

    @property
    def category_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.categories))

    def category(self, name: str) -> FontCategory:
        try:
            return self.categories[name]
        except KeyError as error:
            available = ", ".join(self.category_names)
            raise FontResolutionError(
                f"unknown font category {name!r}; available: {available}"
            ) from error

    def search_paths(self) -> tuple[str, ...]:
        raw = self.payload.get("search_paths")
        if not isinstance(raw, dict):
            return ()
        entries = raw.get(_platform_key())
        if not isinstance(entries, list):
            return ()
        return tuple(str(entry) for entry in entries if isinstance(entry, str))

    def install_hint(self) -> str:
        raw = self.payload.get("install_hints")
        if not isinstance(raw, dict):
            return ""
        hints = raw.get(_platform_key())
        if not isinstance(hints, dict):
            return ""
        for manager in ("pacman", "apt", "dnf"):
            if manager in hints and shutil.which(manager):
                return str(hints[manager])
        return str(hints.get("generic", ""))


@lru_cache(maxsize=4)
def load_font_policy(path: Path | None = None) -> FontPolicy:
    return FontPolicy.load(path)


# ---------------------------------------------------------------------------
# Platform discovery
# ---------------------------------------------------------------------------


def _platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    return "linux"


@lru_cache(maxsize=8)
def _font_file_index(search_paths: tuple[str, ...]) -> dict[str, tuple[Path, ...]]:
    """Map lowercased font file name to every matching path, built once per run.

    Font directories are walked a single time rather than once per candidate,
    which keeps resolution cheap even with a long candidate list.

    Ordering is deterministic: earlier policy search paths win, and within one
    root the shallowest then lexicographically first path wins. Filesystem walk
    order must never decide which font a render uses, or the same project would
    render differently on two machines that both have the font installed.
    """
    index: dict[str, list[tuple[int, int, str, Path]]] = {}
    for rank, raw in enumerate(search_paths):
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        try:
            entries = root.rglob("*")
        except OSError:  # pragma: no cover - unreadable font directory
            continue
        for candidate in entries:
            try:
                if not candidate.is_file():
                    continue
            except OSError:  # pragma: no cover - broken symlink
                continue
            if candidate.suffix.lower() not in {".ttf", ".otf", ".ttc", ".otc"}:
                continue
            try:
                depth = len(candidate.relative_to(root).parts)
            except ValueError:  # pragma: no cover - defensive
                depth = len(candidate.parts)
            index.setdefault(candidate.name.lower(), []).append(
                (rank, depth, str(candidate), candidate)
            )
    return {
        name: tuple(item[3] for item in sorted(entries, key=lambda item: item[:3]))
        for name, entries in index.items()
    }


# ---------------------------------------------------------------------------
# Face selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedFont:
    """A concrete, loadable font face."""

    path: Path
    index: int
    family: str
    subfamily: str
    origin: str
    category: str

    def load(self, size: int) -> "ImageFont.FreeTypeFont":
        if not _PILLOW_AVAILABLE:  # pragma: no cover - planning-only install
            raise FontResolutionError("Pillow is required to load a font face")
        return ImageFont.truetype(str(self.path), size=size, index=self.index)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "path": str(self.path),
            "index": self.index,
            "family": self.family,
            "subfamily": self.subfamily,
            "origin": self.origin,
        }


def _face_names(path: Path, index: int) -> tuple[str, str] | None:
    if not _PILLOW_AVAILABLE:  # pragma: no cover - planning-only install
        return None
    try:
        face = ImageFont.truetype(str(path), size=12, index=index)
    except (OSError, ValueError):
        return None
    try:
        family, subfamily = face.getname()
    except (AttributeError, ValueError):  # pragma: no cover - exotic face
        return None
    return str(family or ""), str(subfamily or "")


def select_face(path: Path, family: str | None) -> tuple[int, str, str] | None:
    """Return ``(index, family, subfamily)`` for *family* inside *path*.

    When *family* is ``None`` the first loadable face is used. When *family* is
    given, every face in the collection is enumerated and matched by name so a
    ``.ttc`` never silently yields the wrong language's glyph variants.
    """
    if not path.is_file():
        return None
    if family is None:
        names = _face_names(path, 0)
        return (0, names[0], names[1]) if names else None
    wanted = family.strip().casefold()
    for index in range(MAX_COLLECTION_FACES):
        names = _face_names(path, index)
        if names is None:
            break
        if names[0].strip().casefold() == wanted:
            return index, names[0], names[1]
    return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _bundled_directory(factory_root: Path | None) -> Path:
    return (factory_root or FACTORY_ROOT) / "resources" / "fonts"


def _resolve_explicit(
    configured: str,
    *,
    category: str,
    origin: str,
    family: str | None,
    factory_root: Path | None,
) -> ResolvedFont | None:
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = (factory_root or FACTORY_ROOT) / path
    selected = select_face(path, family)
    if selected is None and family is not None:
        # An operator override that does not contain the preferred family is
        # still honoured; the operator's explicit choice outranks the policy's
        # family preference.
        selected = select_face(path, None)
    if selected is None:
        return None
    index, resolved_family, subfamily = selected
    return ResolvedFont(
        path=path.resolve(),
        index=index,
        family=resolved_family,
        subfamily=subfamily,
        origin=origin,
        category=category,
    )


def resolve_font(
    category: str,
    configured_path: str | None = None,
    factory_root: Path | None = None,
    *,
    policy: FontPolicy | None = None,
    operator_override: str | None = None,
    search_bundled: bool = True,
) -> ResolvedFont:
    """Resolve a loadable font face for *category*.

    Order: ``operator_override`` → ``configured_path`` (from a style config) →
    policy candidates in platform font directories → policy candidates in the
    bundled ``resources/fonts`` directory.

    Raises :class:`FontResolutionError` with actionable install hints when
    nothing usable is found.
    """
    active = policy or load_font_policy()
    spec = active.category(category)
    preferred_family = spec.candidates[0].family if spec.candidates else None
    tried: list[str] = []

    for value, origin in (
        (operator_override, ORIGIN_OPERATOR_OVERRIDE),
        (configured_path, ORIGIN_STYLE_CONFIG),
    ):
        if not value:
            continue
        tried.append(f"{origin}={value}")
        found = _resolve_explicit(
            value,
            category=category,
            origin=origin,
            family=preferred_family,
            factory_root=factory_root,
        )
        if found is not None:
            return found

    # Candidate preference is primary and origin is secondary: for a category
    # whose first candidate *is* the design intent (a bundled display face, for
    # example), a generic system font must not outrank it just for being
    # installed system-wide.
    index = _font_file_index(active.search_paths())
    bundled = _bundled_directory(factory_root) if search_bundled else None
    for candidate in spec.candidates:
        locations: list[tuple[Path, str]] = [
            (path, ORIGIN_PLATFORM) for path in index.get(candidate.file.lower(), ())
        ]
        if bundled is not None:
            bundled_path = bundled / candidate.file
            if bundled_path.is_file():
                locations.append((bundled_path, ORIGIN_BUNDLED))
        for path, origin in locations:
            tried.append(f"{origin}={path}")
            selected = select_face(path, candidate.family)
            if selected is None:
                continue
            face_index, family, subfamily = selected
            return ResolvedFont(
                path=path.resolve(),
                index=face_index,
                family=family,
                subfamily=subfamily,
                origin=origin,
                category=category,
            )

    hint = active.install_hint()
    message = [
        f"no usable font found for category {category!r}",
        "wanted (in order): "
        + ", ".join(
            f"{item.file}" + (f" [{item.family}]" if item.family else "")
            for item in spec.candidates
        ),
        "searched: " + (", ".join(tried) if tried else "(nothing available)"),
    ]
    if hint:
        message.append(f"install fonts with: {hint}")
    raise FontResolutionError("\n".join(message))


def list_available_fonts(
    factory_root: Path | None = None,
    *,
    policy: FontPolicy | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Return the resolved face (or ``None``) for every policy category."""
    active = policy or load_font_policy()
    result: dict[str, dict[str, Any] | None] = {}
    for category in active.category_names:
        try:
            result[category] = resolve_font(
                category, factory_root=factory_root, policy=active
            ).as_dict()
        except FontResolutionError:
            result[category] = None
    return result


# ---------------------------------------------------------------------------
# Bundled font provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundledFontRecord:
    file: str
    family: str
    license_id: str | None
    license_file: str | None
    source_url: str | None
    sha256: str | None
    present: bool
    gaps: tuple[str, ...] = field(default_factory=tuple)


def bundled_font_records(factory_root: Path | None = None) -> tuple[BundledFontRecord, ...]:
    """Read and verify the bundled font provenance manifest.

    Each record is checked against the filesystem: whether the binary is
    actually present, whether its recorded hash still matches, and whether its
    license text is archived alongside it.
    """
    directory = _bundled_directory(factory_root)
    manifest_path = directory / BUNDLED_MANIFEST_NAME
    if not manifest_path.is_file():
        return ()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    entries = payload.get("fonts")
    if not isinstance(entries, list):
        return ()

    records: list[BundledFontRecord] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        file = str(entry.get("file", "")).strip()
        if not file:
            continue
        path = directory / file
        present = path.is_file()
        license_file = entry.get("license_file")
        license_id = entry.get("license_id")
        gaps: list[str] = []
        if not license_id:
            gaps.append("no license_id recorded")
        if not license_file:
            gaps.append("license text is not archived in the repository")
        elif not (directory / str(license_file)).is_file():
            gaps.append(f"recorded license file {license_file} is missing")
        if present:
            recorded_hash = entry.get("sha256")
            if isinstance(recorded_hash, str) and recorded_hash:
                import hashlib

                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != recorded_hash:
                    gaps.append(
                        f"sha256 mismatch: manifest {recorded_hash}, file {actual}"
                    )
        records.append(
            BundledFontRecord(
                file=file,
                family=str(entry.get("family", "")),
                license_id=str(license_id) if license_id else None,
                license_file=str(license_file) if license_file else None,
                source_url=(
                    str(entry["source_url"])
                    if isinstance(entry.get("source_url"), str)
                    else None
                ),
                sha256=(
                    str(entry["sha256"]) if isinstance(entry.get("sha256"), str) else None
                ),
                present=present,
                gaps=tuple(gaps),
            )
        )
    return tuple(records)


def bundled_font_rights_gaps(
    factory_root: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return one entry per *present* bundled font with a rights gap.

    Only fonts actually redistributed by the repository are reported: a font
    recorded but absent creates no redistribution obligation.
    """
    return tuple(
        {"file": record.file, "family": record.family, "gaps": list(record.gaps)}
        for record in bundled_font_records(factory_root)
        if record.present and record.gaps
    )


def undeclared_bundled_fonts(factory_root: Path | None = None) -> tuple[str, ...]:
    """Return font binaries present in ``resources/fonts`` but not declared."""
    directory = _bundled_directory(factory_root)
    if not directory.is_dir():
        return ()
    declared = {record.file for record in bundled_font_records(factory_root)}
    return tuple(
        sorted(
            path.name
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".ttf", ".otf", ".ttc", ".otc"}
            and path.name not in declared
        )
    )


def policy_portability_violations(
    policy: FontPolicy | None = None,
) -> tuple[str, ...]:
    """Return machine-specific paths found in a style-independent policy.

    :class:`FontPolicy` already rejects these at load time; this helper exists
    so ``doctor.py`` can report the same contract over arbitrary style configs.
    """
    active = policy or load_font_policy()
    violations: list[str] = []
    for name, spec in active.categories.items():
        for candidate in spec.candidates:
            if Path(candidate.file).is_absolute():
                violations.append(f"{name}: {candidate.file}")
    return tuple(violations)


def style_config_violations(fonts: dict[str, Any]) -> tuple[str, ...]:
    """Return absolute, machine-specific font paths in a style config block.

    A style config may point at an operator-installed font, but an absolute
    path baked into a committed config makes the workspace non-portable.
    """
    violations: list[str] = []
    for key, value in sorted(fonts.items()):
        if not isinstance(value, str) or not value:
            continue
        if key.endswith("_license") or key.endswith("_note"):
            continue
        if Path(value).expanduser().is_absolute():
            violations.append(f"{key}: {value}")
    return tuple(violations)


def font_report(factory_root: Path | None = None) -> dict[str, Any]:
    """Aggregate font readiness for ``doctor.py`` and pipeline preflight."""
    try:
        policy = load_font_policy()
    except FontPolicyError as error:
        return {"policy_error": str(error)}
    resolved = list_available_fonts(factory_root, policy=policy)
    return {
        "policy_id": str(policy.payload.get("policy_id", "")),
        "policy_path": str(policy.path),
        "resolved": resolved,
        "missing_categories": sorted(
            name for name, value in resolved.items() if value is None
        ),
        "install_hint": policy.install_hint(),
        "bundled_rights_gaps": list(bundled_font_rights_gaps(factory_root)),
        "undeclared_bundled_fonts": list(undeclared_bundled_fonts(factory_root)),
    }


def iter_categories(policy: FontPolicy | None = None) -> Iterable[str]:
    return (policy or load_font_policy()).category_names
