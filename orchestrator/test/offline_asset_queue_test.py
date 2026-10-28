import threading
import unittest

from app.database import DatabaseHandler
from app.offline_asset_queue import OfflineAssetQueue, now_ms
from fastapi import HTTPException


class OfflineAssetQueueTest(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseHandler("sqlite", {}, ":memory:")
        self.addCleanup(self.db.close)
        for sql in (
            "CREATE TABLE offline_selections(id TEXT PRIMARY KEY,user_id TEXT,state TEXT)",
            "CREATE TABLE offline_selection_items(selection_id TEXT,entity_id TEXT)",
            "CREATE TABLE offline_assets(id TEXT PRIMARY KEY,entity_id TEXT,request_json TEXT,state TEXT,result_json TEXT,error_code TEXT,created_ms INTEGER,updated_ms INTEGER,lease_until INTEGER DEFAULT 0,worker_id TEXT)",
            "CREATE TABLE offline_asset_reservations(asset_id TEXT,selection_id TEXT,user_id TEXT,expires_ms INTEGER,PRIMARY KEY(asset_id,selection_id))",
            "INSERT INTO offline_selections VALUES('selection','viewer','ready')",
            "INSERT INTO offline_selection_items VALUES('selection','movie')",
        ):
            self.db.execute(sql)
        self.queue = OfflineAssetQueue(self.db)
        self.stop = threading.Event()
        self.request = {
            "entityId": "movie",
            "sourceId": "source",
            "profile": "original",
            "sourceRevision": "version-1",
        }

    def enqueue(self):
        return self.queue.enqueue("viewer", "selection", self.request)

    def state(self, identity):
        return self.db.read_execute(
            "SELECT state FROM offline_assets WHERE id=?", (identity,)
        )[0][0]

    def test_duplicate_requests_share_one_preparation(self):
        identity = self.enqueue()
        self.assertEqual(self.enqueue(), identity)
        calls = []
        self.queue.process_next(
            lambda *args: calls.append(args) or {"sizeBytes": 100}, self.stop
        )
        self.assertEqual(self.state(identity), "ready")
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.queue.process_next(lambda *_args: None, self.stop))

    def test_expired_worker_lease_can_recover_after_restart(self):
        identity = self.enqueue()
        self.db.execute(
            "UPDATE offline_assets SET state='preparing',lease_until=?", (now_ms() - 1,)
        )
        restored = OfflineAssetQueue(self.db)
        self.assertTrue(
            restored.process_next(lambda *_args: {"sizeBytes": 100}, self.stop)
        )
        self.assertEqual(self.state(identity), "ready")

    def test_shutdown_keeps_work_queued(self):
        identity = self.enqueue()

        def produce(*_args):
            self.stop.set()
            return {}

        self.queue.process_next(produce, self.stop)
        self.assertEqual(self.state(identity), "queued")

    def test_last_reservation_cancellation_stops_preparation(self):
        identity = self.enqueue()
        self.queue.release_selection("viewer", "selection")
        self.assertEqual(self.state(identity), "cancelled")
        self.assertFalse(self.queue.active(identity))

    def test_one_selection_cannot_cancel_another_selection_reference(self):
        identity = self.enqueue()
        self.db.execute(
            "INSERT INTO offline_selections VALUES('second','viewer','ready')"
        )
        self.db.execute("INSERT INTO offline_selection_items VALUES('second','movie')")
        self.queue.enqueue("viewer", "second", self.request)
        self.queue.release_selection("viewer", "selection")
        self.assertTrue(self.queue.active(identity))

    def test_unowned_or_incomplete_selections_are_rejected(self):
        with self.assertRaises(HTTPException):
            self.queue.enqueue("other", "selection", self.request)
        self.db.execute("UPDATE offline_selections SET state='enumerating'")
        with self.assertRaises(HTTPException):
            self.enqueue()

    def test_failure_is_durable_and_requires_explicit_retry(self):
        identity = self.enqueue()

        def fail(*_args):
            raise OSError("isolated fixture failure")

        self.queue.process_next(fail, self.stop)
        self.assertEqual(self.state(identity), "failed")
        self.queue.enqueue("viewer", "selection", self.request, retry=True)
        self.assertEqual(self.state(identity), "queued")
