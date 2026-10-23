"""starter templates never decide on their own (D22)

Adds a new system-scope version of each template from the JSON files (which now hold no rules)
and retires the previous version, so databases that ran 0003 stop auto-approving.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24

"""

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.models import new_id

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates" / "decision_types"


LATEST = """
SELECT max(version) FROM policies WHERE scope_type = 'system' AND decision_type = :t
"""
RETIRE = """
UPDATE policies SET active = false
WHERE scope_type = 'system' AND decision_type = :t AND active
"""
INSERT = """
INSERT INTO policies (id, scope_type, scope_id, decision_type, version, body, source)
VALUES (:id, 'system', 'system', :t, :v, :body, 'template')
"""


def upgrade() -> None:
    conn = op.get_bind()
    insert = sa.text(INSERT).bindparams(sa.bindparam("body", type_=JSONB))
    for path in sorted(TEMPLATES.glob("*.json")):
        latest = conn.scalar(sa.text(LATEST), {"t": path.stem})
        conn.execute(sa.text(RETIRE), {"t": path.stem})
        body = json.loads(path.read_text())
        conn.execute(
            insert, {"id": new_id("pol"), "t": path.stem, "v": (latest or 0) + 1, "body": body}
        )


def downgrade() -> None:
    # Drop the newest system version of each type and reactivate the one before it.
    op.execute(
        "DELETE FROM policies p WHERE scope_type = 'system' AND version = ("
        " SELECT max(version) FROM policies q"
        " WHERE q.scope_type = 'system' AND q.decision_type = p.decision_type) AND version > 1"
    )
    op.execute(
        "UPDATE policies p SET active = true WHERE scope_type = 'system' AND version = ("
        " SELECT max(version) FROM policies q"
        " WHERE q.scope_type = 'system' AND q.decision_type = p.decision_type)"
    )
