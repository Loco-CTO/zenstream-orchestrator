import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.database import DatabaseHandler


class DatabaseHandlerTest(unittest.TestCase):
    def test_file_reads_use_a_connection_per_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            database = DatabaseHandler("sqlite", {}, str(Path(directory) / "orchestrator.db"))
            database.execute("CREATE TABLE values_table(value INTEGER)")
            database.execute("INSERT INTO values_table VALUES(1)")
            connections = []
            lock = threading.Lock()
            barrier = threading.Barrier(2)

            def read_value():
                barrier.wait()
                self.assertEqual(database.read_execute("SELECT value FROM values_table"), [(1,)])
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
                database.execute(
                    "SELECT value FROM values_table ORDER BY sequence"
                ),
                [("immediate",), ("batch",)],
            )
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
