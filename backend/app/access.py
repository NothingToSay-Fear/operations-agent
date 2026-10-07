"""知识库、证据与记忆的可见范围校验。"""

import time

from sqlalchemy import and_, or_, select, update

from app.extension_models import DocumentScope, SourceSetting, UserMemory, UserScope
from app.models import Document, User


def document_filter(user_id, *, enabled=True):
    # 资料可见性始终由所有者、共享范围和启用状态共同决定。
    private = and_(
        or_(DocumentScope.document_id.is_(None), DocumentScope.shared == 0),
        Document.user_id == user_id,
    )
    shared = DocumentScope.shared == 1
    visible = and_(
        or_(DocumentScope.document_id.is_(None), DocumentScope.deleted == 0),
        or_(private, shared),
    )
    if not enabled:
        return visible
    setting = (
        select(SourceSetting.enabled)
        .where(SourceSetting.user_id == user_id, SourceSetting.document_id == Document.id)
        .scalar_subquery()
    )
    selected = or_(setting == 1, and_(setting.is_(None), private, Document.enabled == 1))
    return and_(visible, selected)


def document_query(user_id, *, enabled=True):
    return (
        select(Document, DocumentScope)
        .outerjoin(DocumentScope)
        .where(document_filter(user_id, enabled=enabled))
    )


async def get_document(session, user_id, document_id, *, enabled=True):
    row = (
        await session.execute(document_query(user_id, enabled=enabled).where(Document.id == document_id))
    ).first()
    if not row:
        raise ValueError("资料不存在或不在当前可访问范围")
    return row


async def scope_revision(session, user_id, *, lock=False):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    await session.execute(insert(UserScope).values(user_id=user_id, revision=0).on_conflict_do_nothing())
    statement = (
        select(UserScope).where(UserScope.user_id == user_id).execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.scalar(statement)).revision


async def bump_scope(session, user_ids):
    # 范围变更递增修订号，使运行中的任务能够发现资料权限已经变化。
    for user_id in sorted(set(user_ids)):
        await scope_revision(session, user_id, lock=True)
        await session.execute(
            update(UserScope).where(UserScope.user_id == user_id).values(revision=UserScope.revision + 1)
        )


async def document_users(session, doc, scope):
    if scope and scope.shared:
        return list(await session.scalars(select(User.id)))
    return [doc.user_id]


async def references_allowed(session, user_id, data):
    # 上下文版本变化只要求后续任务重建上下文，不能据此隐藏既有业务证据。
    """复核证据中的资料版本，禁止已失效来源经证据回读重新进入上下文。"""
    if not isinstance(data, dict):
        return True
    if _contains_hidden_reference(data):
        return False
    for ref in data.get("source_refs", []):
        try:
            _, scope = await get_document(session, user_id, ref["document_id"])
        except ValueError:
            return False
        if ref.get("version_id") and (not scope or scope.active_version_id != ref["version_id"]):
            return False
    for key in ("data", "result"):
        if isinstance(data.get(key), dict) and not await references_allowed(session, user_id, data[key]):
            return False
    return True


def _contains_hidden_reference(value):
    """识别旧任务中曾写入证据、但已不属于知识库的内置参考资料。"""
    if isinstance(value, dict):
        if value.get("source") == "simulated_reference":
            return True
        return any(_contains_hidden_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_hidden_reference(item) for item in value)
    return False


async def memories_valid(session, user_id, stamps):
    for stamp in stamps:
        row = await session.scalar(
            select(UserMemory).where(UserMemory.id == stamp["id"], UserMemory.user_id == user_id)
        )
        if (
            not row
            or row.status != "active"
            or row.version != stamp["version"]
            or (row.expires_at and row.expires_at <= time.time())
        ):
            return False
    return True
