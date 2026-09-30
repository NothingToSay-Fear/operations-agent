"""初始化完整应用库结构。"""

from alembic import op
from sqlalchemy import text

from app.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        for table in ("knowledge_segments", "history_units"):
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_terms "
                    f"ON {table} USING gin (to_tsvector('simple', search_terms))"
                )
            )
            bind.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_vector "
                    f"ON {table} USING hnsw (embedding vector_cosine_ops)"
                )
            )


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())
