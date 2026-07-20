"""Persistent scheduler definitions and non-blocking metadata job runs."""

from alembic import op
import sqlalchemy as sa


revision = "0006_scheduler_jobs"
down_revision = "0005_metadata_libraries"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS job_definitions (
            id TEXT PRIMARY KEY NOT NULL,
            job_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            kind TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL DEFAULT 1440,
            enabled INTEGER NOT NULL DEFAULT 1,
            config TEXT NOT NULL DEFAULT '{}',
            next_run_at TEXT,
            last_run_at TEXT,
            last_run_id TEXT,
            last_state TEXT NOT NULL DEFAULT 'idle',
            last_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """))
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS job_runs (
            id TEXT PRIMARY KEY NOT NULL,
            definition_id TEXT NOT NULL,
            library_id TEXT,
            kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            thread_name TEXT,
            FOREIGN KEY(definition_id) REFERENCES job_definitions(id) ON DELETE CASCADE,
            FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_job_definitions_due ON job_definitions(enabled, next_run_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_job_runs_queue ON job_runs(state, created_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_job_runs_definition ON job_runs(definition_id, created_at)"))


def downgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS job_runs"))
    op.execute(sa.text("DROP TABLE IF EXISTS job_definitions"))
