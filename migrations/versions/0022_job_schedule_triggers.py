"""Persist multiple scheduler triggers per job definition."""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0022_job_schedule_triggers"
down_revision = "0021_remove_catalog_discovery_added_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_schedule_triggers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("definition_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("time_of_day", sa.String(length=5), nullable=True),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("next_run_at", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
    )
    op.create_index(
        "idx_job_schedule_triggers_definition",
        "job_schedule_triggers",
        ["definition_id"],
    )
    op.create_index(
        "idx_job_schedule_triggers_due",
        "job_schedule_triggers",
        ["next_run_at"],
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, interval_minutes, created_at, updated_at FROM job_definitions")
    ).fetchall()
    for definition_id, interval_minutes, created_at, updated_at in rows:
        seconds = max(1, int(interval_minutes or 1440) * 60)
        bind.execute(
            sa.text(
                "INSERT INTO job_schedule_triggers "
                "(id,definition_id,trigger_type,interval_seconds,next_run_at,created_at,updated_at) "
                "SELECT :id,:definition_id,'interval',:seconds,next_run_at,:created_at,:updated_at "
                "FROM job_definitions WHERE id=:definition_id"
            ),
            {
                "id": str(uuid.uuid4()),
                "definition_id": definition_id,
                "seconds": seconds,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )


def downgrade() -> None:
    op.drop_index("idx_job_schedule_triggers_due", table_name="job_schedule_triggers")
    op.drop_index(
        "idx_job_schedule_triggers_definition",
        table_name="job_schedule_triggers",
    )
    op.drop_table("job_schedule_triggers")
