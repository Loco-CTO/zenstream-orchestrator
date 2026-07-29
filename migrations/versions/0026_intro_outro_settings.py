from alembic import op
import sqlalchemy as sa


revision = "0026_intro_outro_settings"
down_revision = "0025_people_credits"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("intro_outro_settings", sa.Column("analysis_percent", sa.Integer(), nullable=False, server_default="25"))
    op.add_column("intro_outro_settings", sa.Column("analysis_length_limit_minutes", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("intro_outro_settings", sa.Column("scan_introduction", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("intro_outro_settings", sa.Column("scan_credits", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("intro_outro_settings", sa.Column("minimum_intro_duration", sa.Integer(), nullable=False, server_default="15"))
    op.add_column("intro_outro_settings", sa.Column("maximum_intro_duration", sa.Integer(), nullable=False, server_default="120"))
    op.add_column("intro_outro_settings", sa.Column("minimum_credits_duration", sa.Integer(), nullable=False, server_default="15"))
    op.add_column("intro_outro_settings", sa.Column("maximum_credits_analysis_seconds", sa.Integer(), nullable=False, server_default="450"))
    op.add_column("intro_outro_settings", sa.Column("maximum_fingerprint_point_differences", sa.Integer(), nullable=False, server_default="6"))
    op.add_column("intro_outro_settings", sa.Column("maximum_time_skip_seconds", sa.Float(), nullable=False, server_default="3.5"))
    op.add_column("intro_outro_settings", sa.Column("inverted_index_shift", sa.Integer(), nullable=False, server_default="2"))
    op.add_column("intro_outro_assets", sa.Column("analysis_key", sa.Text(), nullable=False, server_default=""))


def downgrade():
    op.drop_column("intro_outro_assets", "analysis_key")
    for column in (
        "inverted_index_shift", "maximum_time_skip_seconds", "maximum_fingerprint_point_differences",
        "maximum_credits_analysis_seconds", "minimum_credits_duration", "maximum_intro_duration",
        "minimum_intro_duration", "scan_credits", "scan_introduction", "analysis_length_limit_minutes", "analysis_percent",
    ):
        op.drop_column("intro_outro_settings", column)
