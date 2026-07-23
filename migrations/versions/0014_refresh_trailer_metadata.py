from alembic import op
import sqlalchemy as sa


revision = "0014_refresh_trailer_metadata"
down_revision = "0013_remove_metadata_hydration"
branch_labels = None
depends_on = None


def upgrade():
    # Trailer URLs and language tags are provider-specific and were not
    # normalized consistently by older cache entries.  The next scan or
    # Refresh metadata run must fetch those records again.
    op.execute(
        sa.text(
            "UPDATE metadata_cache SET expires_at='1970-01-01T00:00:00+00:00' "
            "WHERE payload LIKE '%\"trailers\"%'"
        )
    )


def downgrade():
    pass
