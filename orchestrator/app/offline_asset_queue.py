import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from app.logging_config import get_logger
from app.paths import metadata_directory
from fastapi import HTTPException

logger = get_logger("offline_assets")


def now_ms():
    return int(time.time() * 1000)


def prepared_limit():
    try:
        return max(
            1073741824,
            min(
                1099511627776,
                int(os.getenv("OFFLINE_PREPARED_MAX_BYTES", "21474836480")),
            ),
        )
    except ValueError:
        return 21474836480


class OfflineAssetQueue:
    def __init__(self, db, cache_directory=None):
        self.db = db
        self.cache_directory = (
            Path(cache_directory)
            if cache_directory
            else metadata_directory() / "offline-prepared-cache"
        )

    def enqueue(self, user_id, selection_id, request, *, retry=False):
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        now = now_ms()
        with self.db.transaction() as cursor:
            selected = cursor.execute(
                "SELECT 1 FROM offline_selections s JOIN offline_selection_items i ON i.selection_id=s.id WHERE s.id=? AND s.user_id=? AND s.state='ready' AND i.entity_id=?",
                (selection_id, user_id, request["entityId"]),
            ).fetchone()
            if not selected:
                raise HTTPException(404, "Ready download selection not found.")
            existing = cursor.execute(
                "SELECT state FROM offline_assets WHERE id=?", (identity,)
            ).fetchone()
            if existing and existing[0] == "removing":
                raise HTTPException(409, "Asset cleanup is finishing. Retry shortly.")
            if not existing:
                pending = cursor.execute(
                    "SELECT count(*) FROM offline_assets WHERE state IN ('queued','preparing')"
                ).fetchone()[0]
                if pending >= 200:
                    raise HTTPException(429, "Download preparation queue is full.")
                cursor.execute(
                    "INSERT INTO offline_assets(id,entity_id,request_json,state,created_ms,updated_ms) VALUES(?,?,?,'queued',?,?)",
                    (identity, request["entityId"], encoded, now, now),
                )
            elif retry and existing[0] in {"failed", "cancelled"}:
                cursor.execute(
                    "UPDATE offline_assets SET state='queued',error_code=NULL,updated_ms=?,lease_until=0 WHERE id=?",
                    (now, identity),
                )
            cursor.execute(
                "INSERT INTO offline_asset_reservations(asset_id,selection_id,user_id,expires_ms) VALUES(?,?,?,?) ON CONFLICT(asset_id,selection_id) DO UPDATE SET expires_ms=excluded.expires_ms",
                (identity, selection_id, user_id, now + 172800000),
            )
        return identity

    def active(self, identity):
        return bool(
            self.db.read_execute(
                "SELECT 1 FROM offline_asset_reservations r JOIN offline_selections s ON s.id=r.selection_id WHERE r.asset_id=? AND r.expires_ms>? AND s.state='ready' LIMIT 1",
                (identity, now_ms()),
            )
        )

    def release_selection(self, user_id, selection_id):
        with self.db.transaction() as cursor:
            cursor.execute(
                "DELETE FROM offline_asset_reservations WHERE user_id=? AND selection_id=?",
                (user_id, selection_id),
            )
            cursor.execute(
                "UPDATE offline_assets SET state='cancelled',updated_ms=? WHERE state IN ('queued','preparing') AND NOT EXISTS (SELECT 1 FROM offline_asset_reservations r WHERE r.asset_id=offline_assets.id AND r.expires_ms>?)",
                (now_ms(), now_ms()),
            )

    def process_next(self, producer, stop_event):
        if stop_event.is_set():
            return False
        now = now_ms()
        worker = str(uuid4())
        with self.db.transaction() as cursor:
            row = cursor.execute(
                "SELECT id,request_json FROM offline_assets a WHERE (state='queued' OR (state='preparing' AND lease_until<?)) AND EXISTS (SELECT 1 FROM offline_asset_reservations r JOIN offline_selections s ON s.id=r.selection_id WHERE r.asset_id=a.id AND r.expires_ms>? AND s.state='ready') ORDER BY created_ms,id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return False
            identity, raw = row
            cursor.execute(
                "UPDATE offline_assets SET state='preparing',worker_id=?,lease_until=?,updated_ms=? WHERE id=?",
                (worker, now + 90000, now, identity),
            )
        last_check = 0
        cancelled = False

        def should_stop():
            nonlocal last_check, cancelled
            if stop_event.is_set():
                return True
            current = now_ms()
            if current - last_check >= 1000:
                cancelled = not self.active(identity)
                if not cancelled:
                    self.db.execute(
                        "UPDATE offline_assets SET lease_until=? WHERE id=? AND worker_id=?",
                        (current + 90000, identity, worker),
                    )
                last_check = current
            return cancelled

        try:
            result = producer(identity, json.loads(raw), should_stop)
            if stop_event.is_set() or not self.active(identity):
                raise InterruptedError("Preparation interrupted")
            self.db.execute(
                "UPDATE offline_assets SET state='ready',result_json=?,error_code=NULL,updated_ms=?,lease_until=0 WHERE id=? AND worker_id=?",
                (json.dumps(result), now_ms(), identity, worker),
            )
        except InterruptedError:
            self.db.execute(
                "UPDATE offline_assets SET state=?,updated_ms=?,lease_until=0 WHERE id=? AND worker_id=?",
                (
                    "queued" if stop_event.is_set() else "cancelled",
                    now_ms(),
                    identity,
                    worker,
                ),
            )
        except Exception:
            logger.exception("offline preparation failed asset_id=%s", identity)
            self.db.execute(
                "UPDATE offline_assets SET state='failed',error_code='preparation_failed',updated_ms=?,lease_until=0 WHERE id=? AND worker_id=?",
                (now_ms(), identity, worker),
            )
        return True

    def cleanup(self):
        now = now_ms()
        self.db.execute(
            "DELETE FROM offline_asset_reservations WHERE rowid IN (SELECT rowid FROM offline_asset_reservations WHERE expires_ms<? LIMIT 100)",
            (now,),
        )
        rows = self.db.read_execute(
            "SELECT id,result_json FROM offline_assets a WHERE updated_ms<? AND state NOT IN ('preparing','queued') AND NOT EXISTS (SELECT 1 FROM offline_asset_reservations r WHERE r.asset_id=a.id) LIMIT 100",
            (now - 604800000,),
        )
        for identity, raw in rows:
            with self.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE offline_assets SET state='removing' WHERE id=? AND updated_ms<? AND state NOT IN ('preparing','queued') AND NOT EXISTS (SELECT 1 FROM offline_asset_reservations WHERE asset_id=?)",
                    (identity, now - 604800000, identity),
                )
                if not cursor.rowcount:
                    continue
            result = json.loads(raw) if raw else {}
            names = [
                result.get("storageName"),
                *[item.get("storageName") for item in result.get("supplements", [])],
            ]
            for name in names:
                if name:
                    target = self.cache_directory / name
                    if target.resolve().parent != self.cache_directory.resolve():
                        raise ValueError("Invalid prepared asset location")
                    target.unlink(missing_ok=True)
            self.db.execute(
                "DELETE FROM offline_assets WHERE id=? AND NOT EXISTS (SELECT 1 FROM offline_asset_reservations WHERE asset_id=?)",
                (identity, identity),
            )
