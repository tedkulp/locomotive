"""runs

Revision ID: 0001
Revises:
Create Date: 2026-11-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("playbook", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("checkout", sa.String(32), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("sha", sa.String(64), nullable=True),
        sa.Column("check", sa.Boolean(), nullable=False),
        sa.Column("requirements_hash", sa.String(64), nullable=True),
        sa.Column("ansible_core_version", sa.String(64), nullable=True),
        sa.Column("rc", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_runs_status", "runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_runs_status", "runs")
    op.drop_table("runs")
