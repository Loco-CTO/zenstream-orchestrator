import sqlalchemy as sa
from alembic import op

revision = "0034_intro_outro_comparison_state"
down_revision = "0033_playback_viewers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "intro_outro_comparison_state",
        sa.Column("season_id", sa.Text(), nullable=False),
        sa.Column("comparison_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["season_id"], ["library_entities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("season_id"),
    )


def downgrade():
    op.drop_table("intro_outro_comparison_state")
