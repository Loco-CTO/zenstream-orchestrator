from alembic import op


revision = "0020_unlimited_playback_settings"
down_revision = "0019_hls_seek_diagnostics"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("playback_settings", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_playback_settings_global_positive", type_="check"
        )
        batch_op.drop_constraint("ck_playback_settings_user_positive", type_="check")
        batch_op.create_check_constraint(
            "ck_playback_settings_global_non_negative", "max_transcodes >= 0"
        )
        batch_op.create_check_constraint(
            "ck_playback_settings_user_non_negative",
            "max_transcodes_per_user >= 0",
        )


def downgrade():
    with op.batch_alter_table("playback_settings", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_playback_settings_global_non_negative", type_="check"
        )
        batch_op.drop_constraint("ck_playback_settings_user_non_negative", type_="check")
        batch_op.create_check_constraint(
            "ck_playback_settings_global_positive", "max_transcodes >= 1"
        )
        batch_op.create_check_constraint(
            "ck_playback_settings_user_positive", "max_transcodes_per_user >= 1"
        )
