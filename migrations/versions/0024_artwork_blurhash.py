from alembic import op
import sqlalchemy as sa


revision = "0024_artwork_blurhash"
down_revision = "0023_subtitle_renderer"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("metadata_images", sa.Column("blur_hash", sa.Text(), nullable=True))
    op.add_column("media_files", sa.Column("image_blur_hash", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("media_files", "image_blur_hash")
    op.drop_column("metadata_images", "blur_hash")
