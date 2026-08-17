import json

import sqlalchemy as sa
from alembic import op

try:
    from app.language_registry import normalize_track_language
except ModuleNotFoundError as error:
    if error.name not in {"app", "app.language_registry"}:
        raise
    from orchestrator.app.language_registry import normalize_track_language


revision = "0032_playback_language_preferences"
down_revision = "0031_canonical_language_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_preferences", sa.Column("audio_language", sa.Text(), nullable=True)
    )
    op.add_column(
        "account_preferences", sa.Column("subtitle_language", sa.Text(), nullable=True)
    )
    op.create_table(
        "media_track_languages",
        sa.Column("media_file_id", sa.Text(), nullable=False),
        sa.Column("track_type", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("media_file_id", "track_type", "language"),
        sa.ForeignKeyConstraint(
            ["media_file_id"], ["media_files.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("track_type IN ('audio','subtitle')"),
    )
    op.create_index(
        "idx_media_track_languages_type_language",
        "media_track_languages",
        ["track_type", "language", "media_file_id"],
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT media_file_id,probe_payload FROM media_sources")
    ).fetchall()
    values: list[dict[str, str]] = []
    for media_file_id, raw_payload in rows:
        try:
            payload = json.loads(raw_payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        streams = payload.get("streams") if isinstance(payload, dict) else None
        for stream in streams if isinstance(streams, list) else []:
            if not isinstance(stream, dict):
                continue
            track_type = {"audio": "audio", "subtitle": "subtitle"}.get(
                str(stream.get("codec_type") or "").lower()
            )
            if track_type is None:
                continue
            tags = stream.get("tags")
            tags = tags if isinstance(tags, dict) else {}
            language = normalize_track_language(
                tags.get("language") or tags.get("LANGUAGE")
            )
            if language:
                values.append(
                    {
                        "media_file_id": media_file_id,
                        "track_type": track_type,
                        "language": language,
                    }
                )
    if values:
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO media_track_languages "
                "(media_file_id,track_type,language) VALUES "
                "(:media_file_id,:track_type,:language)"
            ),
            values,
        )


def downgrade() -> None:
    op.drop_index(
        "idx_media_track_languages_type_language",
        table_name="media_track_languages",
    )
    op.drop_table("media_track_languages")
    op.drop_column("account_preferences", "subtitle_language")
    op.drop_column("account_preferences", "audio_language")
