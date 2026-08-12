from alembic import op


revision = "0023_global_artwork_selection_rebuild"
down_revision = "0022_job_schedule_triggers"
branch_labels = None
depends_on = None


def upgrade():
    # Recompute selections with language precedence across all provider
    # identities. Older installations may have provider-at-a-time rows whose
    # versioned projection disagrees with the ready cache file.
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )


def downgrade():
    op.execute(
        "UPDATE catalog_read_model_status "
        "SET state='building',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1"
    )
