"""用管理员公共资料替代团队资料权限。"""

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _column_names(bind, table):
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "is_admin" not in _column_names(bind, "users"):
        op.add_column(
            "users",
            sa.Column("is_admin", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )

    document_scope_columns = _column_names(bind, "document_scopes")
    document_scope_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("document_scopes")
    }
    if "shared" not in document_scope_columns:
        op.add_column(
            "document_scopes",
            sa.Column("shared", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    if "team_id" in document_scope_columns:
        bind.execute(sa.text("UPDATE document_scopes SET shared = 1 WHERE team_id IS NOT NULL"))
        if bind.dialect.name == "postgresql":
            for constraint in sa.inspect(bind).get_foreign_keys("document_scopes"):
                if "team_id" in constraint.get("constrained_columns", []):
                    op.drop_constraint(constraint["name"], "document_scopes", type_="foreignkey")
            op.drop_column("document_scopes", "team_id")
        else:
            with op.batch_alter_table("document_scopes") as batch:
                if "ix_document_scopes_team_id" in document_scope_indexes:
                    batch.drop_index("ix_document_scopes_team_id")
                batch.drop_column("team_id")

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("document_scopes")}
    if "ix_document_scopes_shared" not in indexes:
        op.create_index("ix_document_scopes_shared", "document_scopes", ["shared"])

    if "memberships" in tables:
        op.drop_table("memberships")
    if "teams" in tables:
        op.drop_table("teams")


def downgrade():
    raise RuntimeError("该迁移移除了团队权限数据，不能自动恢复。")
