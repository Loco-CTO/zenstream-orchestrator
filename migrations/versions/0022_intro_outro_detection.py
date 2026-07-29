from alembic import op
import sqlalchemy as sa


revision = "0022_intro_outro_detection"
down_revision = "0021_trickplay"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "intro_outro_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scan_on_added", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.execute("INSERT INTO intro_outro_settings(id,scan_on_added,updated_at) VALUES(1,1,CURRENT_TIMESTAMP)")
    op.create_table(
        "intro_outro_assets",
        sa.Column("media_file_id", sa.Text(), primary_key=True),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column("intro_fingerprint", sa.LargeBinary()),
        sa.Column("outro_fingerprint", sa.LargeBinary()),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["media_file_id"], ["media_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["library_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["season_id"], ["library_entities.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "intro_outro_segments",
        sa.Column("media_file_id", sa.Text(), nullable=False),
        sa.Column("segment_type", sa.Text(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("media_file_id", "segment_type"),
        sa.ForeignKeyConstraint(["media_file_id"], ["media_files.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_intro_outro_assets_state", "intro_outro_assets", ["state", "updated_at"])
    op.create_index("idx_intro_outro_assets_season", "intro_outro_assets", ["season_id"])


def downgrade():
    op.drop_index("idx_intro_outro_assets_season", table_name="intro_outro_assets")
    op.drop_index("idx_intro_outro_assets_state", table_name="intro_outro_assets")
    op.drop_table("intro_outro_segments")
    op.drop_table("intro_outro_assets")
    op.drop_table("intro_outro_settings")
