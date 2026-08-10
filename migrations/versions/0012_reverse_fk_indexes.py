"""Add reverse indexes for high-volume foreign-key cleanup paths."""

from alembic import op
import sqlalchemy as sa


revision = "0012_reverse_fk_indexes"
down_revision = "0011_admin_auth_hardening"
branch_labels = None
depends_on = None

_INDEXES = {
    "user_item_state": [("idx_user_item_state_entity", ["entity_id"]), ("idx_user_item_state_user", ["user_id"])],
    "user_library_access": [("idx_user_library_access_library", ["library_id"])],
    "library_sources": [("idx_library_sources_source", ["source_library_id"])],
    "media_files": [("idx_media_files_entity_role", ["entity_id", "role"])],
    "media_sources": [("idx_media_sources_media_file", ["media_file_id"]), ("idx_media_sources_entity", ["entity_id"])],
    "collection_members": [("idx_collection_members_source", ["source_entity_id"])],
    "playback_sessions": [("idx_playback_sessions_user_state", ["user_id", "state"]), ("idx_playback_sessions_expiry", ["expires_at"])],
    "trickplay_assets": [("idx_trickplay_assets_entity", ["entity_id"])],
    "trickplay_sheets": [("idx_trickplay_sheets_output", ["output_key"])],
    "intro_outro_assets": [("idx_intro_outro_assets_entity", ["entity_id"]), ("idx_intro_outro_assets_season", ["season_id"])],
    "intro_outro_segments": [("idx_intro_outro_segments_media_file", ["media_file_id"])],
    "entity_provider_ids": [("idx_entity_provider_ids_provider", ["provider", "provider_id"])],
    "entity_person_credits": [("idx_entity_person_credits_entity", ["entity_id"]), ("idx_entity_person_credits_person", ["person_id"])],
    "person_localizations": [("idx_person_localizations_person", ["person_id"])],
    "library_jobs": [("idx_library_jobs_library_state", ["library_id", "state"])],
    "job_runs": [("idx_job_runs_library_state", ["library_id", "state"]), ("idx_job_runs_definition", ["definition_id"])],
    "enrichment_queue": [("idx_enrichment_queue_entity_state", ["entity_id", "state"]), ("idx_enrichment_queue_library_state", ["library_id", "state"])],
}


def _tables(bind):
    return set(sa.inspect(bind).get_table_names())


def upgrade():
    bind = op.get_bind()
    tables = _tables(bind)
    for table, indexes in _INDEXES.items():
        if table not in tables:
            continue
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        for name, fields in indexes:
            if set(fields).issubset(columns):
                op.create_index(name, table, fields, if_not_exists=True)


def downgrade():
    bind = op.get_bind()
    tables = _tables(bind)
    for table, indexes in reversed(list(_INDEXES.items())):
        if table not in tables:
            continue
        for name, _fields in reversed(indexes):
            op.drop_index(name, table_name=table, if_exists=True)
