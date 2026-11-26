"""log retention: Keep, log truncated / pruned state on Runs, and housekeeping pass records

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("keep", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("log_truncated", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("log_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("log_pruned_at", sa.DateTime(), nullable=True))
    op.create_table(
        "housekeeping_passes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("pruned", sa.JSON(), nullable=False),
        sa.Column("compressed", sa.Integer(), nullable=False),
        sa.Column("freed_bytes", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("kept_bytes", sa.Integer(), nullable=False),
        sa.Column("budget_bytes", sa.Integer(), nullable=False),
        sa.Column("warning", sa.Text(), nullable=True),
        sa.Column("warning_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("housekeeping_passes")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("log_pruned_at")
        batch.drop_column("log_bytes")
        batch.drop_column("log_truncated")
        batch.drop_column("keep")
