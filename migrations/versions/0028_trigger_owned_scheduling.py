import json

import sqlalchemy as sa
from alembic import op

revision = "0028_trigger_owned_scheduling"
down_revision = "0027_job_progress_detail"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    bind = op.get_bind()
    trigger_columns = _columns("job_schedule_triggers")
    if "options" not in trigger_columns:
        op.add_column(
            "job_schedule_triggers",
            sa.Column("options", sa.Text(), nullable=False, server_default="{}"),
        )
    run_columns = _columns("job_runs")
    if "source_trigger_id" not in run_columns:
        op.add_column(
            "job_runs",
            sa.Column("source_trigger_id", sa.String(length=36), nullable=True),
        )
    if "options" not in run_columns:
        op.add_column(
            "job_runs",
            sa.Column("options", sa.Text(), nullable=False, server_default="{}"),
        )

    definition_columns = _columns("job_definitions")
    if {"interval_minutes", "enabled"}.issubset(definition_columns):
        disabled = [
            row[0]
            for row in bind.execute(
                sa.text("SELECT id FROM job_definitions WHERE enabled=0")
            )
        ]
        for definition_id in disabled:
            bind.execute(
                sa.text("DELETE FROM job_schedule_triggers WHERE definition_id=:id"),
                {"id": definition_id},
            )
        # Preserve the existing metadata-refresh preference on every trigger.
        rows = bind.execute(
            sa.text("SELECT id,kind,config FROM job_definitions")
        ).fetchall()
        for definition_id, kind, config_text in rows:
            if kind != "metadata_refresh":
                continue
            try:
                config = json.loads(config_text or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            if "preserveCachedAssets" not in config:
                continue
            options = json.dumps(
                {"preserveCachedAssets": bool(config["preserveCachedAssets"])}
            )
            bind.execute(
                sa.text(
                    "UPDATE job_schedule_triggers SET options=:options WHERE definition_id=:id"
                ),
                {"options": options, "id": definition_id},
            )
            config.pop("preserveCachedAssets", None)
            bind.execute(
                sa.text("UPDATE job_definitions SET config=:config WHERE id=:id"),
                {"config": json.dumps(config), "id": definition_id},
            )
        bind.execute(
            sa.text(
                "UPDATE job_definitions SET next_run_at=(SELECT MIN(next_run_at) FROM job_schedule_triggers t WHERE t.definition_id=job_definitions.id AND t.next_run_at IS NOT NULL)"
            )
        )
        bind.execute(sa.text("DROP INDEX IF EXISTS idx_job_definitions_due"))
        with op.batch_alter_table("job_definitions", recreate="always") as batch:
            batch.drop_column("interval_minutes")
            batch.drop_column("enabled")
        bind.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_job_definitions_due ON job_definitions(next_run_at)"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "ALTER TABLE job_definitions ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 1440"
        )
    )
    bind.execute(
        sa.text(
            "ALTER TABLE job_definitions ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE job_definitions SET enabled=CASE WHEN EXISTS (SELECT 1 FROM job_schedule_triggers t WHERE t.definition_id=job_definitions.id) THEN 1 ELSE 0 END"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE job_definitions SET interval_minutes=COALESCE((SELECT CAST(interval_seconds / 60 AS INTEGER) FROM job_schedule_triggers t WHERE t.definition_id=job_definitions.id AND t.trigger_type='interval' LIMIT 1),1440)"
        )
    )
    if "options" in _columns("job_schedule_triggers"):
        op.drop_column("job_schedule_triggers", "options")
    if "options" in _columns("job_runs"):
        op.drop_column("job_runs", "options")
    if "source_trigger_id" in _columns("job_runs"):
        op.drop_column("job_runs", "source_trigger_id")
