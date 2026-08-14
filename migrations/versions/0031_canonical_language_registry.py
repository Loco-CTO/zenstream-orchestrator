import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

try:
    from app.language_registry import normalize_metadata_locale
    from app.language_registry import normalize_track_language
except ModuleNotFoundError:  # Alembic may run from the workspace root.
    from orchestrator.app.language_registry import normalize_metadata_locale
    from orchestrator.app.language_registry import normalize_track_language


revision = "0031_canonical_language_registry"
down_revision = "0030_invite_reimplementation"
branch_labels = None
depends_on = None


def _normalize_list(value: object) -> list[str]:
    try:
        values = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    result: list[str] = []
    for item in values if isinstance(values, list) else []:
        try:
            locale = normalize_metadata_locale(item)
        except (TypeError, ValueError):
            continue
        if locale not in result:
            result.append(locale)
    return result or ["en"]


def upgrade() -> None:
    bind = op.get_bind()
    metadata_rows = bind.execute(
        sa.text("SELECT value FROM metadata_settings WHERE key='locales'")
    ).fetchall()
    if metadata_rows:
        bind.execute(
            sa.text(
                "UPDATE metadata_settings SET value=:value,updated_at=:updated_at "
                "WHERE key='locales'"
            ),
            {
                "value": json.dumps(_normalize_list(metadata_rows[0][0])),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "account_preferences" in tables:
        rows = bind.execute(
            sa.text("SELECT user_id,metadata_language FROM account_preferences")
        ).fetchall()
        for user_id, language in rows:
            try:
                normalized = normalize_metadata_locale(language) if language else None
            except (TypeError, ValueError):
                normalized = None
            bind.execute(
                sa.text(
                    "UPDATE account_preferences SET metadata_language=:language "
                    "WHERE user_id=:user_id"
                ),
                {"language": normalized, "user_id": user_id},
            )

    if "media_files" in tables:
        rows = bind.execute(
            sa.text(
                "SELECT id,language FROM media_files "
                "WHERE role IN ('subtitle','lyrics') AND language IS NOT NULL"
            )
        ).fetchall()
        for file_id, language in rows:
            bind.execute(
                sa.text("UPDATE media_files SET language=:language WHERE id=:id"),
                {"language": normalize_track_language(language), "id": file_id},
            )


def downgrade() -> None:
    pass
