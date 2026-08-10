import os
import unittest
from unittest.mock import patch

from app.worker_config import configured_worker_limit


class WorkerConfigTest(unittest.TestCase):
    def test_defaults_are_twelve_workers(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_worker_limit("METADATA_ROOT_WORKERS", 64), 12)
            self.assertEqual(configured_worker_limit("METADATA_ASSET_WORKERS", 64), 12)
            self.assertEqual(configured_worker_limit("METADATA_FETCH_WORKERS", 64), 12)

    def test_positive_values_are_capped(self):
        with patch.dict(
            os.environ,
            {
                "METADATA_ROOT_WORKERS": "99",
                "METADATA_ASSET_WORKERS": "3",
                "METADATA_FETCH_WORKERS": "128",
            },
            clear=True,
        ):
            self.assertEqual(configured_worker_limit("METADATA_ROOT_WORKERS", 64), 64)
            self.assertEqual(configured_worker_limit("METADATA_ASSET_WORKERS", 64), 3)
            self.assertEqual(configured_worker_limit("METADATA_FETCH_WORKERS", 64), 64)

    def test_non_positive_values_use_one_worker(self):
        with patch.dict(
            os.environ,
            {
                "METADATA_ROOT_WORKERS": "-1",
                "METADATA_ASSET_WORKERS": "0",
                "METADATA_FETCH_WORKERS": "-5",
            },
            clear=True,
        ):
            self.assertEqual(configured_worker_limit("METADATA_ROOT_WORKERS", 64), 1)
            self.assertEqual(configured_worker_limit("METADATA_ASSET_WORKERS", 64), 1)
            self.assertEqual(configured_worker_limit("METADATA_FETCH_WORKERS", 64), 1)
