"""schedules, controller settings, and the Schedule a Run came from

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("playbook", sa.String(255), nullable=False),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("check", sa.Boolean(), nullable=False),
        sa.Column("diff", sa.Boolean(), nullable=False),
        sa.Column("verbosity", sa.Integer(), nullable=False),
        sa.Column("limit", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("extra_vars", sa.JSON(), nullable=False),
        sa.Column("cron", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("notify_on_changes", sa.Boolean(), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "controller_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
    )
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("schedule_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("schedule_name", sa.String(255), nullable=True))
        batch.create_index("ix_runs_schedule_id", ["schedule_id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_schedule_id")
        batch.drop_column("schedule_name")
        batch.drop_column("schedule_id")
    op.drop_table("controller_settings")
    op.drop_table("schedules")
