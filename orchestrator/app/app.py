### ======================================================================
### ZenStream Orchestrator
### Copyright 2019-2024 © Rystal. All Rights Reserved.
### ======================================================================

import os

from waitress import serve
from flask import Blueprint, Flask
from flask_restx import Api, Resource, fields
from logger import Logger
from .config import load_config

from api import api_namespaces


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

        if os.getenv("SECRET_KEY") is None:
            raise Exception("Environment variable `SECRET_KEY` not set")

        self.app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
        self.app.config["RESTX_MASK_SWAGGER"] = False

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
            self.app.run(debug=True, host="0.0.0.0", port=5090, use_reloader=False)
        else:
            self.logger.info("Serving Orchestrator...")
            serve(self.app, host="127.0.0.1", port=5090)
