from datetime import UTC, datetime


class OfflineService:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def server_time():
        return datetime.now(UTC).isoformat()

    def capabilities(self):
        rows = self.db.read_execute("SELECT instance_id FROM offline_server WHERE id=1")
        if not rows:
            raise RuntimeError("Offline identity migration is required")
        from app.offline_conversion import conversion_capabilities

        conversions = conversion_capabilities()
        profiles = ["original"]
        if conversions["video"]:
            profiles.extend(["video-720p", "video-1080p"])
        if conversions["audio"]:
            profiles.extend(["audio-128k", "audio-256k"])
        return {
            "serverInstanceId": rows[0][0],
            "serverTime": self.server_time(),
            "contractVersion": "offline-v1",
            "enabled": True,
            "features": [
                "account-sync",
                "selections",
                "assets",
                "bundles",
                "reconcile",
            ],
            "profiles": profiles,
            "hdrToneMapping": conversions["toneMapping"],
            "maxBatchSize": 100,
        }
