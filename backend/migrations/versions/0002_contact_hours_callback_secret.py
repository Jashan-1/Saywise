"""contact hours and per-agency callback secret version (D17, D18)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Same shape, new meaning: the hours a person may be texted (null = use the scope's).
    op.alter_column("memberships", "on_hours", new_column_name="contact_hours")
    op.add_column(
        "agencies",
        sa.Column("callback_secret_version", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("agencies", "callback_secret_version")
    op.alter_column("memberships", "contact_hours", new_column_name="on_hours")
