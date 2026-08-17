"""ZenStream's canonical language registry.

Provider APIs and media containers use a mixture of BCP-47, ISO-639-1,
ISO-639-2, and provider-specific language identifiers.  The registry keeps
the value stored by ZenStream stable while allowing adapters to translate
provider and track values at the boundary.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from langcodes import Language, LanguageTagError, standardize_tag, tag_is_valid

SUPPORTED_LANGUAGE_CODES = (
    "af",
    "am",
    "ar",
    "az",
    "be",
    "bg",
    "bn",
    "bs",
    "ca",
    "cs",
    "cy",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "eu",
    "fa",
    "fi",
    "fil",
    "fr",
    "ga",
    "gl",
    "he",
    "hi",
    "hr",
    "hu",
    "id",
    "is",
    "it",
    "ja",
    "ka",
    "kk",
    "km",
    "ko",
    "lt",
    "lv",
    "mk",
    "ml",
    "mn",
    "mr",
    "ms",
    "my",
    "nb",
    "ne",
    "nl",
    "nn",
    "no",
    "pa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sq",
    "sr",
    "sv",
    "sw",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "ur",
    "uz",
    "vi",
    "zu",
    "en-GB",
    "en-US",
    "es-419",
    "es-MX",
    "fr-CA",
    "pt-BR",
    "pt-PT",
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "sr-Cyrl",
    "sr-Latn",
)

SUPPORTED_LANGUAGE_SET = frozenset(SUPPORTED_LANGUAGE_CODES)

# Provider and legacy tags that are not reliably interpreted by generic
# BCP-47 parsers.  The values are canonical ZenStream values.
LANGUAGE_ALIASES = {
    "zhtw": "zh-TW",
    "zht": "zh-TW",
    "jap": "ja",
}


@dataclass(frozen=True)
class SupportedLanguage:
    code: str
    label: str
    metadata: bool = True
    tracks: bool = True


def _display_language(value: object) -> str:
    """Choose a supported language used to render registry labels."""
    raw = str(value or "en").strip().replace("_", "-")
    alias = LANGUAGE_ALIASES.get(raw.casefold())
    if alias:
        raw = alias
    try:
        normalized = standardize_tag(raw)
    except (LanguageTagError, LookupError, TypeError, ValueError):
        return "en"
    if normalized.casefold() == "ja-jp":
        normalized = "ja"
    return normalized if normalized in SUPPORTED_LANGUAGE_SET else "en"


def _display_name(code: str, display_language: object = "en") -> str:
    try:
        language = Language.get(code)
        display_name = language.display_name(_display_language(display_language))
        autonym = language.autonym()
        if _label_key(display_name) == _label_key(autonym):
            return display_name
        return f"{display_name} ({autonym})"
    except (LookupError, ValueError):
        return code


def _label_key(value: str) -> str:
    """Compare labels without treating full-width punctuation as distinct."""
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    return normalized.replace(" (", "(").replace("( ", "(").replace(" )", ")")


SUPPORTED_LANGUAGES = tuple(
    SupportedLanguage(code, _display_name(code)) for code in SUPPORTED_LANGUAGE_CODES
)


def language_options(display_language: object = "en") -> list[dict[str, object]]:
    """Return registry options named in the requested display language."""
    display_language = _display_language(display_language)
    return [
        {
            "value": language.code,
            "label": _display_name(language.code, display_language),
            "metadata": language.metadata,
            "tracks": language.tracks,
        }
        for language in SUPPORTED_LANGUAGES
    ]


def language_label(value: object, display_language: object = "en") -> str:
    try:
        return _display_name(normalize_language(value), display_language)
    except (StopIteration, ValueError):
        return str(value or "")


def _canonicalize(value: object) -> str:
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        raise ValueError("Language must be a non-empty BCP-47 tag.")
    alias = LANGUAGE_ALIASES.get(raw.casefold())
    if alias:
        return alias
    try:
        normalized = standardize_tag(raw)
    except (LanguageTagError, LookupError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid language tag '{value}'.") from error
    if not normalized or not tag_is_valid(normalized):
        raise ValueError(f"Invalid language tag '{value}'.")
    return normalized


def normalize_language(value: object, *, allow_unsupported: bool = False) -> str:
    """Canonicalize one language and enforce ZenStream's curated list."""
    normalized = _canonicalize(value)
    # The metadata registry intentionally treats the provider's ja-JP spelling
    # as Japanese, while meaningful regional variants remain distinct.
    if normalized.casefold() == "ja-jp":
        normalized = "ja"
    if normalized not in SUPPORTED_LANGUAGE_SET and not allow_unsupported:
        raise ValueError(f"Unsupported ZenStream language '{value}'.")
    return normalized


def normalize_metadata_locale(value: object) -> str:
    return normalize_language(value)


def normalize_track_language(value: object) -> str | None:
    """Normalize an embedded/sidecar track tag, dropping unknown markers."""
    raw = str(value or "").strip()
    if not raw or raw.casefold() in {"und", "unknown", "undefined"}:
        return None
    try:
        return normalize_language(raw)
    except ValueError:
        return None


def language_family(value: object) -> str:
    normalized = normalize_language(value, allow_unsupported=True)
    return normalized.split("-", 1)[0].casefold()


def provider_language(provider: str, locale: str) -> str:
    """Return a provider request hint while retaining canonical storage."""
    canonical = normalize_metadata_locale(locale)
    if provider == "tvdb":
        special = {
            "zh-TW": "zhtw",
            "zh-CN": "zho",
            "zh-HK": "zho",
        }
        if canonical in special:
            return special[canonical]
        try:
            return Language.get(canonical).to_alpha3()
        except (LookupError, ValueError):
            return canonical
    if provider == "tmdb":
        return canonical
    return canonical
