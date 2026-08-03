import tempfile
import threading
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
