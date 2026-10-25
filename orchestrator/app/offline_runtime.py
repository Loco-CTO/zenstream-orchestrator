import threading

from app.catalog import Catalog
from app.logging_config import get_logger
from app.offline_selections import OfflineSelections

logger = get_logger("offline")


class OfflineRuntime:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run, name="offline-selection-worker", daemon=True
        )
        self.thread.start()

    def _run(self):
        selections = OfflineSelections(Catalog())
        while not self.stop_event.is_set():
            worked = False
            try:
                worked = selections.process_next()
            except Exception:
                logger.exception("offline selection worker failed")
            self.stop_event.wait(0.05 if worked else 2)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)


runtime = OfflineRuntime()
