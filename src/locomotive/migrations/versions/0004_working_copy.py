"""working copy: dirty flag, capped diff against HEAD, untracked files

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("dirty", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("wc_diff", sa.Text(), nullable=True))
        batch.add_column(sa.Column("diff_truncated", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("untracked", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        for column in ("untracked", "diff_truncated", "wc_diff", "dirty"):
            batch.drop_column(column)
