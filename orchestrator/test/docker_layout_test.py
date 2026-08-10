import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DockerLayoutTest(unittest.TestCase):
    def test_dashboard_static_root_is_the_orchestrator_web_directory(self):
        from api.zenstream.application_routes import _static_roots

        web_root, assets_root = _static_roots()

        self.assertIn(
            web_root,
            {PROJECT_ROOT / "orchestrator" / "web", PROJECT_ROOT / "frontend" / "out"},
        )
        self.assertEqual(assets_root, PROJECT_ROOT / "assets")

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
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("ORCHESTRATOR_PORT=9088", dockerfile)
        self.assertIn("EXPOSE 9088", dockerfile)
        self.assertIn("${ORCHESTRATOR_PORT:-9088}:9088", compose)
        self.assertIn("ORCHESTRATOR_PORT: 9088", compose)

    def test_container_maps_the_configured_metadata_root_to_the_writable_path(self):
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("METADATA_PATH: /app/sqlite", compose)
        self.assertIn('${METADATA_PATH:-./metadata}:/app/sqlite', compose)

    def test_docker_context_keeps_dashboard_assets(self):
        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY assets/ ./assets/", dockerfile)
        self.assertIn("apt-get install -y --no-install-recommends ffmpeg", dockerfile)
        self.assertIn("cp /usr/bin/ffmpeg ./assets/ffmpeg/linux/ffmpeg", dockerfile)
        self.assertIn("cp /usr/bin/ffprobe ./assets/ffmpeg/linux/ffprobe", dockerfile)
        self.assertIn("COPY frontend/ ./", dockerfile)
        self.assertIn(
            "COPY frontend/package.json frontend/package-lock.json ./", dockerfile
        )
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund", dockerfile)
        self.assertNotRegex(dockerignore, r"(?m)^/?assets/?$")
        self.assertNotRegex(dockerignore, r"(?m)^/?frontend/?$")


if __name__ == "__main__":
    unittest.main()
