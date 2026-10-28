import uuid

import sqlalchemy as sa
from alembic import op

revision = "0064_offline_identity"
down_revision = "0063_sonarr_calendar_api_version"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "offline_server",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.CheckConstraint("id=1", name="ck_offline_server_singleton"),
    )
    op.execute(
        sa.text(
            "INSERT INTO offline_server(id,instance_id) VALUES(1,:identity)"
        ).bindparams(identity=str(uuid.uuid4()))
    )


def downgrade():
    op.drop_table("offline_server")
