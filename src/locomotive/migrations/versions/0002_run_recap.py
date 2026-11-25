"""run recap and cancel request

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("recap", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("recap_source", sa.String(16), nullable=True))
        batch.add_column(sa.Column("cancel_request", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("cancel_request")
        batch.drop_column("recap_source")
        batch.drop_column("recap")
