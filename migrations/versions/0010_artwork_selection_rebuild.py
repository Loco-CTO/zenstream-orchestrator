from alembic import op

revision = "0010_artwork_selection_rebuild"
down_revision = "0009_artwork_selection_provider"
branch_labels = None
depends_on = None


def upgrade():
    # 0009 adds provider provenance to existing selection rows.  Force the
    # read-model bootstrap to rebuild so installations that were already
    # complete at 0008 also backfill the new selection projection.
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )


def downgrade():
    # Keeping the model dirty is safe when rolling back: the older bootstrap
    # can rebuild its legacy projections on the next startup.
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )
