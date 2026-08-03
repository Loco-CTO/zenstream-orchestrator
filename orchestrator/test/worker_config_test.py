import os
import unittest
from unittest.mock import patch

from app.worker_config import configured_worker_limit, worker_pool_size


class WorkerConfigTest(unittest.TestCase):
    def test_defaults_are_unlimited(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(configured_worker_limit("METADATA_ROOT_WORKERS", 8))
            self.assertIsNone(configured_worker_limit("METADATA_ASSET_WORKERS", 16))

    def test_positive_values_are_capped(self):
        with patch.dict(os.environ, {
            "METADATA_ROOT_WORKERS": "99",
            "METADATA_ASSET_WORKERS": "3",
        }, clear=True):
            self.assertEqual(configured_worker_limit("METADATA_ROOT_WORKERS", 8), 8)
            self.assertEqual(configured_worker_limit("METADATA_ASSET_WORKERS", 16), 3)

    def test_negative_values_use_one_worker(self):
        with patch.dict(os.environ, {
            "METADATA_ROOT_WORKERS": "-1",
            "METADATA_ASSET_WORKERS": "-5",
        }, clear=True):
            self.assertEqual(configured_worker_limit("METADATA_ROOT_WORKERS", 8), 1)
            self.assertEqual(configured_worker_limit("METADATA_ASSET_WORKERS", 16), 1)

    def test_unlimited_pool_uses_all_items(self):
        self.assertEqual(worker_pool_size(None, 4), 4)
        self.assertEqual(worker_pool_size(None, 0), 1)
        self.assertEqual(worker_pool_size(3, 20), 3)
