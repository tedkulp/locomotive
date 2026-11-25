"""first failed task and notification delivery state on Runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("first_failed_task", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("notification_status", sa.String(16), nullable=True))
        batch.add_column(sa.Column("notification_attempts", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("notification_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("notification_error")
        batch.drop_column("notification_attempts")
        batch.drop_column("notification_status")
        batch.drop_column("first_failed_task")
