"""增量创建团队、资料版本、索引任务与记忆表，保留已有数据。"""

from alembic import op
from sqlalchemy import text

from app.models import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        for table in ("knowledge_segments", "history_units"):
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_terms ON {table} USING gin (to_tsvector('simple', search_terms))"
                )
            )
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_vector ON {table} USING hnsw (embedding vector_cosine_ops)"
                )
            )


def downgrade():
    raise RuntimeError("此迁移含用户资料和记忆，不自动删除；请先备份并显式迁移数据。")
