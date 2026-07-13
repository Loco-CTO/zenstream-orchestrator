import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DockerLayoutTest(unittest.TestCase):
    def test_docker_image_includes_alembic_configuration_and_migrations(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY alembic.ini ./", dockerfile)
        self.assertIn("COPY migrations/ ./migrations/", dockerfile)
        self.assertTrue((PROJECT_ROOT / "alembic.ini").is_file())
        self.assertTrue((PROJECT_ROOT / "migrations" / "env.py").is_file())


if __name__ == "__main__":
    unittest.main()
