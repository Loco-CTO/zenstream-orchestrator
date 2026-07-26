from alembic import op
import sqlalchemy as sa


revision = "0018_playback_settings"
down_revision = "0017_playback_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "playback_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_transcodes", sa.Integer(), nullable=False),
        sa.Column("max_transcodes_per_user", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_playback_settings_singleton"),
        sa.CheckConstraint(
            "max_transcodes >= 1", name="ck_playback_settings_global_positive"
        ),
        sa.CheckConstraint(
            "max_transcodes_per_user >= 1", name="ck_playback_settings_user_positive"
        ),
    )


def downgrade():
    op.drop_table("playback_settings")
