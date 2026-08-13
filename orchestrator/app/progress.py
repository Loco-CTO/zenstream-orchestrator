"""Monotonic, whole-invocation progress for background jobs.

This module deliberately only translates the progress values already emitted by
jobs.  It does not schedule work, claim items, or alter job state transitions.
"""

from __future__ import annotations

import time
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PROGRESS_TOTAL = 10_000


@dataclass(frozen=True)
class ProgressRange:
    start: int
    end: int


def sanitize_progress_item(value: Any, *, limit: int = 160) -> str | None:
    """Return a compact, safe label suitable for an administrator status line."""
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    if not text:
        return None
    # Absolute paths are intentionally reduced to their final relative-looking
    # component.  Progress is observability, not a filesystem disclosure API.
    try:
        path = Path(text)
        if path.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", text):
            text = re.split(r"[\\/]", text.rstrip("\\/"))[-1]
    except (TypeError, ValueError):
        pass
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def format_progress_message(
    label: str,
    *,
    item: Any = None,
    current: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    detail: str | None = None,
) -> str:
    """Format a stable human-readable compatibility message."""
    parts = [sanitize_progress_item(label, limit=96) or "Working"]
    safe_item = sanitize_progress_item(item)
    if safe_item:
        parts.append(safe_item)
    if current is not None and total is not None and total >= 0:
        suffix = f"{max(0, int(current))}/{max(0, int(total))}"
        if unit:
            suffix += f" {sanitize_progress_item(unit, limit=48) or ''}".rstrip()
        parts.append(suffix)
    elif detail:
        parts.append(sanitize_progress_item(detail, limit=96) or "")
    return " · ".join(part for part in parts if part)


def resolve_progress_item(db, entity_id: str | None, fallback: Any = None) -> str:
    """Resolve a catalog title with a safe filename/provider fallback."""
    fallback_text = str(fallback) if fallback else None
    if fallback_text:
        fallback_text = re.split(r"[\\/]", fallback_text.rstrip("\\/"))[-1]
    fallback_label = sanitize_progress_item(fallback_text)
    if not entity_id or db is None:
        return fallback_label or "item"
    try:
        row = db.execute(
            "SELECT entity_type,parent_id,season_number,episode_number,relative_path FROM library_entities WHERE id=?",
            (entity_id,),
        )
        if not row:
            return fallback_label or str(entity_id)
        entity_type, parent_id, season_number, episode_number, relative_path = row[0]
        title = None
        projection = db.execute(
            "SELECT payload FROM catalog_item_projection WHERE entity_id=? ORDER BY locale LIMIT 1",
            (entity_id,),
        )
        if projection:
            import json

            try:
                title = json.loads(projection[0][0]).get("title")
            except (TypeError, ValueError, json.JSONDecodeError):
                title = None
        if entity_type == "episode" and parent_id:
            series_title = None
            season_row = db.execute(
                "SELECT parent_id FROM library_entities WHERE id=?", (parent_id,)
            )
            series_id = season_row[0][0] if season_row else None
            if series_id:
                series_projection = db.execute(
                    "SELECT payload FROM catalog_item_projection WHERE entity_id=? ORDER BY locale LIMIT 1",
                    (series_id,),
                )
                if series_projection:
                    import json

                    try:
                        series_title = json.loads(series_projection[0][0]).get("title")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        series_title = None
            episode_title = sanitize_progress_item(title or fallback_label)
            if series_title:
                number = f"S{int(season_number):02d}E{int(episode_number):02d}" if season_number is not None and episode_number is not None else None
                return sanitize_progress_item(" — ".join(value for value in (series_title, number, episode_title) if value)) or "episode"
        relative_label = (
            re.split(r"[\\/]", str(relative_path).rstrip("\\/"))[-1]
            if relative_path
            else None
        )
        return sanitize_progress_item(title or fallback_label or relative_label or entity_id) or "item"
    except Exception:
        return fallback_label or sanitize_progress_item(entity_id) or "item"


class ProgressReporter:
    """Thread-safe latest-snapshot reporter for concurrent background work."""

    def __init__(self, emit: Callable[..., None], *, unit: str, min_interval: float = 0.5):
        self.emit = emit
        self.unit = unit
        self.min_interval = min_interval
        self.lock = threading.RLock()
        self.settled = 0
        self.failed = 0
        self.total: int | None = None
        self.phase = "processing"
        self.label = "Working"
        self.active: list[str] = []
        self._last_emit = 0.0

    def stage(self, phase: str, label: str, *, total: int | None = None, force: bool = True) -> None:
        with self.lock:
            self.phase, self.label = phase, label
            if total is not None:
                self.total = max(self.settled, int(total))
            self._publish_locked(force=force)

    def start(self, item: Any) -> None:
        with self.lock:
            safe = sanitize_progress_item(item) or "item"
            self.active = [value for value in self.active if value != safe]
            self.active.append(safe)
            self._publish_locked(force=False)

    def settle(self, item: Any = None, *, failed: bool = False, force: bool = False) -> None:
        with self.lock:
            safe = sanitize_progress_item(item)
            if safe:
                self.active = [value for value in self.active if value != safe]
            self.settled += 1
            if failed:
                self.failed += 1
            self._publish_locked(force=force)

    def finish(self, *, failed: bool = False) -> None:
        with self.lock:
            self._publish_locked(force=True, terminal=failed)

    def _publish_locked(self, *, force: bool, terminal: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < self.min_interval:
            return
        self._last_emit = now
        item = self.active[-1] if self.active else None
        detail = f"{self.failed} failed" if self.failed else None
        message = format_progress_message(
            self.label,
            item=item,
            current=self.settled if self.total is not None else None,
            total=self.total,
            unit=self.unit,
            detail=detail,
        )
        values = {
            "message": message,
            "progress_current": self.settled,
            "progress_total": self.total if self.total is not None else 0,
            "progress_phase": self.phase,
            "progress_label": self.label,
            "progress_stage_current": self.settled,
            "progress_stage_total": self.total,
            "progress_stage_unit": self.unit,
            "progress_current_item": item,
        }
        if terminal:
            values["progress_stage_current"] = self.settled
        self.emit(**values)


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))


class WholeJobProgress:
    """Translate existing phase-local counters into a monotonic fixed scale."""

    _MIN_DELTA = 10  # 0.1% in the 0..10,000 scale.
    _MIN_INTERVAL = 0.5

    def __init__(self, kind: str):
        self.kind = kind
        self.current = 0
        self._phase_current = 0
        self._phase_total: float | None = None
        self.phase = ProgressRange(0, PROGRESS_TOTAL)
        self.phase_name = "Starting"
        self._phase_selected = False
        self._last_persisted = -1
        self._last_persisted_at = 0.0

    def _range_for_message(self, message: str | None) -> tuple[str, ProgressRange]:
        value = (message or "").casefold()
        kind = self.kind
        if kind.startswith("scan:"):
            library_type = kind.partition(":")[2]
            if library_type == "music":
                if any(token in value for token in ("discover", "enumerat")):
                    return "discovery", ProgressRange(0, 1_000)
                if any(token in value for token in ("resolv", "metadata", "locale")):
                    return "metadata", ProgressRange(4_500, 8_000)
                if any(
                    token in value
                    for token in ("reconcil", "pruning", "refreshing", "queueing")
                ):
                    return "finalization", ProgressRange(8_000, 10_000)
                return "inventory", ProgressRange(1_000, 4_500)
            kind = "scan"
        if kind in {"scan", "reconcile", "library_scan"}:
            if any(token in value for token in ("discover", "enumerat")):
                return "discovery", ProgressRange(0, 1_000)
            if "reconciling changed" in value:
                return "processing", ProgressRange(1_000, 8_000)
            if any(
                token in value
                for token in (
                    "reconcil",
                    "pruning",
                    "refreshing catalog",
                    "pruning local",
                    "queueing",
                )
            ):
                return "finalization", ProgressRange(8_000, 10_000)
            return "processing", ProgressRange(1_000, 8_000)
        if kind == "collection_rebuild":
            if any(token in value for token in ("discover", "enumerat", "deriving")):
                return "discovery", ProgressRange(0, 1_000)
            if any(token in value for token in ("clean", "refresh", "prun", "publish")):
                return "finalization", ProgressRange(9_000, 10_000)
            return "processing", ProgressRange(1_000, 9_000)
        if kind in {"metadata_missing", "metadata_refresh"}:
            if any(token in value for token in ("discover", "repairing", "starting")):
                return "discovery", ProgressRange(0, 1_000)
            if "extracting fallback artwork" in value:
                return "artwork", ProgressRange(7_000, 9_000)
            if any(token in value for token in ("complete", "checked", "repaired")):
                return "finalization", ProgressRange(9_000, 10_000)
            return "metadata", ProgressRange(1_000, 7_000)
        if kind == "trickplay_extract":
            if "extracted" in value:
                return "extraction", ProgressRange(1_000, 9_500)
            if any(token in value for token in ("current", "complete", "terminat")):
                return "finalization", ProgressRange(9_500, 10_000)
            return "preparation", ProgressRange(0, 1_000)
        if kind == "intro_outro_detect":
            if "fingerprint" in value:
                return "fingerprinting", ProgressRange(1_000, 8_000)
            if any(
                token in value
                for token in ("marker", "detected", "current", "complete")
            ):
                return "finalization", ProgressRange(8_000, 10_000)
            return "preparation", ProgressRange(0, 1_000)
        if kind == "metadata_cleanup":
            if any(token in value for token in ("clean", "orphan")):
                return "cleanup", ProgressRange(500, 9_500)
            return "preparation", ProgressRange(0, 500)
        return "processing", ProgressRange(0, PROGRESS_TOTAL)

    def _range_for_phase(self, phase: str | None) -> tuple[str, ProgressRange] | None:
        if not phase:
            return None
        ranges = {
            "preparation": ProgressRange(0, 1_000),
            "discovery": ProgressRange(0, 1_000),
            "inventory": ProgressRange(1_000, 4_500),
            "processing": ProgressRange(1_000, 8_000),
            "metadata": ProgressRange(1_000, 7_000),
            "artwork": ProgressRange(7_000, 9_000),
            "extraction": ProgressRange(1_000, 9_500),
            "fingerprinting": ProgressRange(1_000, 8_000),
            "comparison": ProgressRange(8_000, 9_500),
            "cleanup": ProgressRange(500, 9_500),
            "finalization": ProgressRange(9_000, 10_000),
        }
        phase_key = str(phase).casefold()
        selected = ranges.get(phase_key)
        return (phase_key, selected) if selected else None

    def _select_phase(self, message: str | None) -> None:
        name, phase = self._range_for_message(message)
        self.phase_name = name
        # A later phase may advance the floor, but never move it backwards.
        if not self._phase_selected:
            self.phase = phase
            self._phase_selected = True
        elif phase.start >= self.phase.start and phase.end >= self.phase.end:
            self.phase = ProgressRange(
                phase.start,
                phase.end,
            )

    def _mapped(self, current: Any, total: Any) -> int:
        try:
            numerator = float(current)
            denominator = float(total)
        except (TypeError, ValueError):
            return self.current
        if denominator <= 0:
            return self.current
        ratio = _clamp(round(numerator / denominator * 10_000), 0, 10_000) / 10_000
        return round(self.phase.start + (self.phase.end - self.phase.start) * ratio)

    def apply(self, values: dict[str, Any]) -> dict[str, Any]:
        """Return storage values with progress translated and throttled."""
        result = dict(values)
        message = result.get("message")
        explicit_phase = result.get("progress_phase")
        if explicit_phase:
            selected = self._range_for_phase(str(explicit_phase))
            if selected:
                self.phase_name, self.phase = selected
                self._phase_selected = True
        elif message is not None:
            self._select_phase(str(message))

        if result.get("state") == "completed":
            self.current = PROGRESS_TOTAL
            result["progress_current"] = PROGRESS_TOTAL
            result["progress_total"] = PROGRESS_TOTAL
            self._last_persisted = PROGRESS_TOTAL
            self._last_persisted_at = time.monotonic()
            return result

        has_current = "progress_current" in result
        has_total = "progress_total" in result
        denominator_changed = False
        if has_total:
            try:
                next_total = float(result["progress_total"])
            except (TypeError, ValueError):
                next_total = None
            denominator_changed = next_total != self._phase_total
            self._phase_total = next_total
            # A newly announced denominator starts a new phase-local counter.
            # Later updates commonly provide only progress_current.
            if not has_current:
                self._phase_current = 0
        if has_current:
            try:
                self._phase_current = float(result["progress_current"])
            except (TypeError, ValueError):
                self._phase_current = 0
        if has_current or has_total:
            mapped = self._mapped(
                self._phase_current,
                self._phase_total,
            )
            self.current = max(self.current, mapped)
            now = time.monotonic()
            force = bool(
                result.get("state") in {"running", "failed", "terminated"}
                or denominator_changed
            )
            if (
                not force
                and self._last_persisted >= 0
                and (
                    self.current - self._last_persisted < self._MIN_DELTA
                    and now - self._last_persisted_at < self._MIN_INTERVAL
                )
            ):
                result.pop("progress_current", None)
                result.pop("progress_total", None)
            else:
                result["progress_current"] = self.current
                result["progress_total"] = PROGRESS_TOTAL
                self._last_persisted = self.current
                self._last_persisted_at = now
        elif self._last_persisted < 0:
            result["progress_current"] = self.current
            result["progress_total"] = PROGRESS_TOTAL
            self._last_persisted = self.current
            self._last_persisted_at = time.monotonic()
        return result
