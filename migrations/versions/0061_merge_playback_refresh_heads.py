"""Merge playback lease and refresh recovery migration branches."""

revision = "0061_merge_playback_refresh_heads"
down_revision = ("0060_playback_access_leases", "0060_refresh_attempt_recovery")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
