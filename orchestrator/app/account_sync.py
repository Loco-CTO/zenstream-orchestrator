def sync_available(db):
    rows = db.read_execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_sync_epochs'"
    )
    return isinstance(rows, (list, tuple)) and bool(rows)


def epochs(db, user_id):
    rows = db.read_execute(
        "SELECT history_epoch,privacy_epoch FROM account_sync_epochs WHERE user_id=?",
        (user_id,),
    )
    return {
        "historyEpoch": rows[0][0] if rows else 0,
        "privacyEpoch": rows[0][1] if rows else 0,
    }


def clear_history_barrier(db, user_id):
    if sync_available(db):
        db.execute(
            "INSERT INTO account_sync_epochs(user_id,history_epoch) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET history_epoch=history_epoch+1,privacy_epoch=privacy_epoch+1",
            (user_id,),
        )


def field_revision(db, user_id, target_id, field):
    rows = db.read_execute(
        "SELECT revision,activity_ms,event_id FROM account_sync_fields WHERE user_id=? AND target_id=? AND field=?",
        (user_id, target_id, field),
    )
    return (
        {"revision": rows[0][0], "activityMs": rows[0][1], "eventId": rows[0][2]}
        if rows
        else {"revision": 0, "activityMs": 0, "eventId": ""}
    )
