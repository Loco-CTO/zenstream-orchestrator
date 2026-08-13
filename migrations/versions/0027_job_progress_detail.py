from alembic import op
from sqlalchemy import Column, Integer, Text


revision = "0027_job_progress_detail"
down_revision = "0026_catalog_user_state_indexes"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("progress_phase", Text()),
    ("progress_label", Text()),
    ("progress_stage_current", Integer()),
    ("progress_stage_total", Integer()),
    ("progress_stage_unit", Text()),
    ("progress_current_item", Text()),
)


def upgrade():
    for table in ("job_runs", "library_jobs"):
        for name, column_type in _COLUMNS:
            op.add_column(table, Column(name, column_type, nullable=True))


def downgrade():
    for table in ("library_jobs", "job_runs"):
        for name, _column_type in reversed(_COLUMNS):
            op.drop_column(table, name)
