from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_ORCHESTRATOR_ROOT = str(Path(__file__).resolve().parents[1])
if _ORCHESTRATOR_ROOT not in sys.path:
    sys.path.insert(0, _ORCHESTRATOR_ROOT)

from api.zenstream.application_routes import health_ready
from app.config import Config
from fastapi import HTTPException


class ReadinessRouteTest(unittest.TestCase):
    def test_readiness_queries_sqlite_and_returns_ok(self):
        database = Mock()
        config = Mock()
        config.database = database

        with patch.object(Config, "_instance", config):
            self.assertEqual(health_ready(), {"status": "ok"})

        database.execute.assert_called_once_with("SELECT 1")

    def test_readiness_returns_generic_unavailable_when_database_is_absent(self):
        with patch.object(Config, "_instance", None):
            with self.assertRaises(HTTPException) as raised:
                health_ready()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Database unavailable.")

    def test_readiness_does_not_expose_database_errors(self):
        database = Mock()
        database.execute.side_effect = RuntimeError("private sqlite path")
        config = Mock()
        config.database = database

        with patch.object(Config, "_instance", config):
            with self.assertRaises(HTTPException) as raised:
                health_ready()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Database unavailable.")


if __name__ == "__main__":
    unittest.main()
