"""Create media, transfer job and metadata tables.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_metadata",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "media_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.String(length=600), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("generated_path", sa.String(length=600), nullable=False),
        sa.Column("target_dir", sa.String(length=600), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("year", sa.String(length=4), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("media_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_episodes", sa.Integer(), nullable=True),
        sa.Column("update_schedule", sa.String(length=100), nullable=False),
        sa.Column("baidu_link", sa.String(length=1000), nullable=False),
        sa.Column("synced_files", sa.JSON(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_path"),
    )
    op.create_index("ix_media_items_source_path", "media_items", ["source_path"])
    op.create_table(
        "transfer_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("destination", sa.String(length=1000), nullable=False),
        sa.Column("command", sa.JSON(), nullable=False),
        sa.Column("job_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("return_code", sa.Integer(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stderr", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transfer_jobs_created_at", "transfer_jobs", ["created_at"])
    op.create_index("ix_transfer_jobs_provider", "transfer_jobs", ["provider"])
    op.create_index("ix_transfer_jobs_status", "transfer_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_transfer_jobs_status", table_name="transfer_jobs")
    op.drop_index("ix_transfer_jobs_provider", table_name="transfer_jobs")
    op.drop_index("ix_transfer_jobs_created_at", table_name="transfer_jobs")
    op.drop_table("transfer_jobs")
    op.drop_index("ix_media_items_source_path", table_name="media_items")
    op.drop_table("media_items")
    op.drop_table("app_metadata")
