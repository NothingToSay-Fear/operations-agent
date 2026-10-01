"""删除已由知识片段版本表替代的旧 chunks 表。"""

from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # 旧表从未参与当前资料检索；当前版本化资料由 document_versions 和 knowledge_segments 保存。
    op.execute("DROP TABLE IF EXISTS chunks")


def downgrade():
    raise RuntimeError("旧 chunks 表已废弃，不支持自动恢复。")
