from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pycountry

ARTWORK_CATEGORIES = ("Primary", "Backdrop", "Logo", "Banner")
ARTWORK_CATEGORY_SET = frozenset(ARTWORK_CATEGORIES)


def language_family(value: str | None) -> str:
    return str(value or "").lower().replace("_", "-").split("-", 1)[0]


def locale_variants(language: str, available: Iterable[str]) -> list[str]:
    wanted = str(language or "").lower()
    exact = [value for value in available if str(value).lower() == wanted]
    family = [
        value
        for value in available
        if language_family(value) == language_family(language) and value not in exact
    ]
    return exact + sorted(family, key=lambda value: str(value).lower())


def fallback_tiers(
    requested: str, original: str | None, *, media: bool, include_english: bool = True
) -> list[str]:
    values: list[str] = [requested]
    if media:
        values.append("")
    if include_english:
        values.append("en")
    values.append(original)
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        normalized = str(value)
        if normalized not in result:
            result.append(normalized)
    return result


def nonempty(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def is_language_code_placeholder(value: Any) -> bool:
    """Return whether a provider supplied a language code instead of text."""
    if not isinstance(value, str):
        return False
    raw = value.strip().replace("_", "-")
    if not raw or raw != raw.lower():
        return False
    parts = raw.split("-")
    if len(parts) > 2 or len(parts[0]) not in {2, 3}:
        return False
    if len(parts) == 2 and len(parts[1]) not in {2, 3}:
        return False
    language = None
    for field in ("alpha_2", "alpha_3", "bibliographic"):
        language = pycountry.languages.get(**{field: parts[0]})
        if language:
            break
    return language is not None


def usable_text(field: str, value: Any) -> bool:
    return nonempty(value) and not (
        field in {"overview", "description"} and is_language_code_placeholder(value)
    )


@dataclass(frozen=True)
class MetadataImage:
    type: str
    url: str
    language: str | None = None
    provider: str | None = None
    score: float = 0
    width: int = 0
    height: int = 0


def image_language_rank(
    image: dict,
    requested: str,
    original: str | None,
    *,
    include_english: bool = True,
) -> int:
    language = str(image.get("language") or "").lower()
    requested = str(requested or "").lower()
    if language == requested:
        return 0
    if language and language_family(language) == language_family(requested):
        return 1
    if not language:
        return 2
    if include_english and language == "en":
        return 3
    if include_english and language_family(language) == "en":
        return 4
    if original and language == str(original).lower():
        return 5
    if original and language_family(language) == language_family(original):
        return 6
    return 99


def choose_artwork(
    images: Iterable[dict],
    requested: str,
    image_type: str,
    original: str | None,
    providers: list[str],
    *,
    include_english: bool = True,
) -> dict | None:
    ranked = rank_artwork_candidates(
        images,
        requested,
        image_type,
        original,
        providers,
        include_english=include_english,
    )
    return ranked[0] if ranked else None


def rank_artwork_candidates(
    images: Iterable[dict],
    requested: str,
    image_type: str,
    original: str | None,
    providers: list[str],
    *,
    include_english: bool = True,
) -> list[dict]:
    """Return provider-ordered artwork candidates for one locale/category.

    TMDB and TVDB already return each artwork category in their preferred
    order.  ZenStream only applies locale and provider precedence; it must not
    replace the provider's ordering with local score, dimension, or timestamp
    sorting.
    """
    if image_type not in ARTWORK_CATEGORY_SET:
        raise ValueError(
            f"Unsupported image type '{image_type}'. Expected one of: {', '.join(ARTWORK_CATEGORIES)}"
        )
    provider_rank = {provider: index for index, provider in enumerate(providers)}
    best_by_url: dict[str, tuple[int, int, int, dict]] = {}
    for index, image in enumerate(images):
        if not isinstance(image, dict) or image.get("type") != image_type:
            continue
        language_rank = image_language_rank(
            image,
            requested,
            original,
            include_english=include_english,
        )
        if language_rank >= 99:
            continue
        url = image.get("url")
        if not isinstance(url, str) or not url:
            continue
        candidate = (
            language_rank,
            provider_rank.get(image.get("provider"), 99),
            index,
            image,
        )
        previous = best_by_url.get(url)
        if previous is None or candidate[:3] < previous[:3]:
            best_by_url[url] = candidate
    return [
        candidate[3]
        for candidate in sorted(best_by_url.values(), key=lambda value: value[:3])
    ]
