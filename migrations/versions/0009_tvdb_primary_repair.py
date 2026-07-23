from alembic import op
import sqlalchemy as sa


revision = "0009_tvdb_primary_repair"
down_revision = "0008_metadata_diagnostics"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        UPDATE entity_provider_ids
        SET is_primary = CASE
            WHEN provider='tvdb' AND identifier_type IN ('series','season','episode','collection') THEN 1
            WHEN provider='tmdb' AND identifier_type='movie' THEN 1
            WHEN provider='musicbrainz' AND identifier_type IN ('artist','release','release_group','track','recording') THEN 1
            ELSE 0
        END
    """))
    op.execute(sa.text("""
        UPDATE library_entities
        SET match_status='unresolved', match_confidence=NULL, match_method='tvdb_primary_repair', updated_at=datetime('now')
        WHERE entity_type IN ('series','season','episode')
          AND NOT EXISTS (
              SELECT 1 FROM entity_provider_ids
              WHERE entity_provider_ids.entity_id=library_entities.id
                AND entity_provider_ids.provider='tvdb'
          )
    """))


def downgrade():
    pass
