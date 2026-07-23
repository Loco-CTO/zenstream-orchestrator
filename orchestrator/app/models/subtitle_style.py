from __future__ import annotations

import re


SUPPORTED_LOCALES = {"en", "ja"}
DEFAULT_SUBTITLE_STYLE = {
    "fontFamily": "sans",
    "bold": False,
    "textScale": 100,
    "fontColor": "#ffffff",
    "borderSize": 0,
    "borderColor": "#000000",
    "backgroundColor": "#000000",
    "backgroundOpacity": 0,
}
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_subtitle_style(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Subtitle style must be an object.")
    result = dict(DEFAULT_SUBTITLE_STYLE)
    for key in result:
        if key in value:
            result[key] = value[key]
    if (
        not isinstance(result["textScale"], (int, float))
        or not 50 <= result["textScale"] <= 200
    ):
        raise ValueError("textScale must be between 50 and 200.")
    if (
        not isinstance(result["borderSize"], (int, float))
        or not 0 <= result["borderSize"] <= 8
    ):
        raise ValueError("borderSize must be between 0 and 8.")
    if (
        not isinstance(result["backgroundOpacity"], (int, float))
        or not 0 <= result["backgroundOpacity"] <= 100
    ):
        raise ValueError("backgroundOpacity must be between 0 and 100.")
    for key in ("fontColor", "borderColor", "backgroundColor"):
        if not isinstance(result[key], str) or not _HEX_COLOR.fullmatch(result[key]):
            raise ValueError(f"{key} must be a six-digit hex color.")
    if result["fontFamily"] not in {"sans", "serif", "mono"}:
        raise ValueError("fontFamily must be sans, serif, or mono.")
    if not isinstance(result["bold"], bool):
        raise ValueError("bold must be a boolean.")
    return result
