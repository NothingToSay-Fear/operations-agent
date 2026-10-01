"""管理员公共资料、个人资料及确认式记忆的用户接口。"""

import time
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import Field
from sqlalchemy import delete, select, update

from app import access, knowledge, memory, retrieval
from app.analytics import StrictModel
from app.auth import current_user
from app.extension_models import (
    BackgroundJob,
    DocumentScope,
    DocumentVersion,
    KnowledgeSegment,
    MemoryCandidate,
    MemoryEvent,
    ModelContextSnapshot,
    SourceSetting,
    UserMemory,
)
from app.models import Document, Task


class SourceEdit(StrictModel):
    enabled: bool


class CandidateCreate(StrictModel):
    content: str = Field(min_length=2, max_length=600)
    kind: Literal[
        "work_profile", "analysis_preference", "answer_preference", "focus_direction", "stable_constraint"
    ] = "answer_preference"
    expires_at: float | None = None
    replaces_id: str | None = None
    replaces_version: int | None = None


class CandidateConfirm(StrictModel):
    version: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=2, max_length=600)
    kind: str | None = None
    expires_at: float | None = None


class MemoryControl(StrictModel):
    version: int = Field(ge=1)
    action: Literal["disable", "delete"]


def serialize(row, fields):
    return {key: getattr(row, key) for key in fields.split()}


def router(database, settings):
    api = APIRouter(prefix="/api")

    async def document(session, doc_id, user, manage=False):
        try:
            doc, scope = await access.get_document(session, user.id, doc_id, enabled=False)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        if manage:
            if scope and scope.shared and not user.is_admin:
                raise HTTPException(403, "只有管理员可以管理公共资料")
            if doc.user_id != user.id and (not user.is_admin or not scope or not scope.shared):
                raise HTTPException(403, "不能管理其他用户的资料")
        return doc, scope

    async def task_for(session, task_id, user_id):
        task = await session.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))
        if not task:
            raise HTTPException(404, "任务不存在")
        return task

    @api.get("/documents")
    async def documents(user=Depends(current_user)):
        async with database.sessions() as session:
            rows = (
                await session.execute(
                    access.document_query(user.id, enabled=False).order_by(Document.created_at.desc())
                )
            ).all()
            result = []
            for doc, scope in rows:
                selected = await session.scalar(
                    select(SourceSetting).where(
                        SourceSetting.user_id == user.id, SourceSetting.document_id == doc.id
                    )
                )
                latest = await session.scalar(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == doc.id)
                    .order_by(DocumentVersion.number.desc())
                    .limit(1)
                )
                job = (
                    await session.scalar(
                        select(BackgroundJob).where(
                            BackgroundJob.target_id == latest.id, BackgroundJob.kind == "index"
                        )
                    )
                    if latest
                    else None
                )
                result.append(
                    {
                        "id": doc.id,
                        "title": doc.title,
                        "enabled": bool(
                            selected.enabled
                            if selected
                            else (doc.enabled if not scope or not scope.shared else 0)
                        ),
                        "shared": bool(scope.shared) if scope else False,
                        "owner_id": doc.user_id,
                        "characters": len(doc.content),
                        "created_at": doc.created_at,
                        "active_version_id": scope.active_version_id if scope else None,
                        "version": doc.version,
                        "status": job.status if job else "legacy",
                        "progress": job.progress if job else 0,
                        "error": job.error if job else "",
                        "mode": latest.mode if latest else "旧索引",
                        "latest_version_id": latest.id if latest else None,
                    }
                )
            return result

    async def read_upload(file):
        data = await file.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, "资料最大 8MB")
        if not data or (file.filename or "").rsplit(".", 1)[-1].lower() not in {
            "md",
            "txt",
            "csv",
            "pdf",
            "docx",
        }:
            raise HTTPException(422, "请选择支持格式的非空文件")
        return data

    @api.post("/documents", status_code=201)
    async def upload(
        file: UploadFile = File(...), shared: bool | None = Form(default=None), user=Depends(current_user)
    ):
        data = await read_upload(file)
        async with database.sessions() as session, session.begin():
            if shared and not user.is_admin:
                raise HTTPException(403, "只有管理员可以上传公共资料")
            doc = Document(user_id=user.id, title=(file.filename or "资料")[:200], content="")
            session.add(doc)
            await session.flush()
            session.add(DocumentScope(document_id=doc.id, shared=int(bool(user.is_admin))))
            version = await knowledge.enqueue_version(session, doc, doc.title, data)
            return {"id": doc.id, "title": doc.title, "version_id": version.id, "status": "queued"}

    @api.post("/documents/{doc_id}/versions", status_code=201)
    async def new_version(doc_id: str, file: UploadFile = File(...), user=Depends(current_user)):
        data = await read_upload(file)
        async with database.sessions() as session, session.begin():
            doc, _ = await document(session, doc_id, user, manage=True)
            version = await knowledge.enqueue_version(session, doc, file.filename, data)
            return {"id": version.id, "number": version.number, "status": "queued"}

    @api.get("/documents/{doc_id}/versions")
    async def versions(doc_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            await document(session, doc_id, user)
            return [
                serialize(
                    v,
                    "id number filename content_hash status mode model_id dimension parser_version created_at",
                )
                for v in await session.scalars(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == doc_id)
                    .order_by(DocumentVersion.number.desc())
                )
            ]

    @api.get("/documents/{doc_id}/content")
    async def content(
        doc_id: str,
        version_id: str | None = None,
        segment_id: str | None = None,
        position: int = 0,
        user=Depends(current_user),
    ):
        if position < 0 or position > 300000:
            raise HTTPException(422, "原文位置无效")
        async with database.sessions() as session, database.read_sessions() as commerce:
            try:
                return await knowledge.read(
                    session, commerce, user.id, doc_id, position, version_id, segment_id
                )
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None

    @api.post("/documents/{doc_id}/retry")
    async def retry(doc_id: str, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            doc, _ = await document(session, doc_id, user, manage=True)
            latest = await session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc_id)
                .order_by(DocumentVersion.number.desc())
                .limit(1)
            )
            if not latest:
                raise HTTPException(409, "没有可重建版本")
            version = await knowledge.enqueue_version(session, doc, latest.filename, latest.raw)
            return {"version_id": version.id, "status": "queued"}

    @api.patch("/documents/{doc_id}")
    async def source_setting(doc_id: str, body: SourceEdit, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            doc, _ = await document(session, doc_id, user)
            await access.bump_scope(session, [user.id])
            row = await session.scalar(
                select(SourceSetting).where(
                    SourceSetting.user_id == user.id, SourceSetting.document_id == doc_id
                )
            )
            if row:
                row.enabled = int(body.enabled)
            else:
                session.add(SourceSetting(user_id=user.id, document_id=doc_id, enabled=int(body.enabled)))
            return {"ok": True}

    @api.delete("/documents/{doc_id}")
    async def remove_document(doc_id: str, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            doc, scope = await document(session, doc_id, user, manage=True)
            await access.bump_scope(session, await access.document_users(session, doc, scope))
            if not scope:
                scope = DocumentScope(document_id=doc.id)
                session.add(scope)
            scope.deleted, scope.active_version_id = 1, None
            versions = list(
                await session.scalars(select(DocumentVersion.id).where(DocumentVersion.document_id == doc_id))
            )
            await session.execute(
                update(BackgroundJob)
                .where(BackgroundJob.target_id.in_(versions))
                .values(status="cancelled", lease_token=None)
            )
            await session.execute(delete(KnowledgeSegment).where(KnowledgeSegment.version_id.in_(versions)))
            await session.execute(delete(DocumentVersion).where(DocumentVersion.document_id == doc_id))
            doc.content = ""
            return {"ok": True}

    @api.get("/retrieval/health")
    async def retrieval_health(user=Depends(current_user)):
        return await retrieval.health(settings)

    @api.get("/memories")
    async def memories(user=Depends(current_user)):
        async with database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(UserMemory)
                    .where(UserMemory.user_id == user.id)
                    .order_by(UserMemory.created_at.desc())
                )
            )
            return [
                {
                    **serialize(r, "id kind content version expires_at created_at"),
                    "status": "expired" if r.expires_at and r.expires_at <= time.time() else r.status,
                }
                for r in rows
            ]

    @api.get("/memory-candidates")
    async def candidates(user=Depends(current_user)):
        async with database.sessions() as session:
            rows = await session.scalars(
                select(MemoryCandidate)
                .where(MemoryCandidate.user_id == user.id, MemoryCandidate.status == "pending")
                .order_by(MemoryCandidate.created_at.desc())
            )
            return [
                serialize(
                    r,
                    "id task_id kind content source source_seq version replaces_id replaces_version expires_at created_at",
                )
                for r in rows
            ]

    @api.post("/memory-candidates", status_code=201)
    async def create_candidate(body: CandidateCreate, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            try:
                row = await memory.propose(session, user.id, **body.model_dump())
                return {"id": row.id, "status": "pending", "version": row.version}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @api.post("/memory-candidates/{candidate_id}/confirm")
    async def confirm_candidate(candidate_id: str, body: CandidateConfirm, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            try:
                row = await memory.confirm(
                    session, user.id, candidate_id, **body.model_dump(exclude_unset=True)
                )
                return {"id": row.id, "status": row.status}
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @api.post("/memory-candidates/{candidate_id}/dismiss")
    async def dismiss(candidate_id: str, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            row = await session.scalar(
                select(MemoryCandidate)
                .where(
                    MemoryCandidate.id == candidate_id,
                    MemoryCandidate.user_id == user.id,
                    MemoryCandidate.status == "pending",
                )
                .with_for_update()
            )
            if not row:
                raise HTTPException(404, "候选不存在或已处理")
            row.status, row.version = "dismissed", row.version + 1
            return {"ok": True}

    @api.post("/memories/{memory_id}/control")
    async def memory_control(memory_id: str, body: MemoryControl, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            await access.bump_scope(session, [user.id])
            row = await session.scalar(
                select(UserMemory)
                .where(UserMemory.id == memory_id, UserMemory.user_id == user.id)
                .with_for_update()
            )
            if not row or row.version != body.version:
                raise HTTPException(409, "记忆不存在或版本已变化")
            session.add(MemoryEvent(user_id=user.id, memory_id=row.id, action=body.action))
            if body.action == "delete":
                await session.execute(
                    delete(MemoryCandidate).where(
                        MemoryCandidate.user_id == user.id,
                        (MemoryCandidate.id == row.source_candidate_id)
                        | (MemoryCandidate.replaces_id == row.id),
                    )
                )
                await session.delete(row)
            else:
                row.status, row.version = "disabled", row.version + 1
            return {"ok": True}

    @api.get("/memories/events")
    async def memory_events(user=Depends(current_user)):
        async with database.sessions() as session:
            return [
                serialize(r, "memory_id task_id action detail created_at")
                for r in await session.scalars(
                    select(MemoryEvent)
                    .where(MemoryEvent.user_id == user.id)
                    .order_by(MemoryEvent.created_at.desc())
                    .limit(100)
                )
            ]

    @api.get("/tasks/{task_id}/memory")
    async def task_memory(task_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            task = await task_for(session, task_id, user.id)
            built = await memory.build_context(session, task, settings)
            jobs = list(
                await session.scalars(
                    select(BackgroundJob)
                    .where(
                        BackgroundJob.task_id == task_id, BackgroundJob.kind.in_(["summary", "candidates"])
                    )
                    .order_by(BackgroundJob.created_at.desc())
                    .limit(20)
                )
            )
            snapshots = list(
                await session.scalars(
                    select(ModelContextSnapshot)
                    .where(ModelContextSnapshot.task_id == task_id)
                    .order_by(ModelContextSnapshot.call_number.desc())
                    .limit(20)
                )
            )
            return {
                "summary": built["task_memory"],
                "summary_version": built["memory_summary_version"],
                "memory_state_revision": built["memory_state_revision"],
                "effective_constraints": built["effective_constraints"],
                "constraint_state_version": built["constraint_state_version"],
                "recent_turns": built["recent_turns"],
                "selected_memories": built["long_term_memories"],
                "used_memories": task.state.get("used_memories", []),
                "jobs": [serialize(j, "id kind status error usage") for j in jobs],
                "context_snapshots": [
                    serialize(row, "id call_number phase manifest created_at") for row in snapshots
                ],
                "history_scope": "current_task_only",
            }

    @api.get("/tasks/{task_id}/history/{unit_id}")
    async def read_history(task_id: str, unit_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            task = await task_for(session, task_id, user.id)
            try:
                return await memory.history_search(session, task, "", settings, unit_id=unit_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None

    return api
