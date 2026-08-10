import logging
import os
from datetime import datetime

from termcolor import colored


class Logger:
    LOG_LEVELS = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    def __init__(self, level="DEBUG"):
        """
        Initializes the Logger instance by setting up both file and console handlers.
        """
        name = "ZENSTREAM_ORCHESTRATOR"
        self.logger = logging.getLogger(name)
        self.logger.setLevel(self._get_logging_level(level))

        self.logs_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(self.logs_dir, exist_ok=True)

        self._configure_console_handler()
        self._configure_file_handler()

    @property
    def __class__(self):
        return super().__class__

    def _get_logging_level(self, level_str):
        """
        Converts the logging level string to corresponding logging level constant.

        Args:
            level_str (str): Logging level as a string.

        Returns:
            int: Corresponding logging level constant.
        """
        return self.LOG_LEVELS.get(level_str, logging.DEBUG)

    def _configure_console_handler(self):
        """
        Configures the console handler to output messages to the console with a specific formatter.
        """
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.logger.level)

        formatter = logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")
        console_handler.setFormatter(formatter)

        self.logger.addHandler(console_handler)

    def _configure_file_handler(self):
        """Configures file handler with timestamped filename"""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = os.path.join(self.logs_dir, f"{timestamp}.txt")

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(self.logger.level)

        formatter = logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")
        file_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)

    @staticmethod
    def _color_message(level, message):
        """
        Colors the log message based on the log level.

        Args:
            level (str): The level of the log message.
            message (str): The message to log.

        Returns:
            str: The colored message.
        """
        colors = {
            "DEBUG": "blue",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "magenta",
        }
        return colored(message, colors.get(level, "white"))

    def set_level(self, level):
        """
        Sets the logging level dynamically.

        Args:
            level (str): The new logging level as a string (e.g., 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL').
        """
        self.logger.setLevel(self._get_logging_level(level))
        for handler in self.logger.handlers:
            handler.setLevel(self.logger.level)

    def debug(self, message):
        """
        Logs a debug message.

        Args:
            message (str): The message to log.
        """
        self.logger.debug(self._color_message("DEBUG", message))

    def info(self, message):
        """
        Logs an informational message.

        Args:
            message (str): The message to log.
        """
        self.logger.info(self._color_message("INFO", message))

    def warning(self, message):
        """
        Logs a warning message.

        Args:
            message (str): The message to log.
        """
        self.logger.warning(self._color_message("WARNING", message))

    def error(self, message):
        """
        Logs an error message.

        Args:
            message (str): The message to log.
        """
        self.logger.error(self._color_message("ERROR", message))

    def critical(self, message):
        """
        Logs a critical message.

        Args:
            message (str): The message to log.
        """
        self.logger.critical(self._color_message("CRITICAL", message))
