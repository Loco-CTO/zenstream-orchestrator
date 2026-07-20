import json
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class VSCodeConfigTest(unittest.TestCase):
    def test_full_stack_launch_uses_reloadable_backend_and_dashboard_task(self):
        launch = json.loads(
            (REPOSITORY_ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8")
        )
        configurations = {
            configuration["name"]: configuration
            for configuration in launch["configurations"]
        }

        backend = configurations["Debug Orchestrator: Backend"]
        self.assertEqual(backend["env"]["USE_RELOADER"], "true")
        self.assertNotIn("args", backend)

        compound = next(
            compound
            for compound in launch["compounds"]
            if compound["name"] == "Debug Orchestrator: Full Stack"
        )
        self.assertEqual(compound["configurations"], ["Debug Orchestrator: Backend"])
        self.assertEqual(compound["preLaunchTask"], "Orchestrator: Frontend")

        tasks = json.loads(
            (REPOSITORY_ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8")
        )
        frontend = next(
            task for task in tasks["tasks"] if task["label"] == "Orchestrator: Frontend"
        )
        self.assertTrue(frontend["isBackground"])
        self.assertEqual(frontend["args"][-2:], ["--port", "3001"])
        self.assertEqual(
            frontend["options"]["env"]["ORCHESTRATOR_API_URL"],
            "http://127.0.0.1:9090",
        )
        self.assertEqual(
            frontend["problemMatcher"]["background"]["endsPattern"],
            ".*Ready in.*",
        )


if __name__ == "__main__":
    unittest.main()
