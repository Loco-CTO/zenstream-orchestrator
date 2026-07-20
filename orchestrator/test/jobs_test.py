import unittest

from app.jobs import JobStore


class JobMappingTest(unittest.TestCase):
    def test_definition_mapping_uses_all_persisted_columns(self):
        row = ("id", "key", "Name", "Description", "kind", 60, 1, "{}", "next", "last", "run", "completed", "done", "created", "updated")
        value = JobStore._definition(row)
        self.assertEqual(value["createdAt"], "created")
        self.assertEqual(value["updatedAt"], "updated")
        self.assertTrue(value["enabled"])

    def test_run_mapping_preserves_progress_and_thread(self):
        row = ("run", "definition", None, "metadata_refresh", "running", 4, 10, "Working", None, "created", "started", None, "worker")
        value = JobStore._run(row)
        self.assertEqual(value["progressCurrent"], 4)
        self.assertEqual(value["threadName"], "worker")


if __name__ == "__main__":
    unittest.main()
