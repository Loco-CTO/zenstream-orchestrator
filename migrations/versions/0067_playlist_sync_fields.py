import sqlalchemy as sa
from alembic import op

revision = "0067_playlist_sync_fields"
down_revision = "0066_follow_sync_updates"
branch_labels = None
depends_on = None


def _stamp(user, target, field, *, clear_value=False):
    return (
        "INSERT INTO account_sync_fields(user_id,target_id,field,revision,activity_ms,event_id) "
        f"VALUES({user},{target},{field},1,CAST((julianday('now')-2440587.5)*86400000 AS INTEGER),'server') "
        "ON CONFLICT(user_id,target_id,field) DO UPDATE SET revision=revision+1,activity_ms=excluded.activity_ms,event_id='server'"
        + (",value_json=NULL" if clear_value else "")
        + ";"
    )


def upgrade():
    op.add_column(
        "account_sync_fields", sa.Column("value_json", sa.Text(), nullable=True)
    )
    op.execute("DROP TRIGGER sync_playlist_update")
    for field in ("name", "description"):
        op.execute(
            f"CREATE TRIGGER sync_playlist_update_{field} AFTER UPDATE OF {field} ON user_playlists WHEN NEW.{field} IS NOT OLD.{field} BEGIN "
            + _stamp("NEW.user_id", "NEW.id", f"'{field}'")
            + " END"
        )
    for action in ("INSERT", "UPDATE", "DELETE"):
        op.execute(f"DROP TRIGGER sync_membership_{action.lower()}")
        prefix = "OLD" if action == "DELETE" else "NEW"
        owner = f"(SELECT user_id FROM user_playlists WHERE id={prefix}.playlist_id)"
        body = (
            _stamp(owner, f"{prefix}.playlist_id", "'order'", clear_value=True)
            if action == "UPDATE"
            else _stamp(
                owner, f"{prefix}.playlist_id", f"'member:' || {prefix}.entity_id"
            )
        )
        op.execute(
            f"CREATE TRIGGER sync_membership_{action.lower()} AFTER {action} ON user_playlist_items WHEN {owner} IS NOT NULL BEGIN {body} END"
        )


def downgrade():
    for action in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER sync_membership_{action}")
    for field in ("name", "description"):
        op.execute(f"DROP TRIGGER sync_playlist_update_{field}")
    op.execute(
        "CREATE TRIGGER sync_playlist_update AFTER UPDATE ON user_playlists BEGIN "
        + _stamp("NEW.user_id", "NEW.id", "'name'")
        + _stamp("NEW.user_id", "NEW.id", "'description'")
        + " END"
    )
    for action in ("INSERT", "UPDATE", "DELETE"):
        prefix = "OLD" if action == "DELETE" else "NEW"
        owner = f"(SELECT user_id FROM user_playlists WHERE id={prefix}.playlist_id)"
        body = _stamp(
            owner, f"{prefix}.playlist_id", f"'member:' || {prefix}.entity_id"
        ) + _stamp(owner, f"{prefix}.playlist_id", "'order'")
        op.execute(
            f"CREATE TRIGGER sync_membership_{action.lower()} AFTER {action} ON user_playlist_items WHEN {owner} IS NOT NULL BEGIN {body} END"
        )
    op.drop_column("account_sync_fields", "value_json")
