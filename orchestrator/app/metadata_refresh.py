from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.library import JobTerminated
from app.logging_config import get_logger
from app.metadata_services import MetadataIngestService, metadata_task_results
from app.models.metadata import MetadataLanguageSettings, MetadataRefreshSettings
from app.providers import ProviderError

logger = get_logger("metadata_refresh")
SUPPORTED_PROVIDERS = {"tmdb", "tvdb"}
PROVIDER_PRIORITIES = {
    "movie": ("tmdb", "tvdb"),
    "series": ("tvdb", "tmdb"),
    "season": ("tvdb", "tmdb"),
    "episode": ("tvdb", "tmdb"),
}
DATE_NAME_RE = re.compile(r"^\d{4}(?:[-/.]\d{1,2}){0,2}$")


def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_exceeded(value: str | None, days: int, current: datetime) -> bool:
    if days == -1:
        return False
    parsed = _utc(value)
    return parsed is None or current - parsed >= timedelta(days=days)


def _usable(value) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _patterns(value: str) -> list[str]:
    return [
        item.strip().casefold()
        for item in re.split(r"[|,\r\n]+", value)
        if item.strip()
    ]


def _ready_path(value: str | None) -> bool:
    if not value:
        return False
    try:
        path = Path(value)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


class MetadataRefreshJob:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self._tables = None
        self._columns: dict[str, set[str]] = {}

    def _table_names(self) -> set[str]:
        if self._tables is None:
            self._tables = {
                str(row[0])
                for row in self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        return self._tables

    def _table_columns(self, table: str) -> set[str]:
        if table not in self._columns:
            try:
                self._columns[table] = {
                    str(row[1])
                    for row in self.db.execute(f"PRAGMA table_info({table})")
                }
            except Exception:
                self._columns[table] = set()
        return self._columns[table]

    def _entities(self) -> list[dict]:
        if "library_entities" not in self._table_names():
            return []
        columns = self._table_columns("library_entities")
        selected = ["id", "entity_type"]
        selected.extend(
            name if name in columns else f"NULL AS {name}"
            for name in ("parent_id", "created_at", "relative_path")
        )
        rows = self.db.execute(
            "SELECT "
            + ",".join(selected)
            + " FROM library_entities WHERE entity_type IN ('movie','series','season','episode')"
            " ORDER BY entity_type,id"
        )
        return [
            {
                "id": str(row[0]),
                "type": str(row[1]),
                "parentId": row[2],
                "createdAt": row[3],
                "path": str(row[4] or ""),
            }
            for row in rows
        ]

    def _identities(self, entity_id: str, entity_type: str) -> list[dict]:
        if "entity_provider_ids" not in self._table_names():
            return []
        rows = self.db.execute(
            "SELECT provider,provider_id,identifier_type,is_primary "
            "FROM entity_provider_ids WHERE entity_id=? AND provider IN ('tmdb','tvdb') "
            "ORDER BY provider,provider_id",
            (entity_id,),
        )
        values = [
            {
                "provider": str(row[0]),
                "id": str(row[1]),
                "type": str(row[2] or entity_type),
                "primary": bool(row[3]),
            }
            for row in rows
            if row[1]
            and str(row[2] or entity_type)
            in {
                entity_type,
                "series" if entity_type in {"season", "episode"} else entity_type,
            }
        ]
        priorities = PROVIDER_PRIORITIES.get(entity_type, ())
        return sorted(
            values,
            key=lambda value: (
                priorities.index(value["provider"])
                if value["provider"] in priorities
                else len(priorities),
                value["provider"],
                value["id"],
            ),
        )

    def _state(self, entity_id: str) -> tuple[str | None, str | None]:
        if "metadata_refresh_state" not in self._table_names():
            return None, None
        rows = self.db.execute(
            "SELECT last_attempted_at,last_completed_at FROM metadata_refresh_state WHERE entity_id=?",
            (entity_id,),
        )
        return (rows[0][0], rows[0][1]) if rows else (None, None)

    def _state_update(
        self,
        entity_ids: list[str],
        *,
        attempted: str | None = None,
        completed: str | None = None,
        error: str | None = None,
    ) -> None:
        if "metadata_refresh_state" not in self._table_names() or not entity_ids:
            return
        for entity_id in entity_ids:
            self.db.execute(
                "INSERT INTO metadata_refresh_state(entity_id,last_attempted_at,last_completed_at,last_error) "
                "VALUES(?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
                "last_attempted_at=COALESCE(excluded.last_attempted_at,metadata_refresh_state.last_attempted_at), "
                "last_completed_at=COALESCE(excluded.last_completed_at,metadata_refresh_state.last_completed_at), "
                "last_error=excluded.last_error",
                (entity_id, attempted, completed, error),
            )

    def _projection_values(self, entity_id: str, locales: list[str]) -> list[dict]:
        if "catalog_item_projection" not in self._table_names():
            return []
        values = []
        placeholders = ",".join("?" for _ in locales)
        rows = self.db.execute(
            "SELECT payload FROM catalog_item_projection WHERE entity_id=? "
            f"AND locale IN ({placeholders})",
            [entity_id, *locales],
        )
        for row in rows:
            try:
                value = json.loads(row[0] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                values.append(value)
        return values

    def _cached_values(
        self, entity_type: str, identities: list[dict], locales: list[str]
    ) -> list[dict]:
        if "metadata_cache" not in self._table_names():
            return []
        values = []
        for identity in identities:
            placeholders = ",".join("?" for _ in locales)
            rows = self.db.execute(
                "SELECT payload FROM metadata_cache WHERE provider=? AND entity_type=? "
                "AND provider_id=? AND locale IN (" + placeholders + ")",
                [identity["provider"], entity_type, identity["id"], *locales],
            )
            for row in rows:
                try:
                    value = json.loads(row[0] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    values.append(value)
        return values

    def _metadata_bucket_due(
        self, entity_type: str, identities: list[dict], locales: list[str], days: int
    ) -> bool:
        if days == -1:
            return True
        if "metadata_cache" not in self._table_names():
            return True
        rows = []
        placeholders = ",".join("?" for _ in locales)
        for identity in identities:
            rows.extend(
                self.db.execute(
                    "SELECT fetched_at FROM metadata_cache WHERE provider=? AND entity_type=? "
                    "AND provider_id=? AND locale IN (" + placeholders + ")",
                    [identity["provider"], entity_type, identity["id"], *locales],
                )
            )
        if not rows:
            return True
        current = datetime.now(timezone.utc)
        return any(_age_exceeded(row[0], days, current) for row in rows)

    def _artwork_available(
        self,
        entity_id: str,
        entity_type: str,
        identities: list[dict],
        locales: list[str],
        image_type: str,
    ) -> bool:
        if "catalog_artwork_selection" in self._table_names():
            placeholders = ",".join("?" for _ in locales)
            rows = self.db.execute(
                "SELECT local_path FROM catalog_artwork_selection WHERE entity_id=? "
                "AND image_type=? AND locale IN (" + placeholders + ")",
                [entity_id, image_type, *locales],
            )
            if any(_ready_path(row[0]) for row in rows):
                return True
        if "metadata_images" not in self._table_names():
            return False
        image_locales = list(dict.fromkeys([*locales, ""]))
        for identity in identities:
            placeholders = ",".join("?" for _ in image_locales)
            rows = self.db.execute(
                "SELECT local_path FROM metadata_images WHERE provider=? AND entity_type=? "
                "AND provider_id=? AND image_type=? AND locale IN ("
                + placeholders
                + ")",
                [
                    identity["provider"],
                    entity_type,
                    identity["id"],
                    image_type,
                    *image_locales,
                ],
            )
            if any(_ready_path(row[0]) for row in rows):
                return True
        return False

    def _artwork_bucket_due(
        self,
        entity_type: str,
        identities: list[dict],
        locales: list[str],
        image_type: str,
        days: int,
    ) -> bool:
        if days == -1:
            return True
        if "metadata_images" not in self._table_names():
            return True
        image_locales = list(dict.fromkeys([*locales, ""]))
        placeholders = ",".join("?" for _ in image_locales)
        rows = []
        for identity in identities:
            rows.extend(
                self.db.execute(
                    "SELECT fetched_at FROM metadata_images WHERE provider=? AND entity_type=? "
                    "AND provider_id=? AND image_type=? AND locale IN ("
                    + placeholders
                    + ")",
                    [
                        identity["provider"],
                        entity_type,
                        identity["id"],
                        image_type,
                        *image_locales,
                    ],
                )
            )
        if not rows:
            return True
        current = datetime.now(timezone.utc)
        return any(_age_exceeded(row[0], days, current) for row in rows)

    def _root(self, entity: dict, entities_by_id: dict[str, dict]) -> dict:
        current = entity
        seen = set()
        while current.get("parentId") and current["id"] not in seen:
            seen.add(current["id"])
            parent = entities_by_id.get(str(current["parentId"]))
            if not parent:
                break
            current = parent
        return current

    def _blocked(
        self, entity: dict, entities_by_id: dict[str, dict], patterns: list[str]
    ) -> bool:
        if not patterns:
            return False
        root = self._root(entity, entities_by_id)
        candidates = [root.get("path", ""), Path(root.get("path", "")).stem]
        candidates.extend(
            identity["id"] for identity in self._identities(root["id"], root["type"])
        )
        folded = [str(value).casefold() for value in candidates if value]
        return any(
            pattern == "*" or any(pattern in value for value in folded)
            for pattern in patterns
        )

    def _titles_and_overviews(
        self, entity: dict, identities: list[dict], locales: list[str]
    ) -> tuple[list[str], list[str], list[dict]]:
        values = self._projection_values(entity["id"], locales)
        if not values:
            values = self._cached_values(entity["type"], identities, locales)
        titles = [
            str(value["title"]) for value in values if _usable(value.get("title"))
        ]
        overviews = [
            str(value.get("overview") or value.get("description"))
            for value in values
            if _usable(value.get("overview") or value.get("description"))
        ]
        return titles, overviews, values

    def _candidate(
        self,
        entity: dict,
        settings: dict,
        entities_by_id: dict[str, dict],
        locales: list[str],
    ) -> tuple[dict | None, str | None]:
        config = settings["itemTypes"][entity["type"]]
        if not config["enabled"]:
            return None, "disabled item type"
        if self._blocked(
            entity, entities_by_id, _patterns(settings["seriesBlockList"])
        ):
            return None, "series block list"
        current = datetime.now(timezone.utc)
        created = _utc(entity.get("createdAt"))
        if (
            config["cutoffDays"] != -1
            and created is not None
            and current - created > timedelta(days=config["cutoffDays"])
        ):
            return None, "outside catalog age cutoff"
        last_attempted, last_completed = self._state(entity["id"])
        if config["cooldownMinutes"] != -1 and not _age_exceeded(
            last_attempted,
            config["cooldownMinutes"] / 1440,
            current,
        ):
            return None, "refresh cooldown"
        identities = self._identities(entity["id"], entity["type"])
        if len(identities) < config["minimumProviderIds"]:
            return None, "minimum provider IDs"
        if not identities:
            return None, "no supported provider identity"
        titles, overviews, values = self._titles_and_overviews(
            entity, identities, locales
        )
        reasons = []
        metadata_due = False
        checks = config["checks"]
        patterns = _patterns(settings["badNames"])
        if checks["missingTitle"] and not titles:
            reasons.append("missing title")
            metadata_due = metadata_due or self._metadata_bucket_due(
                entity["type"], identities, locales, config["documentMaxAgeDays"]
            )
        if checks["missingOverview"] and not overviews:
            reasons.append("missing overview")
            metadata_due = metadata_due or self._metadata_bucket_due(
                entity["type"], identities, locales, config["documentMaxAgeDays"]
            )
        if checks["missingName"] and not titles:
            reasons.append("missing name")
            metadata_due = metadata_due or self._metadata_bucket_due(
                entity["type"], identities, locales, config["documentMaxAgeDays"]
            )
        if checks["nameIsDate"] and any(
            DATE_NAME_RE.fullmatch(title.strip()) for title in titles
        ):
            reasons.append("date name")
            metadata_due = metadata_due or self._metadata_bucket_due(
                entity["type"], identities, locales, config["documentMaxAgeDays"]
            )
        if (
            checks["overviewContainsBadName"]
            and patterns
            and any(
                pattern in overview.casefold()
                for overview in overviews
                for pattern in patterns
            )
        ):
            reasons.append("bad overview name")
            metadata_due = metadata_due or self._metadata_bucket_due(
                entity["type"], identities, locales, config["documentMaxAgeDays"]
            )
        if (
            patterns
            and entity["type"] == "episode"
            and any(
                title.casefold().startswith(pattern)
                for title in titles
                for pattern in patterns
            )
        ):
            reasons.append("bad name")
            metadata_due = metadata_due or self._metadata_bucket_due(
                entity["type"], identities, locales, config["documentMaxAgeDays"]
            )
        if entity["type"] == "series" and config["statusAfterDays"] != -1:
            statuses = {str(value.get("status") or "").casefold() for value in values}
            if statuses.intersection(
                {"continuing", "returning series", "in production", "planned"}
            ) and _age_exceeded(last_completed, config["statusAfterDays"], current):
                reasons.append("status refresh age")
                metadata_due = True
        artwork_due = False
        for image_type, image_config in config["artwork"].items():
            if not image_config["enabled"]:
                continue
            if not self._artwork_available(
                entity["id"], entity["type"], identities, locales, image_type
            ):
                reasons.append(f"missing {image_type} artwork")
                if self._artwork_bucket_due(
                    entity["type"],
                    identities,
                    locales,
                    image_type,
                    image_config["maxAgeDays"],
                ):
                    artwork_due = True
        if not reasons or not (metadata_due or artwork_due):
            return None, "metadata buckets are fresh"
        return (
            {
                "entity": entity,
                "identities": identities,
                "config": config,
                "reasons": reasons,
                "rootId": self._root(entity, entities_by_id)["id"],
            },
            None,
        )

    def _select(
        self, settings: dict, locales: list[str]
    ) -> tuple[list[dict], dict[str, int]]:
        entities = self._entities()
        entities_by_id = {entity["id"]: entity for entity in entities}
        candidates = []
        skipped = {"disabled": 0, "cutoff": 0, "cooldown": 0, "incomplete": 0}
        for entity in entities:
            candidate, reason = self._candidate(
                entity, settings, entities_by_id, locales
            )
            if candidate:
                candidates.append(candidate)
                continue
            if reason == "disabled item type":
                skipped["disabled"] += 1
            elif reason == "outside catalog age cutoff":
                skipped["cutoff"] += 1
            elif reason == "refresh cooldown":
                skipped["cooldown"] += 1
            elif reason != "metadata buckets are fresh":
                skipped["incomplete"] += 1
        return candidates, skipped

    @staticmethod
    def _groups(candidates: list[dict]) -> list[dict]:
        groups: dict[tuple[str, str, str], dict] = {}
        for candidate in candidates:
            config = candidate["config"]
            for identity in candidate["identities"]:
                key = (
                    identity["provider"],
                    candidate["entity"]["type"],
                    identity["id"],
                )
                group = groups.setdefault(
                    key,
                    {
                        "provider": identity["provider"],
                        "entityType": candidate["entity"]["type"],
                        "providerId": identity["id"],
                        "candidates": [],
                        "forceAssets": False,
                        "replaceMetadata": False,
                    },
                )
                if candidate not in group["candidates"]:
                    group["candidates"].append(candidate)
                group["forceAssets"] = group["forceAssets"] or bool(
                    config["replaceAllImages"]
                )
                group["replaceMetadata"] = group["replaceMetadata"] or bool(
                    config["replaceAllMetadata"]
                )
        return [groups[key] for key in sorted(groups)]

    def _publish(self, root_ids: set[str]) -> None:
        if not root_ids:
            return
        try:
            from app.catalog_read_model import CatalogReadModel

            CatalogReadModel(self.db).refresh_roots(sorted(root_ids))
        except Exception:
            logger.exception(
                "sparse metadata catalog publication failed roots=%s", root_ids
            )

    def run(
        self,
        run_id: str,
        definition: dict,
        should_terminate=None,
        *,
        preserve_cached_assets: bool = False,
    ) -> None:
        should_terminate = should_terminate or (lambda: False)
        settings = MetadataRefreshSettings(self.db).get()
        ingest = MetadataIngestService(background_assets=False)
        locales = ingest.locales()
        if not locales:
            locales = MetadataLanguageSettings().get()
        try:
            from app.jobs import _repair_missing_tv_child_identities

            _repair_missing_tv_child_identities(self.db, ingest.metadata_service)
        except Exception:
            logger.warning(
                "sparse metadata child identity repair failed", exc_info=True
            )
        candidates, skipped = self._select(settings, locales)
        groups = self._groups(candidates)
        total = max(1, len(groups))
        self.store.update_run(
            run_id,
            state="running",
            started_at=self._timestamp(),
            message=f"Selecting sparse metadata refreshes · {len(candidates)} items",
            progress_total=total,
            progress_phase="selection",
            progress_label="Selecting sparse metadata refreshes",
            progress_stage_current=0,
            progress_stage_total=total,
            progress_stage_unit="provider identities",
        )
        logger.info(
            "sparse metadata refresh start run_id=%s candidates=%d groups=%d locales=%s",
            run_id,
            len(candidates),
            len(groups),
            locales,
        )
        if settings["pretend"]:
            summary = (
                f"Pretend mode: checked {len(self._entities())} items; "
                f"would refresh {len(candidates)} items across {len(groups)} provider identities"
            )
            self.store.update_run(
                run_id,
                state="completed",
                progress_current=total,
                progress_total=total,
                finished_at=self._timestamp(),
                message=summary,
                error_details=json.dumps(
                    {
                        "mode": "sparse",
                        "pretend": True,
                        "candidates": len(candidates),
                        "groups": len(groups),
                        "skipped": skipped,
                    }
                ),
            )
            return
        failures = []
        refreshed_items: set[str] = set()
        completed_groups = 0
        refreshed_groups = 0
        for group, result, error in metadata_task_results(
            groups,
            lambda value: self._process_group(
                value,
                run_id,
                ingest,
                locales,
                should_terminate,
                preserve_cached_assets,
            ),
            should_terminate,
        ):
            if should_terminate():
                raise JobTerminated()
            completed_groups += 1
            if error is not None:
                failures.append(
                    {
                        "provider": group["provider"],
                        "entityType": group["entityType"],
                        "providerId": group["providerId"],
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
            else:
                refreshed_groups += 1
                refreshed_items.update(result["entityIds"])
                self._publish(set(result["rootIds"]))
                logger.info(
                    "sparse metadata refresh identity complete run_id=%s provider=%s entity_type=%s provider_id=%s",
                    run_id,
                    group["provider"],
                    group["entityType"],
                    group["providerId"],
                )
            self.store.update_run(
                run_id,
                progress_current=completed_groups,
                progress_total=total,
                progress_phase="metadata",
                progress_label="Refreshing sparse metadata",
                progress_stage_current=completed_groups,
                progress_stage_total=total,
                progress_stage_unit="provider identities",
                message=f"Refreshing sparse metadata · {completed_groups}/{total} provider identities",
            )
        if should_terminate():
            raise JobTerminated()
        summary = (
            f"Checked {len(self._entities())} items; refreshed {len(refreshed_items)} "
            f"items across {refreshed_groups} provider identities; "
            f"skipped {len(self._entities()) - len(candidates)}"
        )
        if failures:
            summary += f"; {len(failures)} failed"
            state = "failed"
        else:
            state = "completed"
        self.store.update_run(
            run_id,
            state=state,
            progress_current=completed_groups,
            progress_total=total,
            finished_at=self._timestamp(),
            message=summary,
            error=summary if failures else None,
            error_details=json.dumps(
                {
                    "mode": "sparse",
                    "checked": len(self._entities()),
                    "candidates": len(candidates),
                    "refreshed": len(refreshed_items),
                    "providerIdentities": refreshed_groups,
                    "skipped": skipped,
                    "failed": failures,
                }
            ),
        )
        logger.info(
            "sparse metadata refresh complete run_id=%s checked=%d refreshed=%d failures=%d",
            run_id,
            len(self._entities()),
            len(refreshed_items),
            len(failures),
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _process_group(
        self,
        group: dict,
        run_id: str,
        ingest: MetadataIngestService,
        locales: list[str],
        should_terminate,
        preserve_cached_assets: bool,
    ) -> dict:
        if should_terminate():
            raise JobTerminated()
        entity_ids = [candidate["entity"]["id"] for candidate in group["candidates"]]
        attempted = self._timestamp()
        self._state_update(entity_ids, attempted=attempted, error=None)
        try:
            documents = ingest.ingest_locales(
                group["provider"],
                group["entityType"],
                group["providerId"],
                locales,
                force=True,
                force_assets=bool(group["forceAssets"] and not preserve_cached_assets),
                replace_metadata=bool(group["replaceMetadata"]),
            )
            if not isinstance(documents, dict) or any(
                not isinstance(documents.get(locale), dict) for locale in locales
            ):
                raise ProviderError("metadata refresh returned an incomplete document")
        except (ProviderError, ValueError, OSError) as error:
            message = f"{type(error).__name__}: {error}"
            self._state_update(entity_ids, error=message)
            logger.warning(
                "sparse metadata refresh failed run_id=%s provider=%s entity_type=%s provider_id=%s error=%s",
                run_id,
                group["provider"],
                group["entityType"],
                group["providerId"],
                message,
            )
            raise
        completed = self._timestamp()
        self._state_update(entity_ids, completed=completed, error=None)
        return {
            "entityIds": entity_ids,
            "rootIds": [candidate["rootId"] for candidate in group["candidates"]],
        }
