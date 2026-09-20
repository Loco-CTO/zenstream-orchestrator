import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database import DatabaseHandler
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError


class DatabaseHandlerTest(unittest.TestCase):
    def test_file_reads_use_a_connection_per_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            database.execute("CREATE TABLE values_table(value INTEGER)")
            database.execute("INSERT INTO values_table VALUES(1)")
            connections = []
            lock = threading.Lock()
            barrier = threading.Barrier(2)

            def read_value():
                barrier.wait()
                with database.read_session(label="test:per_thread"):
                    self.assertEqual(
                        database.read_execute("SELECT value FROM values_table"),
                        [(1,)],
                    )
                    with lock:
                        connections.append(id(database.read_local.connection))

            first = threading.Thread(target=read_value)
            second = threading.Thread(target=read_value)
            first.start()
            second.start()
            first.join()
            second.join()
            database.close()

        self.assertEqual(len(set(connections)), 2)
        self.assertIsNone(getattr(database.read_local, "connection", None))
        self.assertEqual(database.metrics()["reader_active"], 0)

    def test_reader_leases_release_on_success_and_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            try:
                database.execute("CREATE TABLE values_table(value INTEGER)")
                with database.read_session(label="test:success"):
                    self.assertEqual(
                        database.read_execute("SELECT COUNT(*) FROM values_table"),
                        [(0,)],
                    )
                    time.sleep(0.01)
                with self.assertRaisesRegex(RuntimeError, "expected"):
                    with database.read_session(label="test:exception"):
                        raise RuntimeError("expected")
                metrics = database.metrics()
                self.assertEqual(metrics["reader_active"], 0)
                self.assertEqual(metrics["reader_holders"], [])
                self.assertGreaterEqual(metrics["reader_sessions"], 2)
                self.assertGreaterEqual(metrics["reader_hold_seconds"], 0)
            finally:
                database.close()

    def test_nested_reader_sessions_share_one_lease_and_emit_safe_label(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            try:
                database.execute("CREATE TABLE values_table(value INTEGER)")
                before = database.metrics()["reader_sessions"]
                with database.read_session(label="catalog:items"):
                    inside = database.metrics()
                    self.assertEqual(inside["reader_active"], 1)
                    self.assertEqual(inside["reader_holders"][0]["label"], "catalog:items")
                    with database.read_session(label="ignored:nested"):
                        self.assertEqual(database.metrics()["reader_active"], 1)
                after = database.metrics()
                self.assertEqual(after["reader_sessions"], before + 1)
                self.assertEqual(after["reader_active"], 0)
            finally:
                database.close()

    def test_reader_hold_and_checkout_timeout_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler(
                "sqlite", {}, str(Path(directory) / "orchestrator.db")
            )
            try:
                database.execute("CREATE TABLE values_table(value INTEGER)")
                with patch("app.database.READER_LONG_HOLD_SECONDS", 0.01):
                    with database.read_session(label="library_cleanup:referenced_paths"):
                        time.sleep(0.02)
                with patch.object(
                    database.persistence,
                    "read_sessions",
                    side_effect=SQLAlchemyTimeoutError("pool is busy"),
                ):
                    with self.assertRaises(SQLAlchemyTimeoutError):
                        database.read_execute("SELECT 1")
                metrics = database.metrics()
                self.assertGreaterEqual(metrics["reader_max_hold_seconds"], 0.01)
                self.assertGreaterEqual(metrics["reader_long_holds"], 1)
                self.assertGreaterEqual(metrics["reader_checkout_timeouts"], 1)
                self.assertEqual(metrics["reader_active"], 0)
            finally:
                database.close()

    def test_write_many_prepares_generator_before_acquiring_writer(self):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        database.execute(
            "CREATE TABLE values_table(sequence INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)"
        )
        preparing = threading.Event()
        prepared = threading.Event()

        def statements():
            preparing.set()
            self.assertTrue(prepared.wait(2))
            yield "INSERT INTO values_table(value) VALUES(?)", ("batch",)

        worker = threading.Thread(target=lambda: database.write_many(statements()))
        worker.start()
        self.assertTrue(preparing.wait(2))
        database.execute("INSERT INTO values_table(value) VALUES(?)", ("immediate",))
        prepared.set()
        worker.join(2)
        try:
            self.assertFalse(worker.is_alive())
            self.assertEqual(
                database.execute("SELECT value FROM values_table ORDER BY sequence"),
                [("immediate",), ("batch",)],
            )
        finally:
            database.close()

    def test_failed_write_rolls_back_and_raises(self):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        database.execute("CREATE TABLE values_table(value TEXT UNIQUE)")
        database.execute("INSERT INTO values_table VALUES('first')")
        try:
            with self.assertRaises(Exception):
                database.execute("INSERT INTO values_table VALUES('first')")
            database.execute("INSERT INTO values_table VALUES('second')")
            self.assertEqual(
                database.execute("SELECT value FROM values_table ORDER BY value"),
                [("first",), ("second",)],
            )
        finally:
            database.close()

    def test_nested_writes_and_reads_share_the_outer_transaction(self):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        database.execute("CREATE TABLE values_table(value INTEGER)")
        try:
            with database.transaction():
                database.execute("INSERT INTO values_table VALUES(?)", (1,))
                database.write_many(
                    [
                        ("INSERT INTO values_table VALUES(?)", (2,)),
                        ("INSERT INTO values_table VALUES(?)", (3,)),
                    ]
                )
                self.assertEqual(
                    database.read_execute(
                        "SELECT value FROM values_table ORDER BY value"
                    ),
                    [(1,), (2,), (3,)],
                )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM values_table"), [(3,)]
            )
        finally:
            database.close()

    def test_writer_metrics_include_commit_and_hold_time(self):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        try:
            database.execute("CREATE TABLE values_table(value INTEGER)")
            before = database.metrics()
            database.write_many(
                [
                    ("INSERT INTO values_table VALUES(?)", (1,)),
                    ("INSERT INTO values_table VALUES(?)", (2,)),
                ]
            )
            after = database.metrics()
            self.assertGreaterEqual(after["commit_count"] - before["commit_count"], 1)
            self.assertIn("writer_hold_seconds", after)
        finally:
            database.close()

    def test_writer_gate_admits_waiters_in_arrival_order(self):
        database = DatabaseHandler("sqlite", {}, ":memory:")
        database.execute("CREATE TABLE values_table(value INTEGER)")
        holder_ready = threading.Event()
        release_holder = threading.Event()
        order = []

        def holder():
            with database.transaction():
                holder_ready.set()
                self.assertTrue(release_holder.wait(2))

        def writer(value):
            database.execute("INSERT INTO values_table VALUES(?)", (value,))
            order.append(value)

        holding = threading.Thread(target=holder)
        holding.start()
        self.assertTrue(holder_ready.wait(2))
        writers = []
        for value in range(4):
            thread = threading.Thread(target=writer, args=(value,))
            thread.start()
            writers.append(thread)
            time.sleep(0.02)
        release_holder.set()
        holding.join(2)
        for thread in writers:
            thread.join(2)
        try:
            self.assertEqual(order, [0, 1, 2, 3])
        finally:
            database.close()
