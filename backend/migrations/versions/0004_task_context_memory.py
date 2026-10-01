"""补齐任务上下文与分层记忆所需的表和字段。"""

import time

import sqlalchemy as sa
from alembic import op

from app.models import Base

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _column_names(bind, table):
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "task_memories" in tables:
        columns = _column_names(bind, "task_memories")
        if "recent_turns" not in columns:
            op.add_column(
                "task_memories",
                sa.Column("recent_turns", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
            )
        if "updated_at" not in columns:
            op.add_column(
                "task_memories",
                sa.Column("updated_at", sa.Float(), nullable=False, server_default=str(time.time())),
            )

    if "history_units" in tables and "message_ids" not in _column_names(bind, "history_units"):
        op.add_column(
            "history_units",
            sa.Column("message_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        )

    # create_all 只创建缺失表，不改写或删除已有业务数据。
    Base.metadata.create_all(bind=bind)


def downgrade():
    raise RuntimeError("此迁移包含任务上下文与记忆数据，不支持自动删除。")
