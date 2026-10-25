import sqlalchemy as sa
from alembic import op

revision = "0065_account_sync"
down_revision = "0064_offline_identity"
branch_labels = None
depends_on = None


def _stamp(user, target, field, *, expression=False):
    key = field if expression else f"'{field}'"
    return (
        "INSERT INTO account_sync_fields(user_id,target_id,field,revision,activity_ms,event_id) "
        f"VALUES({user},{target},{key},1,CAST((julianday('now')-2440587.5)*86400000 AS INTEGER),'server') "
        "ON CONFLICT(user_id,target_id,field) DO UPDATE SET revision=revision+1,"
        "activity_ms=excluded.activity_ms,event_id='server';"
    )


def upgrade():
    op.create_table(
        "account_sync_epochs",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("history_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("privacy_epoch", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "account_sync_devices",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("device_id", sa.Text(), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "account_sync_fields",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("target_id", sa.Text(), primary_key=True),
        sa.Column("field", sa.Text(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("activity_ms", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
    )
    for action in ("INSERT", "UPDATE"):
        for field, changed in (
            ("favorite", "NEW.favorite IS NOT OLD.favorite"),
            (
                "playback",
                "NEW.played IS NOT OLD.played OR NEW.position_seconds IS NOT OLD.position_seconds OR NEW.duration_seconds IS NOT OLD.duration_seconds",
            ),
        ):
            condition = "" if action == "INSERT" else f" WHEN {changed}"
            op.execute(
                f"CREATE TRIGGER sync_state_{action.lower()}_{field} AFTER {action} ON user_item_state{condition} BEGIN "
                + _stamp("NEW.user_id", "NEW.entity_id", field)
                + " END"
            )
    for action, prefix in (("INSERT", "NEW"), ("DELETE", "OLD")):
        op.execute(
            f"CREATE TRIGGER sync_follow_{action.lower()} AFTER {action} ON user_follow_targets WHEN {prefix}.entity_id IS NOT NULL BEGIN "
            + _stamp(f"{prefix}.user_id", f"{prefix}.entity_id", "following")
            + " END"
        )
    op.execute(
        "CREATE TRIGGER sync_history_privacy AFTER UPDATE OF watch_history_enabled ON account_preferences "
        "WHEN NEW.watch_history_enabled IS NOT OLD.watch_history_enabled BEGIN "
        "INSERT INTO account_sync_epochs(user_id,privacy_epoch) VALUES(NEW.user_id,1) "
        "ON CONFLICT(user_id) DO UPDATE SET privacy_epoch=privacy_epoch+1; END"
    )
    for action in ("INSERT", "UPDATE", "DELETE"):
        prefix = "OLD" if action == "DELETE" else "NEW"
        fields = ("deleted",) if action == "DELETE" else ("name", "description")
        body = "".join(
            _stamp(f"{prefix}.user_id", f"{prefix}.id", field) for field in fields
        )
        op.execute(
            f"CREATE TRIGGER sync_playlist_{action.lower()} AFTER {action} ON user_playlists BEGIN {body} END"
        )
        owner = f"(SELECT user_id FROM user_playlists WHERE id={prefix}.playlist_id)"
        body = _stamp(
            owner,
            f"{prefix}.playlist_id",
            f"'member:' || {prefix}.entity_id",
            expression=True,
        )
        body += _stamp(owner, f"{prefix}.playlist_id", "order")
        op.execute(
            f"CREATE TRIGGER sync_membership_{action.lower()} AFTER {action} ON user_playlist_items WHEN {owner} IS NOT NULL BEGIN {body} END"
        )


def downgrade():
    for action in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER sync_playlist_{action}")
        op.execute(f"DROP TRIGGER sync_membership_{action}")
    op.execute("DROP TRIGGER sync_history_privacy")
    for action in ("insert", "update"):
        for field in ("favorite", "playback"):
            op.execute(f"DROP TRIGGER sync_state_{action}_{field}")
    for action in ("insert", "delete"):
        op.execute(f"DROP TRIGGER sync_follow_{action}")
    for name in ("account_sync_fields", "account_sync_devices", "account_sync_epochs"):
        op.drop_table(name)
