import sqlalchemy as sa
from alembic import op

revision = "0049_artist_follow_notifications"
down_revision = "0048_music_artist_credits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_follow_targets", recreate="always") as batch:
        batch.drop_constraint("ck_user_follow_targets_type", type_="check")
        batch.drop_constraint("ck_user_follow_targets_provider", type_="check")
        batch.create_check_constraint(
            "ck_user_follow_targets_type",
            "target_type IN ('movie','series','artist')",
        )
        batch.create_check_constraint(
            "ck_user_follow_targets_provider",
            "provider IN ('tmdb','tvdb','musicbrainz','entity')",
        )

    with op.batch_alter_table("catalog_admissions", recreate="always") as batch:
        batch.drop_constraint("ck_catalog_admissions_type", type_="check")
        batch.create_check_constraint(
            "ck_catalog_admissions_type",
            "entity_type IN ('movie','episode','track')",
        )

    op.execute(
        sa.text(
            "INSERT INTO catalog_admissions(entity_id,library_id,entity_type,admitted_at) "
            "SELECT e.id,e.library_id,e.entity_type,e.created_at "
            "FROM library_entities e "
            "WHERE e.entity_type='track' "
            "AND EXISTS (SELECT 1 FROM media_files m WHERE m.entity_id=e.id AND m.role='media')"
        )
    )

    with op.batch_alter_table("notifications", recreate="always") as batch:
        batch.add_column(sa.Column("artist_id", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_notifications_artist_id",
            "library_entities",
            ["artist_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.drop_constraint("ck_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_notifications_kind",
            "kind IN ('new_episode','new_movie','new_release')",
        )


def downgrade() -> None:
    with op.batch_alter_table("notifications", recreate="always") as batch:
        batch.drop_constraint("fk_notifications_artist_id", type_="foreignkey")
        batch.drop_constraint("ck_notifications_kind", type_="check")
        batch.drop_column("artist_id")
        batch.create_check_constraint(
            "ck_notifications_kind",
            "kind IN ('new_episode','new_movie')",
        )
    op.execute("DELETE FROM catalog_admissions WHERE entity_type='track'")
    with op.batch_alter_table("catalog_admissions", recreate="always") as batch:
        batch.drop_constraint("ck_catalog_admissions_type", type_="check")
        batch.create_check_constraint(
            "ck_catalog_admissions_type",
            "entity_type IN ('movie','episode')",
        )
    op.execute("DELETE FROM user_follow_targets WHERE target_type='artist'")
    with op.batch_alter_table("user_follow_targets", recreate="always") as batch:
        batch.drop_constraint("ck_user_follow_targets_type", type_="check")
        batch.drop_constraint("ck_user_follow_targets_provider", type_="check")
        batch.create_check_constraint(
            "ck_user_follow_targets_type",
            "target_type IN ('movie','series')",
        )
        batch.create_check_constraint(
            "ck_user_follow_targets_provider",
            "provider IN ('tmdb','tvdb','entity')",
        )
