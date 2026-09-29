import asyncio
import os
import unittest
from unittest.mock import patch

from app.app import _SECRET_KEY_PLACEHOLDER, _validate_secret_key, lifespan


class SecretKeyValidationTest(unittest.TestCase):
    def test_missing_and_blank_secrets_are_rejected(self):
        for secret_key in (None, "", " \t\r\n"):
            with (
                self.subTest(secret_key=secret_key),
                self.assertRaisesRegex(RuntimeError, "SECRET_KEY.*not set"),
            ):
                _validate_secret_key(secret_key)

    def test_documented_placeholder_is_rejected_with_surrounding_whitespace(self):
        for secret_key in (
            _SECRET_KEY_PLACEHOLDER,
            f" \t{_SECRET_KEY_PLACEHOLDER} \r\n",
        ):
            with (
                self.subTest(secret_key=secret_key),
                self.assertRaisesRegex(RuntimeError, "replaced with a random secret"),
            ):
                _validate_secret_key(secret_key)

    def test_custom_secret_is_accepted_without_a_length_requirement(self):
        self.assertIsNone(_validate_secret_key("legacy-custom-secret"))

    def test_lifespan_rejects_placeholder_before_starting_catalog_or_workers(self):
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY_PLACEHOLDER}),
            patch("app.app.load_config") as load_config,
            patch("app.app.CatalogReadModel") as catalog_read_model,
            patch("app.app.library_runtime.start") as library_start,
            patch("app.app.job_scheduler.start") as scheduler_start,
        ):
            with self.assertRaisesRegex(RuntimeError, "documented placeholder"):
                asyncio.run(lifespan(None).__aenter__())

        load_config.assert_called_once_with()
        catalog_read_model.assert_not_called()
        library_start.assert_not_called()
        scheduler_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
