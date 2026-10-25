from datetime import UTC, datetime


class OfflineService:
    def __init__(self, db):
        self.db = db

    def capabilities(self):
        rows = self.db.read_execute("SELECT instance_id FROM offline_server WHERE id=1")
        if not rows:
            raise RuntimeError("Offline identity migration is required")
        return {
            "serverInstanceId": rows[0][0],
            "serverTime": datetime.now(UTC).isoformat(),
            "contractVersion": "offline-v1",
            "enabled": False,
            "features": [],
            "profiles": [],
            "maxBatchSize": 100,
        }
