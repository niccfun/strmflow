"""Add persistent Emby media extraction queue.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_probes",
        sa.Column("target_path", sa.String(length=1000), nullable=False),
        sa.Column("item_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("target_path"),
    )
    op.create_index("ix_media_probes_item_id", "media_probes", ["item_id"])
    op.create_index("ix_media_probes_status", "media_probes", ["status"])


def downgrade() -> None:
    op.drop_index("ix_media_probes_status", table_name="media_probes")
    op.drop_index("ix_media_probes_item_id", table_name="media_probes")
    op.drop_table("media_probes")
