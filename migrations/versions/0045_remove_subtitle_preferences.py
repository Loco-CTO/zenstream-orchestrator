import sqlalchemy as sa
from alembic import op

revision = "0045_remove_subtitle_preferences"
down_revision = "0044_bazarr_movie_mapping_cache"
branch_labels = None
depends_on = None


_SUBTITLE_STYLE_COLUMNS = (
    ("subtitle_font_family", sa.Text(), "'sans'"),
    ("subtitle_bold", sa.Integer(), "0"),
    ("subtitle_text_scale", sa.Float(), "100"),
    ("subtitle_font_color", sa.Text(), "'#ffffff'"),
    ("subtitle_border_size", sa.Float(), "2"),
    ("subtitle_border_color", sa.Text(), "'#000000'"),
    ("subtitle_background_color", sa.Text(), "'#000000'"),
    ("subtitle_background_opacity", sa.Float(), "0"),
    ("subtitle_renderer", sa.Text(), "'native'"),
)


def upgrade() -> None:
    with op.batch_alter_table("account_preferences", recreate="always") as batch:
        for name, _, _ in _SUBTITLE_STYLE_COLUMNS:
            batch.drop_column(name)


def downgrade() -> None:
    with op.batch_alter_table("account_preferences", recreate="always") as batch:
        for name, column_type, default in _SUBTITLE_STYLE_COLUMNS:
            batch.add_column(
                sa.Column(
                    name,
                    column_type,
                    nullable=False,
                    server_default=sa.text(default),
                )
            )
