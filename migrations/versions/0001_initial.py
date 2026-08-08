from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


TABLES = [
    """CREATE TABLE users (
        id TEXT PRIMARY KEY NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0,
        password_scheme TEXT NOT NULL DEFAULT 'sha256'
    )""",
    "CREATE TABLE invites (url TEXT UNIQUE NOT NULL)",
    "CREATE TABLE admins (username TEXT PRIMARY KEY NOT NULL, password TEXT NOT NULL, is_root INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE admin_sessions (username TEXT NOT NULL, token TEXT UNIQUE NOT NULL, expiration TEXT NOT NULL)",
    """CREATE TABLE syncplay_groups (
        id TEXT PRIMARY KEY, host_user_id TEXT NOT NULL, host_name TEXT NOT NULL,
        allow_controls INTEGER NOT NULL DEFAULT 0, item_id TEXT, position REAL NOT NULL DEFAULT 0,
        playing INTEGER NOT NULL DEFAULT 0, resume INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 0, timeline_revision INTEGER NOT NULL DEFAULT 0,
        media_generation INTEGER NOT NULL DEFAULT 0, anchor_position REAL NOT NULL DEFAULT 0,
        anchor_time REAL NOT NULL DEFAULT 0, effective_at REAL NOT NULL DEFAULT 0,
        playback_state TEXT NOT NULL DEFAULT 'paused', pause_reason TEXT,
        host_disconnected_at REAL, ended INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL
    )""",
    """CREATE TABLE syncplay_members (
        group_id TEXT NOT NULL, user_id TEXT NOT NULL, participant_id TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL, viewing INTEGER NOT NULL DEFAULT 0, loading INTEGER NOT NULL DEFAULT 0,
        ready_generation INTEGER NOT NULL DEFAULT -1, presence_sequence INTEGER NOT NULL DEFAULT 0,
        watching_together INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(group_id, participant_id)
    )""",
    "CREATE TABLE syncplay_operations (operation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL, state TEXT NOT NULL)",
    "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    """CREATE TABLE metadata_credentials (
        provider TEXT PRIMARY KEY NOT NULL, ciphertext TEXT NOT NULL,
        credential_type TEXT NOT NULL DEFAULT 'api_key', validated_at TEXT, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE metadata_settings (
        key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE libraries (
        id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('tv_series','movies','music','collection')),
        directory TEXT, watch_enabled INTEGER NOT NULL DEFAULT 1,
        scan_interval_minutes INTEGER NOT NULL DEFAULT 1440, scan_state TEXT NOT NULL DEFAULT 'idle',
        scan_error TEXT, last_scan_started_at TEXT, last_scan_finished_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((type = 'collection' AND directory IS NULL) OR (type <> 'collection' AND directory IS NOT NULL))
    )""",
    """CREATE TABLE library_sources (
        library_id TEXT NOT NULL, source_library_id TEXT NOT NULL,
        PRIMARY KEY(library_id, source_library_id),
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
        FOREIGN KEY(source_library_id) REFERENCES libraries(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE library_entities (
        id TEXT PRIMARY KEY NOT NULL, library_id TEXT NOT NULL, parent_id TEXT,
        entity_type TEXT NOT NULL, relative_path TEXT, season_number INTEGER, episode_number INTEGER,
        episode_end_number INTEGER, disc_number INTEGER, track_number INTEGER,
        match_status TEXT NOT NULL DEFAULT 'unresolved', match_confidence REAL, match_method TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(library_id, entity_type, relative_path),
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
        FOREIGN KEY(parent_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE entity_provider_ids (
        entity_id TEXT NOT NULL, provider TEXT NOT NULL, identifier_type TEXT NOT NULL,
        provider_id TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(entity_id, provider, identifier_type),
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE media_files (
        id TEXT PRIMARY KEY NOT NULL, entity_id TEXT NOT NULL, relative_path TEXT NOT NULL,
        role TEXT NOT NULL, language TEXT, flags TEXT, size INTEGER NOT NULL DEFAULT 0,
        modified_ns INTEGER NOT NULL DEFAULT 0, file_hash TEXT, quick_fingerprint TEXT,
        image_blur_hash TEXT,
        UNIQUE(entity_id, relative_path, role),
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE collection_members (
        collection_entity_id TEXT NOT NULL, source_entity_id TEXT NOT NULL, position INTEGER NOT NULL,
        PRIMARY KEY(collection_entity_id, source_entity_id),
        FOREIGN KEY(collection_entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
        FOREIGN KEY(source_entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE library_jobs (
        id TEXT PRIMARY KEY NOT NULL, library_id TEXT NOT NULL, kind TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0,
        progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, created_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, error_details TEXT,
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE metadata_cache (
        provider TEXT NOT NULL, entity_type TEXT NOT NULL, provider_id TEXT NOT NULL, locale TEXT NOT NULL,
        payload TEXT NOT NULL, fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        PRIMARY KEY(provider, entity_type, provider_id, locale)
    )""",
    """CREATE TABLE metadata_images (
        provider TEXT NOT NULL, entity_type TEXT NOT NULL, provider_id TEXT NOT NULL, locale TEXT NOT NULL DEFAULT '',
        image_type TEXT NOT NULL, image_url TEXT NOT NULL, local_path TEXT, fetched_at TEXT,
        expires_at TEXT, blur_hash TEXT,
        PRIMARY KEY(provider, entity_type, provider_id, locale, image_type, image_url)
    )""",
    """CREATE TABLE job_definitions (
        id TEXT PRIMARY KEY NOT NULL, job_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        description TEXT, kind TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 1440,
        enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}', next_run_at TEXT,
        last_run_at TEXT, last_run_id TEXT, last_state TEXT NOT NULL DEFAULT 'idle',
        last_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE job_runs (
        id TEXT PRIMARY KEY NOT NULL, definition_id TEXT NOT NULL, library_id TEXT, kind TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0,
        progress_total INTEGER NOT NULL DEFAULT 0, message TEXT, error TEXT, created_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, thread_name TEXT, error_details TEXT,
        FOREIGN KEY(definition_id) REFERENCES job_definitions(id) ON DELETE CASCADE,
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE user_sessions (
        id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE user_library_access (
        user_id TEXT NOT NULL, library_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(user_id, library_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE account_preferences (
        user_id TEXT PRIMARY KEY NOT NULL, locale TEXT NOT NULL DEFAULT 'en', metadata_language TEXT,
        subtitle_font_family TEXT NOT NULL DEFAULT 'sans', subtitle_bold INTEGER NOT NULL DEFAULT 0,
        subtitle_text_scale REAL NOT NULL DEFAULT 100, subtitle_font_color TEXT NOT NULL DEFAULT '#ffffff',
        subtitle_border_size REAL NOT NULL DEFAULT 0, subtitle_border_color TEXT NOT NULL DEFAULT '#000000',
        subtitle_background_color TEXT NOT NULL DEFAULT '#000000', subtitle_background_opacity REAL NOT NULL DEFAULT 0,
        subtitle_renderer TEXT NOT NULL DEFAULT 'native',
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE user_item_state (
        user_id TEXT NOT NULL, entity_id TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
        played INTEGER NOT NULL DEFAULT 0, play_count INTEGER NOT NULL DEFAULT 0,
        position_seconds REAL NOT NULL DEFAULT 0, duration_seconds REAL NOT NULL DEFAULT 0,
        last_played_at TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(user_id, entity_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE media_sources (
        id TEXT PRIMARY KEY NOT NULL, entity_id TEXT NOT NULL, media_file_id TEXT NOT NULL,
        container TEXT, duration_seconds REAL, bitrate INTEGER, width INTEGER, height INTEGER,
        video_codec TEXT, audio_codec TEXT, probe_payload TEXT NOT NULL DEFAULT '{}', probed_at TEXT NOT NULL,
        UNIQUE(entity_id, media_file_id),
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
        FOREIGN KEY(media_file_id) REFERENCES media_files(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE playback_sessions (
        id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL, entity_id TEXT NOT NULL, source_id TEXT NOT NULL,
        mode TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active', output_directory TEXT,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, process_id INTEGER, profile_hash TEXT,
        requested_start_seconds REAL, audio_stream_id TEXT, last_accessed_at TEXT, started_at TEXT,
        completed_at TEXT, failure_code TEXT, failure_detail TEXT, actual_start_seconds REAL,
        seek_generation INTEGER, first_segment_duration_seconds REAL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
        FOREIGN KEY(source_id) REFERENCES media_sources(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE playback_settings (
        id INTEGER PRIMARY KEY, max_transcodes INTEGER NOT NULL, max_transcodes_per_user INTEGER NOT NULL,
        updated_at TEXT NOT NULL, trickplay_frame_width INTEGER NOT NULL DEFAULT 320,
        trickplay_frame_height INTEGER NOT NULL DEFAULT 180, trickplay_interval_seconds INTEGER NOT NULL DEFAULT 10,
        CONSTRAINT ck_playback_settings_singleton CHECK(id = 1),
        CONSTRAINT ck_playback_settings_global_non_negative CHECK(max_transcodes >= 0),
        CONSTRAINT ck_playback_settings_user_non_negative CHECK(max_transcodes_per_user >= 0)
    )""",
    """CREATE TABLE trickplay_assets (
        media_file_id TEXT PRIMARY KEY NOT NULL, entity_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
        frame_width INTEGER NOT NULL, frame_height INTEGER NOT NULL, interval_seconds INTEGER NOT NULL DEFAULT 10,
        state TEXT NOT NULL DEFAULT 'queued', output_key TEXT, error TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(media_file_id) REFERENCES media_files(id) ON DELETE CASCADE,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE trickplay_sheets (
        media_file_id TEXT NOT NULL, output_key TEXT NOT NULL, sheet_index INTEGER NOT NULL,
        first_frame INTEGER NOT NULL, frame_count INTEGER NOT NULL, relative_path TEXT NOT NULL,
        PRIMARY KEY(media_file_id, output_key, sheet_index),
        FOREIGN KEY(media_file_id) REFERENCES media_files(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE intro_outro_settings (
        id INTEGER PRIMARY KEY, scan_on_added BOOLEAN NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
        analysis_percent INTEGER NOT NULL DEFAULT 25, analysis_length_limit_minutes INTEGER NOT NULL DEFAULT 10,
        scan_introduction BOOLEAN NOT NULL DEFAULT 1, scan_credits BOOLEAN NOT NULL DEFAULT 1,
        minimum_intro_duration INTEGER NOT NULL DEFAULT 15, maximum_intro_duration INTEGER NOT NULL DEFAULT 120,
        minimum_credits_duration INTEGER NOT NULL DEFAULT 15, maximum_credits_analysis_seconds INTEGER NOT NULL DEFAULT 450,
        maximum_fingerprint_point_differences INTEGER NOT NULL DEFAULT 6,
        maximum_time_skip_seconds REAL NOT NULL DEFAULT 3.5, inverted_index_shift INTEGER NOT NULL DEFAULT 2
    )""",
    """CREATE TABLE intro_outro_assets (
        media_file_id TEXT PRIMARY KEY NOT NULL, entity_id TEXT NOT NULL, season_id TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL, intro_fingerprint BLOB, outro_fingerprint BLOB,
        state TEXT NOT NULL DEFAULT 'queued', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        analysis_key TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(media_file_id) REFERENCES media_files(id) ON DELETE CASCADE,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
        FOREIGN KEY(season_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE intro_outro_segments (
        media_file_id TEXT NOT NULL, segment_type TEXT NOT NULL, start_seconds REAL NOT NULL,
        end_seconds REAL NOT NULL, PRIMARY KEY(media_file_id, segment_type),
        FOREIGN KEY(media_file_id) REFERENCES media_files(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE people (
        id TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL, provider_person_id TEXT NOT NULL,
        image_url TEXT, local_path TEXT, image_blur_hash TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(provider, provider_person_id)
    )""",
    """CREATE TABLE person_localizations (
        person_id TEXT NOT NULL, locale TEXT NOT NULL, name TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(person_id, locale), FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE entity_person_credits (
        id TEXT PRIMARY KEY NOT NULL, entity_id TEXT NOT NULL, person_id TEXT NOT NULL, provider TEXT NOT NULL,
        locale TEXT NOT NULL, credit_type TEXT NOT NULL, role TEXT, department TEXT,
        credit_order INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
        FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE enrichment_queue (
        id TEXT PRIMARY KEY NOT NULL, entity_id TEXT NOT NULL, library_id TEXT NOT NULL,
        kind TEXT NOT NULL, locale TEXT, priority INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT, lease_owner TEXT, lease_expires_at TEXT,
        source_job_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(entity_id, kind, locale),
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE catalog_entity_rollups (
        entity_id TEXT PRIMARY KEY NOT NULL, library_id TEXT NOT NULL,
        descendant_count INTEGER NOT NULL DEFAULT 0, playable_count INTEGER NOT NULL DEFAULT 0,
        played_leaf_count INTEGER NOT NULL DEFAULT 0, unplayed_leaf_count INTEGER NOT NULL DEFAULT 0,
        added_ns INTEGER, last_added_ns INTEGER, generation INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE,
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE catalog_user_rollups (
        user_id TEXT NOT NULL, entity_id TEXT NOT NULL,
        favorite INTEGER NOT NULL DEFAULT 0, played INTEGER NOT NULL DEFAULT 0,
        play_count INTEGER NOT NULL DEFAULT 0,
        played_leaf_count INTEGER NOT NULL DEFAULT 0, unplayed_leaf_count INTEGER NOT NULL DEFAULT 0,
        position_seconds REAL NOT NULL DEFAULT 0, duration_seconds REAL NOT NULL DEFAULT 0,
        last_played_at TEXT, generation INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        PRIMARY KEY(user_id, entity_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE catalog_metadata_projection (
        entity_id TEXT NOT NULL, locale TEXT NOT NULL, payload TEXT NOT NULL,
        updated_at TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(entity_id, locale),
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE catalog_home_projection (
        user_id TEXT, library_id TEXT, section TEXT NOT NULL, entity_id TEXT NOT NULL,
        rank INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        PRIMARY KEY(user_id, library_id, section, entity_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE,
        FOREIGN KEY(entity_id) REFERENCES library_entities(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE catalog_projection_status (
        library_id TEXT PRIMARY KEY NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', progress_current INTEGER NOT NULL DEFAULT 0,
        progress_total INTEGER NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL,
        FOREIGN KEY(library_id) REFERENCES libraries(id) ON DELETE CASCADE
    )""",
]

INDEXES = [
    "CREATE UNIQUE INDEX idx_users_id ON users(id)",
    "CREATE INDEX idx_library_entities_parent ON library_entities(library_id, parent_id)",
    "CREATE INDEX idx_library_entities_status ON library_entities(library_id, match_status)",
    "CREATE INDEX idx_entity_provider_ids_lookup ON entity_provider_ids(provider, identifier_type, provider_id)",
    "CREATE INDEX idx_entity_provider_ids_provider_id ON entity_provider_ids(provider, provider_id, entity_id)",
    "CREATE INDEX idx_library_jobs_state ON library_jobs(library_id, state, created_at)",
    "CREATE INDEX idx_library_jobs_global_queue ON library_jobs(state, created_at)",
    "CREATE INDEX idx_job_definitions_due ON job_definitions(enabled, next_run_at)",
    "CREATE INDEX idx_job_runs_queue ON job_runs(state, created_at)",
    "CREATE INDEX idx_job_runs_definition ON job_runs(definition_id, created_at)",
    "CREATE INDEX idx_user_sessions_user ON user_sessions(user_id, expires_at)",
    "CREATE INDEX idx_user_item_state_resume ON user_item_state(user_id, last_played_at)",
    "CREATE INDEX idx_media_sources_entity ON media_sources(entity_id)",
    "CREATE INDEX idx_trickplay_assets_state ON trickplay_assets(state, updated_at)",
    "CREATE INDEX idx_trickplay_assets_entity ON trickplay_assets(entity_id)",
    "CREATE INDEX idx_intro_outro_assets_state ON intro_outro_assets(state, updated_at)",
    "CREATE INDEX idx_intro_outro_assets_season ON intro_outro_assets(season_id)",
    "CREATE INDEX idx_entity_person_credits_item ON entity_person_credits(entity_id, locale, credit_type, credit_order)",
    "CREATE INDEX idx_entity_person_credits_person ON entity_person_credits(person_id)",
    "CREATE INDEX idx_user_sessions_expiry ON user_sessions(expires_at)",
    "CREATE INDEX idx_library_entities_hierarchy_order ON library_entities(library_id, entity_type, parent_id, season_number, episode_number, relative_path)",
    "CREATE INDEX idx_media_files_entity_role_modified ON media_files(entity_id, role, modified_ns)",
    "CREATE INDEX idx_enrichment_queue_state_priority ON enrichment_queue(state, priority DESC, created_at)",
    "CREATE INDEX idx_enrichment_queue_library ON enrichment_queue(library_id, state, updated_at)",
    "CREATE INDEX idx_catalog_entity_rollups_library ON catalog_entity_rollups(library_id, last_added_ns)",
    "CREATE INDEX idx_catalog_user_rollups_user ON catalog_user_rollups(user_id, last_played_at)",
    "CREATE INDEX idx_catalog_metadata_locale ON catalog_metadata_projection(locale, entity_id)",
    "CREATE INDEX idx_catalog_home_lookup ON catalog_home_projection(user_id, section, rank)",
    "CREATE INDEX idx_metadata_images_url_path_ready ON metadata_images(image_url, local_path) WHERE blur_hash IS NOT NULL",
    "CREATE INDEX idx_metadata_images_type_url_fetched ON metadata_images(image_type, image_url, fetched_at DESC)",
]


def upgrade():
    for statement in TABLES:
        op.execute(sa.text(statement))
    op.execute(sa.text("CREATE VIRTUAL TABLE catalog_search USING fts5(entity_id UNINDEXED, library_id UNINDEXED, locale UNINDEXED, title, tokenize='trigram')"))
    for statement in INDEXES:
        op.execute(sa.text(statement))
    op.execute(sa.text("INSERT INTO schema_metadata(key,value) VALUES('generation','catalog-projection-v1')"))
    op.execute(sa.text("INSERT INTO intro_outro_settings(id,scan_on_added,updated_at) VALUES(1,1,CURRENT_TIMESTAMP)"))


def downgrade():
    op.execute(sa.text("DROP TABLE catalog_search"))
    for table in reversed([statement.split()[2] for statement in TABLES]):
        op.execute(sa.text(f"DROP TABLE {table}"))
