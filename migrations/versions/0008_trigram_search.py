from alembic import op


revision = "0008_trigram_search"
down_revision = "0007_catalog_performance"
branch_labels = None
depends_on = None


def upgrade():
    # Existing rows contain only the short-search grams.  Startup rebuilds the
    # read model atomically so three-character grams and original-title rows
    # are generated from current projections before the catalog is served.
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )


def downgrade():
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )
