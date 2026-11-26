"""launch options: limit, resolved hosts, tags, diff, verbosity, extra vars

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("diff", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("verbosity", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("limit", sa.Text(), nullable=True))
        batch.add_column(sa.Column("hosts", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("extra_vars", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        for column in ("extra_vars", "tags", "hosts", "limit", "verbosity", "diff"):
            batch.drop_column(column)
