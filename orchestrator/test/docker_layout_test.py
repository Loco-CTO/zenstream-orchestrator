import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DockerLayoutTest(unittest.TestCase):
    def test_docker_image_includes_alembic_configuration_and_migrations(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY alembic.ini ./", dockerfile)
        self.assertIn("COPY .main-version.json ./", dockerfile)
        self.assertIn("COPY migrations/ ./migrations/", dockerfile)
        self.assertTrue((PROJECT_ROOT / ".main-version.json").is_file())
        self.assertTrue((PROJECT_ROOT / "alembic.ini").is_file())
        self.assertTrue((PROJECT_ROOT / "migrations" / "env.py").is_file())

    def test_container_uses_application_port(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        containerfile = (PROJECT_ROOT / "Containerfile").read_text(encoding="utf-8")
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("ORCHESTRATOR_PORT=9090", dockerfile)
        self.assertIn("EXPOSE 9090", dockerfile)
        self.assertIn("ORCHESTRATOR_PORT=9090", containerfile)
        self.assertIn("EXPOSE 9090", containerfile)
        self.assertIn("${ORCHESTRATOR_PORT:-9090}:9090", compose)
        self.assertIn("ORCHESTRATOR_PORT: 9090", compose)

    def test_docker_context_keeps_dashboard_assets(self):
        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY assets/ ./assets/", dockerfile)
        self.assertIn("COPY frontend/ ./", dockerfile)
        self.assertIn("RUN npm ci --ignore-scripts --no-audit --no-fund", dockerfile)
        self.assertNotRegex(dockerignore, r"(?m)^/?assets/?$")
        self.assertNotRegex(dockerignore, r"(?m)^/?frontend/?$")


if __name__ == "__main__":
    unittest.main()
