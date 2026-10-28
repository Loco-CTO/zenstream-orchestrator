from alembic import op

revision = "0066_follow_sync_updates"
down_revision = "0065_account_sync"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TRIGGER sync_follow_update AFTER UPDATE ON user_follow_targets "
        "WHEN NEW.entity_id IS NOT NULL BEGIN "
        "INSERT INTO account_sync_fields(user_id,target_id,field,revision,activity_ms,event_id) "
        "VALUES(NEW.user_id,NEW.entity_id,'following',1,CAST((julianday('now')-2440587.5)*86400000 AS INTEGER),'server') "
        "ON CONFLICT(user_id,target_id,field) DO UPDATE SET revision=revision+1,activity_ms=excluded.activity_ms,event_id='server'; END"
    )


def downgrade():
    op.execute("DROP TRIGGER sync_follow_update")
