from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

from app.paths import metadata_directory

_configured = False
_lock = __import__("threading").Lock()
_secret_pattern = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization|access)=?[^\s,;&\"']+"
)


def redact(value: object) -> str:
    return _secret_pattern.sub(r"\1=<redacted>", str(value))


class ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        context = {key: value for key, value in self.extra.items() if value is not None}
        prefix = " ".join(f"{key}={redact(value)}" for key, value in context.items())
        return (f"[{prefix}] {redact(msg)}" if prefix else redact(msg)), kwargs


class RedactingFormatter(logging.Formatter):
    """Redact interpolated arguments and exception tracebacks as well as messages."""

    def format(self, record):
        return redact(super().format(record))


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    with _lock:
        if _configured:
            return
        level = getattr(
            logging, os.getenv("ZENSTREAM_LOG_LEVEL", "INFO").upper(), logging.INFO
        )
        root = logging.getLogger("zenstream")
        root.setLevel(level)
        root.propagate = False
        formatter = RedactingFormatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(name)s %(message)s"
        )
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(level)
        root.addHandler(console)
        log_directory = metadata_directory() / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_directory / "orchestrator.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
        for logger_name in ("uvicorn.access", "uvicorn.error"):
            external_logger = logging.getLogger(logger_name)
            for handler in external_logger.handlers:
                handler.setFormatter(formatter)
        _configured = True


def get_logger(name: str, **context) -> logging.LoggerAdapter:
    configure_logging()
    return ContextAdapter(logging.getLogger(f"zenstream.{name}"), context)
