"""In-process, account-scoped read-only catalog adapter for Lumi."""

from __future__ import annotations

import math
from typing import Any

from app.catalog import Catalog
from app.foreground import run_foreground
from app.language_registry import normalize_metadata_locale
from app.models.account_preference import AccountPreference
from app.models.metadata import MetadataLanguageSettings

MAX_LUMI_ITEMS = 18
MAX_LUMI_FAVORITES = 10


class LumiCatalogAdapter:
    """Expose fixed Catalog reads using the authenticated account in Lumi context.

    The Lumi package supplies a context created by the Orchestrator route after
    normal account authentication. No tool argument can override its account ID.
    """

    def __init__(self, catalog: Catalog, *, tool_error: type[Exception]) -> None:
        self._catalog = catalog
        self._tool_error = tool_error

    async def search(
        self,
        context: Any,
        *,
        query: str,
        item_type: str | None,
        limit: int,
        language: str | None,
    ) -> dict[str, Any]:
        user_id = _context_user_id(context)
        try:
            result, history_enabled = await run_foreground(
                self._search,
                self._catalog,
                user_id,
                query,
                item_type,
                limit,
                language,
            )
        except Exception as error:
            raise self._tool_error(
                "The local catalog lookup is unavailable."
            ) from error
        raw_items = result.get("items") if isinstance(result, dict) else None
        items = _compact_items(
            raw_items, max(1, min(10, limit)), include_history=history_enabled
        )
        total = result.get("total", 0) if isinstance(result, dict) else 0
        return {"items": items, "total": _count(total)}

    async def item_detail(
        self,
        context: Any,
        *,
        entity_id: str,
        language: str | None,
    ) -> dict[str, Any]:
        user_id = _context_user_id(context)
        try:
            result, history_enabled = await run_foreground(
                self._item_detail, self._catalog, user_id, entity_id, language
            )
        except Exception as error:
            raise self._tool_error(
                "The local catalog lookup is unavailable."
            ) from error
        if not isinstance(result, dict):
            return {"item": None, "backgroundItem": None, "seasons": []}
        return {
            "item": _compact_item(
                result.get("item"), include_history=history_enabled
            ),
            "backgroundItem": _compact_item(
                result.get("backgroundItem"), include_history=history_enabled
            ),
            "seasons": _compact_items(
                result.get("seasons"), 12, include_history=history_enabled
            ),
        }

    async def home_recommendations(self, context: Any) -> dict[str, Any]:
        return await self._home_read(context, "recommendations")

    async def continue_watching(self, context: Any) -> dict[str, Any]:
        return await self._home_read(context, "continue-watching")

    async def next_up(self, context: Any) -> dict[str, Any]:
        return await self._home_read(context, "next-up")

    async def favorites(self, context: Any) -> dict[str, Any]:
        user_id = _context_user_id(context)
        try:
            result, history_enabled = await run_foreground(
                self._favorites, self._catalog, user_id
            )
        except Exception as error:
            raise self._tool_error(
                "The local favorites lookup is unavailable."
            ) from error
        raw_items = result.get("items") if isinstance(result, dict) else None
        items = _compact_items(
            raw_items, MAX_LUMI_FAVORITES, include_history=history_enabled
        )
        total = result.get("total", 0) if isinstance(result, dict) else 0
        return {"items": items, "total": _count(total)}

    async def _home_read(self, context: Any, section: str) -> dict[str, Any]:
        user_id = _context_user_id(context)
        methods = {
            "recommendations": self._catalog.home_recommendations,
            "continue-watching": self._catalog.home_continue_watching,
            "next-up": self._catalog.home_next_up,
        }
        method = methods[section]
        try:
            result = await run_foreground(self._history_home, user_id, method)
        except Exception as error:
            raise self._tool_error("The local Home lookup is unavailable.") from error
        return {"items": _compact_items(result, MAX_LUMI_ITEMS)}

    def _search(
        self,
        catalog: Catalog,
        user_id: str,
        query: str,
        item_type: str | None,
        limit: int,
        language: str | None,
    ) -> tuple[dict[str, Any], bool]:
        locale = _effective_language(user_id, language)
        result = catalog.search(user_id, query, locale, 1, limit, item_type)
        return result, _watch_history_enabled(user_id)

    def _item_detail(
        self, catalog: Catalog, user_id: str, entity_id: str, language: str | None
    ) -> tuple[dict[str, Any], bool]:
        locale = _effective_language(user_id, language)
        result = catalog.detail(user_id, entity_id, locale, None, "header", 1, 12)
        return result, _watch_history_enabled(user_id)

    def _history_home(self, user_id: str, method) -> list[dict[str, Any]]:
        if not _watch_history_enabled(user_id):
            return []
        locale = _effective_language(user_id, None)
        return method(user_id, locale)

    def _favorites(
        self, catalog: Catalog, user_id: str
    ) -> tuple[dict[str, Any], bool]:
        locale = _effective_language(user_id, None)
        result = catalog.favorites(
            user_id, locale, 1, MAX_LUMI_FAVORITES, "title", "ascending"
        )
        return result, _watch_history_enabled(user_id)


def _watch_history_enabled(user_id: str) -> bool:
    enabled = AccountPreference(user_id).watch_history().get("enabled", True)
    return enabled is True


def _context_user_id(context: Any) -> str:
    user_id = getattr(context, "account_id", None)
    if (
        not isinstance(user_id, str)
        or not user_id
        or len(user_id) > 128
        or user_id != user_id.strip()
    ):
        raise ValueError("An authenticated account context is required")
    return user_id


def _effective_language(user_id: str, requested: str | None) -> str:
    configured = MetadataLanguageSettings().get()
    if requested:
        try:
            normalized = normalize_metadata_locale(requested)
        except (TypeError, ValueError):
            normalized = None
        if normalized in configured:
            return normalized
    preference = AccountPreference(user_id).metadata_language().get("language")
    if preference in configured:
        return preference
    return configured[0] if configured else "en"


def _compact_item(
    value: object, *, include_history: bool = True
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    item_id = value.get("id")
    entity_type = value.get("type")
    metadata = value.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    title = _bounded_text(value.get("name") or metadata.get("title"), 200)
    if (
        not isinstance(item_id, str)
        or not item_id
        or len(item_id) > 128
        or not isinstance(entity_type, str)
        or not title
    ):
        return None
    compact: dict[str, Any] = {"id": item_id, "type": entity_type, "title": title}
    for key, limit in (
        ("overview", 800),
        ("description", 800),
        ("year", 16),
        ("date", 32),
        ("releaseDate", 32),
        ("album", 200),
        ("albumArtist", 200),
    ):
        text = _bounded_text(metadata.get(key), limit)
        if text:
            compact[key] = text
    for key, maximum in (("communityRating", 10), ("runtimeMinutes", 100_000)):
        number = metadata.get(key)
        if (
            type(number) is int
            and 0 <= number <= maximum
            or type(number) is float
            and math.isfinite(number)
            and 0 <= number <= maximum
        ):
            compact[key] = number
    for key in ("genres", "artists", "tags"):
        values = metadata.get(key)
        if isinstance(values, list):
            items = [
                text
                for item in values[:8]
                if (text := _bounded_text(item, 80)) is not None
            ]
            if items:
                compact[key] = items
    state = value.get("userState")
    if isinstance(state, dict):
        selected: dict[str, Any] = {}
        if isinstance(state.get("favorite"), bool):
            selected["favorite"] = state["favorite"]
        if include_history:
            if isinstance(state.get("played"), bool):
                selected["played"] = state["played"]
            count = state.get("playCount")
            if type(count) is int and 0 <= count <= 1_000_000_000:
                selected["playCount"] = count
            for key in ("positionSeconds", "durationSeconds"):
                seconds = state.get(key)
                if (
                    isinstance(seconds, (int, float))
                    and not isinstance(seconds, bool)
                    and math.isfinite(seconds)
                    and 0 <= seconds <= 100_000_000
                ):
                    selected[key] = seconds
            last_played = _bounded_text(state.get("lastPlayedAt"), 40)
            if last_played:
                selected["lastPlayedAt"] = last_played
        if selected:
            compact["userState"] = selected
    return compact


def _compact_items(
    values: object, limit: int, *, include_history: bool = True
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values[: max(0, limit)]:
        item = _compact_item(value, include_history=include_history)
        if item is None:
            continue
        key = (item["type"], item["id"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] if normalized else None


def _count(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else 0
