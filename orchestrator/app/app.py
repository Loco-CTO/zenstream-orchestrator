import os

from flask import Blueprint, Flask
from flask_restx import Api
from logger import Logger
from .config import load_config
from flask_cors import CORS

from api import api_namespaces
from app.config import Config
from app.syncplay_socket import socketio


class Orchestrator:
    def __init__(self, logger: Logger = None, version: str = None):
        """
        Initialize the Orchestrator.

        Args:
            logger (Logger): The logger instance.
            version (str): The version of the Orchestrator.
        """
        load_config()
        self.logger = logger
        self.version = version

    def create(self):
        """Create the Orchestrator."""
        self.logger.info("Creating Orchestrator...")
        self.app = Flask(__name__)
        configured_origins = os.getenv("CORS_ORIGINS", "")
        cors_origins = [
            origin.strip() for origin in configured_origins.split(",") if origin.strip()
        ]
        cors_origins.extend(
            origin
            for origin in [
                "http://localhost:3000",
                "http://127.0.0.1:3000",
            ]
            if origin not in cors_origins
        )
        CORS(
            self.app,
            resources={
                r"/api/.*": {
                    "origins": cors_origins,
                    "supports_credentials": True,
                    "methods": [
                        "GET",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                        "OPTIONS",
                    ],
                    "allow_headers": [
                        "Accept",
                        "Authorization",
                        "Content-Type",
                        "TOKEN",
                        "Username",
                        "Password",
                        "url",
                        "X-Jellyfin-Token",
                        "X-Zenstream-Username",
                    ],
                    "expose_headers": ["TOKEN"],
                }
            },
        )

        self.logger.info(f"CORS_ORIGINS runtime value: {os.getenv('CORS_ORIGINS')!r}")
        if os.getenv("SECRET_KEY") is None:
            raise Exception("Environment variable `SECRET_KEY` not set")

        self.app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
        self.app.config["RESTX_MASK_SWAGGER"] = False
        socketio.init_app(self.app)
        reloader_enabled = os.getenv("USE_RELOADER", "").lower() in {
            "1",
            "true",
            "yes",
        }
        api_blueprint = Blueprint("api", __name__, url_prefix="/api")

        self.api = Api(
            api_blueprint,
            authorizations={
                "token": {"type": "apiKey", "in": "header", "name": "TOKEN"},
            },
            security="token",
            version=self.version,
            title="ZenStream API",
            description="ZenStream Orchestrator API",
            doc="/swagger/",
        )

        for api_namespace in api_namespaces:
            self.api.add_namespace(api_namespace, "/")
            self.logger.info(f"Registered API namespace: {api_namespace.name}")

        self.app.register_blueprint(api_blueprint)

        self.serve()

    def serve(self):
        """Serve the Orchestrator."""
        if os.getenv("DEBUG"):
            self.logger.info("Serving Orchestrator in debug mode...")
            socketio.run(
                self.app,
                debug=True,
                host="127.0.0.1",
                port=int(os.getenv("ORCHESTRATOR_PORT", "9090")),
                use_reloader=os.getenv("USE_RELOADER", "").lower()
                in {"1", "true", "yes"},
            )
        else:
            self.logger.info("Serving Orchestrator...")
            socketio.run(
                self.app,
                host=os.getenv("ORCHESTRATOR_HOST", "127.0.0.1"),
                port=int(os.getenv("ORCHESTRATOR_PORT", "9090")),
                allow_unsafe_werkzeug=True,
            )
