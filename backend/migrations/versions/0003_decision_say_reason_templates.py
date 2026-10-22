"""decision say and reason (D20), starter decision-type templates at system scope

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.models import REASONS, new_id

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates" / "decision_types"


def upgrade() -> None:
    op.add_column("decisions", sa.Column("say", sa.Text(), nullable=True))
    op.add_column("decisions", sa.Column("reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_decisions_reason"),
        "decisions",
        f"reason IN ({', '.join(repr(r) for r in REASONS)})",
    )
    # shortcut: seeds the JSON files as they are when this runs; ship a later template change as
    # a new migration that adds a system-scope version, so existing databases get it too.
    policies = sa.table(
        "policies",
        sa.column("id", sa.Text),
        sa.column("scope_type", sa.Text),
        sa.column("scope_id", sa.Text),
        sa.column("decision_type", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("body", JSONB),
        sa.column("source", sa.Text),
    )
    op.bulk_insert(
        policies,
        [
            {
                "id": new_id("pol"),
                "scope_type": "system",
                "scope_id": "system",
                "decision_type": path.stem,
                "version": 1,
                "body": json.loads(path.read_text()),
                "source": "template",
            }
            for path in sorted(TEMPLATES.glob("*.json"))
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM policies WHERE scope_type = 'system'")
    op.drop_constraint(op.f("ck_decisions_reason"), "decisions", type_="check")
    op.drop_column("decisions", "reason")
    op.drop_column("decisions", "say")
