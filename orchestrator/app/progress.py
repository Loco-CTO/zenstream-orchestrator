"""Monotonic, whole-invocation progress for background jobs.

This module deliberately only translates the progress values already emitted by
jobs.  It does not schedule work, claim items, or alter job state transitions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

PROGRESS_TOTAL = 10_000


@dataclass(frozen=True)
class ProgressRange:
    start: int
    end: int


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
        if message is not None:
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
        if has_total:
            try:
                self._phase_total = float(result["progress_total"])
            except (TypeError, ValueError):
                self._phase_total = None
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
            force = bool(result.get("state") in {"running", "failed", "terminated"})
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
