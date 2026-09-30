import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import copy
import json
import secrets
import time

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app import access, analytics, background, memory, task_context
from app.agent.runtime import TERMINAL, initial_state, worker
from app.analytics import Query, StrictModel
from app.auth import current_user, hash_password, token_hash, verify_password
from app.config import get_settings
from app.db import Database
from app.extension_models import DocumentScope, SourceSetting, TaskMessage
from app.models import Artifact, Base, Document, Evidence, Run, Session, Task, TaskEvent, User, uid
from app.task_cleanup import delete_task_contents


class Credentials(StrictModel):
    username: str = Field(min_length=3, max_length=60, pattern=r"^[\w@.\-]+$")
    password: str = Field(min_length=8, max_length=128)


class NewTask(StrictModel):
    goal: str = Field(min_length=2, max_length=6000)


class Control(StrictModel):
    action: str = Field(pattern="^(pause|resume|cancel|message)$")
    message: str = Field(default="", max_length=6000)


class DocumentSetting(StrictModel):
    enabled: bool


class ArtifactEdit(StrictModel):
    content: str = Field(min_length=1, max_length=60000)


def task_view(task, detail=True):
    result = dict(
        id=task.id,
        goal=task.goal,
        status=task.status,
        revision=task.revision,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
    if detail:
        result["state"] = task.state
    return result


async def own_task(session, task_id, user_id):
    task = await session.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


async def conversation_view(session, task):
    """从持久化事件恢复按时间排列的用户问题和 Agent 回复。"""
    revision = await access.scope_revision(session, task.user_id)
    rows = list(
        await session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.seq))
    )
    turns = [
        {
            "id": "goal",
            "seq": 0,
            "role": "user",
            "kind": "goal",
            "content": task.goal,
            "created_at": task.created_at,
        }
    ]
    assistant_sequences = set()
    for row in rows:
        payload = row.payload or {}
        if row.kind == "user_control" and payload.get("action") == "message":
            content = str(payload.get("message", "")).strip()
            if content:
                turns.append(
                    {
                        "id": row.id,
                        "seq": row.seq,
                        "role": "user",
                        "kind": "message",
                        "content": content,
                        "created_at": row.created_at,
                    }
                )
            continue

        content = ""
        turn_kind = "answer"
        if row.kind == "evaluation" and payload.get("kind") in {"finish", "stop", "ask_user"}:
            content = str(payload.get("answer") or payload.get("reason") or "").strip()
            turn_kind = "question" if payload.get("kind") == "ask_user" else "answer"
        elif row.kind == "action" and payload.get("kind") == "ask_user":
            content = str(payload.get("summary", "")).strip()
            turn_kind = "question"
        elif row.kind in {"error", "blocked", "budget_exhausted", "model_error"}:
            content = str(payload.get("message", "")).strip()
            turn_kind = "notice"
        elif row.kind == "decision_invalid" and task.status == "failed" and row.seq == task.state["seq"]:
            content = str(task.state.get("answer", "")).strip()
            turn_kind = "notice"
        if not content:
            continue
        if payload.get("scope_revision", revision) != revision:
            content = "原资料或记忆范围已变化，此历史回复已隐藏，请重新提问以核验。"
            turn_kind = "notice"
        turns.append(
            {
                "id": row.id,
                "seq": row.seq,
                "role": "assistant",
                "kind": turn_kind,
                "content": content,
                "created_at": row.created_at,
            }
        )
        assistant_sequences.add(row.seq)

    current_answer = str(task.state.get("answer", "")).strip()
    if current_answer and task.state.get("seq") not in assistant_sequences:
        turns.append(
            {
                "id": f"current-{task.revision}",
                "seq": task.state.get("seq", 0),
                "role": "assistant",
                "kind": "answer" if task.status == "completed" else "notice",
                "content": current_answer,
                "created_at": task.updated_at,
            }
        )
    persisted = list(
        await session.scalars(
            select(TaskMessage).where(TaskMessage.task_id == task.id).order_by(TaskMessage.seq)
        )
    )
    merged = {(row["seq"], row["role"]): row for row in turns}
    for row in persisted:
        content, kind = row.content, row.kind
        if row.role == "assistant" and row.scope_revision != revision:
            content = "原资料或记忆范围已变化，此历史回复已隐藏，请重新提问以核验。"
            kind = "notice"
        merged[(row.seq, row.role)] = {
            "id": row.id,
            "seq": row.seq,
            "role": row.role,
            "kind": kind,
            "content": content,
            "created_at": row.created_at,
        }
    return sorted(merged.values(), key=lambda row: (row["seq"], 0 if row["role"] == "user" else 1))


async def safe_task_view(session, task):
    result = task_view(task)
    result["state"] = copy.deepcopy(task.state)
    invalid = False
    for observation in result["state"]["observations"]:
        if not await access.references_allowed(session, task.user_id, observation.get("data", {})):
            observation["data"] = {"message": "资料来源已失效，内容已隐藏"}
            observation["arguments"] = {}
            invalid = True
    revision = await access.scope_revision(session, task.user_id)
    if invalid or task.state.get("context_scope_revision", revision) != revision:
        result["state"].update(
            plan=None,
            plans=[],
            steps={},
            answer="资料或记忆范围已变化，旧结论已隐藏，请继续任务重新核验。",
            feedback="",
        )
    return result


async def artifact_allowed(session, task, artifact):
    for eid in artifact.evidence_ids:
        evidence = await session.scalar(
            select(Evidence).where(Evidence.id == eid, Evidence.task_id == task.id)
        )
        if evidence and not await access.references_allowed(session, task.user_id, evidence.result):
            return False
    return True


async def ensure_admin(database, settings):
    """确保管理员账号和其资料的公共范围与配置保持一致。"""
    async with database.sessions() as session:
        user = await session.scalar(select(User).where(User.username == settings.admin_username))
        password_matches = bool(
            user
            and await asyncio.to_thread(
                verify_password,
                settings.admin_password,
                user.password_hash,
            )
        )
        if not user or not user.is_admin or not password_matches:
            hashed_password = await asyncio.to_thread(hash_password, settings.admin_password)
            if user:
                user.password_hash = hashed_password
                user.is_admin = 1
                await session.execute(delete(Session).where(Session.user_id == user.id))
            else:
                user = User(
                    username=settings.admin_username,
                    password_hash=hashed_password,
                    is_admin=1,
                )
                session.add(user)
                await session.flush()

        rows = (
            await session.execute(
                select(Document, DocumentScope)
                .outerjoin(DocumentScope, DocumentScope.document_id == Document.id)
                .where(Document.user_id == user.id)
            )
        ).all()
        changed_ids = []
        for document, scope in rows:
            if scope is None:
                session.add(DocumentScope(document_id=document.id, shared=1))
                changed_ids.append(document.id)
            elif not scope.shared:
                scope.shared = 1
                scope.revision += 1
                changed_ids.append(document.id)
        if changed_ids:
            await session.execute(delete(SourceSetting).where(SourceSetting.document_id.in_(changed_ids)))
            await access.bump_scope(session, list(await session.scalars(select(User.id))))
        await session.commit()


def create_app(settings=None, *, start_worker=True, model=None):
    settings = settings or get_settings()
    database = Database(settings)
    stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        # PostgreSQL 由 Alembic 迁移，SQLite 用于本地开发与隔离测试。
        if settings.app_database_url.startswith("sqlite"):
            async with database.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        await ensure_admin(database, settings)
        task = asyncio.create_task(worker(database, settings, stop, model)) if start_worker else None
        maintenance = (
            asyncio.create_task(background.worker(database, settings, stop))
            if start_worker and settings.background_in_api
            else None
        )
        yield
        stop.set()
        if maintenance:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()

    app = FastAPI(title="Operations Agent", version="0.1.0", lifespan=lifespan)
    app.state.db = database
    app.state.settings = settings
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )
    attempts = defaultdict(deque)

    @app.middleware("http")
    async def origin_check(request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "PATCH", "DELETE"} and origin and origin not in origins:
            return JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def limit_auth(request):
        key = request.client.host if request.client else "local"
        now = time.time()
        queue = attempts[key]
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= 15:
            raise HTTPException(429, "请求过于频繁，请稍后再试")
        queue.append(now)

    async def sign_in(session, user, response):
        raw = secrets.token_urlsafe(32)
        session.add(
            Session(
                token_hash=token_hash(raw),
                user_id=user.id,
                expires_at=time.time() + settings.session_days * 86400,
            )
        )
        await session.commit()
        response.set_cookie(
            "ops_session",
            raw,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_days * 86400,
            path="/",
        )
        return {"id": user.id, "username": user.username, "is_admin": bool(user.is_admin)}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/auth/register")
    async def register(body: Credentials, request: Request, response: Response):
        limit_auth(request)
        async with database.sessions() as session:
            user = User(
                username=body.username, password_hash=await asyncio.to_thread(hash_password, body.password)
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                raise HTTPException(409, "用户名不可用") from None
            return await sign_in(session, user, response)

    @app.post("/api/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        limit_auth(request)
        async with database.sessions() as session:
            user = await session.scalar(select(User).where(User.username == body.username))
            if not user or not await asyncio.to_thread(verify_password, body.password, user.password_hash):
                raise HTTPException(401, "用户名或密码不正确")
            return await sign_in(session, user, response)

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        async with database.sessions() as session, session.begin():
            await session.execute(
                delete(Session).where(
                    Session.token_hash == token_hash(request.cookies.get("ops_session", ""))
                )
            )
        response.delete_cookie("ops_session", path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(user=Depends(current_user)):
        return {"id": user.id, "username": user.username, "is_admin": bool(user.is_admin)}

    @app.get("/api/config")
    async def config(user=Depends(current_user)):
        return {
            "model_ready": settings.llm_enabled,
            "model": settings.llm_model or None,
            "provider": settings.llm_provider,
            "simulated": True,
            "business_access": "read_only",
        }

    @app.get("/api/overview")
    async def overview(user=Depends(current_user)):
        try:
            async with database.read_sessions() as session:
                as_of, meta = await analytics.cutoff(session)
                from datetime import timedelta

                current = await analytics.query_metrics(
                    session, Query(start_date=as_of - timedelta(days=6), end_date=as_of)
                )
                previous = await analytics.query_metrics(
                    session, Query(start_date=as_of - timedelta(days=13), end_date=as_of - timedelta(days=7))
                )
                trend = await analytics.query_metrics(
                    session, Query(start_date=as_of - timedelta(days=13), end_date=as_of, group_by="day")
                )
            return {"dataset": meta, "current": current, "previous": previous, "trend": trend}
        except Exception:
            raise HTTPException(503, "经营数据尚不可用，请先运行模拟数据初始化并检查只读连接。") from None

    @app.post("/api/tasks", status_code=201)
    async def new_task(body: NewTask, user=Depends(current_user)):
        async with database.sessions() as session:
            task = Task(
                user_id=user.id, goal=body.goal.strip(), state=initial_state(body.goal.strip(), settings)
            )
            session.add(task)
            await session.flush()
            message, _ = await task_context.record_user_message(
                session, task, 0, task.goal, settings, kind="goal"
            )
            await memory.append_history(
                session,
                task,
                0,
                "user",
                {"content": task.goal, "message_ids": [message.id]},
                settings,
            )
            notice = await memory.explicit_forget(session, user.id, task.goal)
            if notice:
                task.state = {**task.state, "answer": notice}
                task.status = "completed"
            await session.commit()
            return task_view(task)

    @app.get("/api/tasks")
    async def tasks(user=Depends(current_user)):
        async with database.sessions() as session:
            items = (
                await session.scalars(
                    select(Task).where(Task.user_id == user.id).order_by(Task.updated_at.desc()).limit(100)
                )
            ).all()
            return [task_view(t, False) for t in items]

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            return await safe_task_view(session, await own_task(session, task_id, user.id))

    @app.get("/api/tasks/{task_id}/conversation")
    async def conversation(task_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            return await conversation_view(session, await own_task(session, task_id, user.id))

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str, user=Depends(current_user)):
        async with database.sessions() as session, session.begin():
            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.user_id == user.id).with_for_update()
            )
            if task is None:
                raise HTTPException(404, "任务不存在")
            await delete_task_contents(session, task)
        return {"ok": True, "id": task_id}

    @app.post("/api/tasks/{task_id}/control")
    async def control(task_id: str, body: Control, user=Depends(current_user)):
        async with database.sessions() as session:
            task = await own_task(session, task_id, user.id)
            scope_revision = await access.scope_revision(session, user.id, lock=True)
            task = await session.scalar(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            state = copy.deepcopy(task.state)
            if body.action == "message" and not body.message.strip():
                raise HTTPException(422, "请输入补充条件或新目标")
            if task.status == "cancelled":
                raise HTTPException(409, "任务已取消，请新建任务")
            notice = (
                await memory.explicit_forget(session, user.id, body.message)
                if body.action == "message"
                else None
            )
            if notice:
                state["messages"].append({"role": "user", "content": body.message.strip()})
                state["messages"] = state["messages"][-settings.memory_recent_turn_limit :]
                state["answer"] = notice
                status = task.status
            elif body.action == "pause":
                if task.status not in {"queued", "running"}:
                    raise HTTPException(409, "当前任务没有正在执行")
                status = "paused"
            elif body.action == "cancel":
                status = "cancelled"
            elif body.action == "resume":
                if task.status in {"running", "queued", "completed"}:
                    raise HTTPException(409, "当前状态无需继续")
                if task.status == "waiting_user":
                    raise HTTPException(409, "请先回复待补充的问题")
                status = "queued"
                state["invalid_outputs"] = 0
            else:
                status = "queued"
                state["messages"].append({"role": "user", "content": body.message.strip()})
                state["messages"] = state["messages"][-settings.memory_recent_turn_limit :]
                state["constraint_version"] += 1
                state["steps"] = {}
                state["step_tools"] = {}
                state["phase"] = "plan"
                state["finalizing"] = False
                state["pending"] = None
                state["feedback"] = "用户更新条件，请评估旧证据失效范围并按新约束规划。"
                state["waiting_question"] = ""
                state["answer"] = ""
                state["invalid_outputs"] = 0
            if state.get("active_started_at"):
                elapsed = max(0, time.time() - state.pop("active_started_at"))
                state["usage"]["active_seconds"] += elapsed
                state["turn_usage"]["active_seconds"] += elapsed
            if body.action == "message":
                state["turn_number"] += 1
                state["turn_usage"] = {
                    "model_calls": 0,
                    "tool_calls": 0,
                    "replans": 0,
                    "active_seconds": 0,
                }
            state["seq"] += 1
            result = await session.execute(
                update(Task)
                .where(Task.id == task_id, Task.revision == task.revision)
                .values(
                    state=state,
                    status=status,
                    revision=task.revision + 1,
                    lease_owner=None,
                    lease_until=0,
                    updated_at=time.time(),
                )
            )
            if not result.rowcount:
                await session.rollback()
                raise HTTPException(409, "任务状态刚刚更新，请重试")
            session.add(
                TaskEvent(task_id=task_id, seq=state["seq"], kind="user_control", payload=body.model_dump())
            )
            task_message = None
            if body.action == "message":
                task_message, _ = await task_context.record_user_message(
                    session,
                    task,
                    state["seq"],
                    body.message,
                    settings,
                    update_constraints=not bool(notice),
                )
            history_payload = body.model_dump()
            if task_message:
                history_payload["message_ids"] = [task_message.id]
            if notice:
                reply = await task_context.record_assistant_message(
                    session,
                    task,
                    state["seq"],
                    notice,
                    settings,
                    kind="notice",
                    scope_revision=scope_revision,
                )
                history_payload.setdefault("message_ids", []).append(reply.id)
            await memory.append_history(
                session, task, state["seq"], "user_control", history_payload, settings
            )
            await session.execute(
                update(Run)
                .where(Run.task_id == task_id, Run.status == "running")
                .values(status="interrupted", ended_at=time.time())
            )
            await session.commit()
            await session.refresh(task)
            return task_view(task)

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0, user=Depends(current_user)):
        async with database.sessions() as session:
            await own_task(session, task_id, user.id)
        try:
            cursor = max(0, after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "事件序号无效") from None

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                async with database.sessions() as session:
                    task = await own_task(session, task_id, user.id)
                    rows = (
                        await session.scalars(
                            select(TaskEvent)
                            .where(TaskEvent.task_id == task_id, TaskEvent.seq > cursor)
                            .order_by(TaskEvent.seq)
                            .limit(100)
                        )
                    ).all()
                    for row in rows:
                        cursor = row.seq
                        current_revision = await access.scope_revision(session, user.id)
                        visible_payload = row.payload
                        if (
                            row.kind != "user_control"
                            and row.payload.get("scope_revision", current_revision) != current_revision
                        ):
                            visible_payload = {"message": "原资料或记忆范围已变化，此历史事件内容已隐藏"}
                        payload = json.dumps(
                            {"kind": row.kind, "payload": visible_payload, "at": row.created_at},
                            ensure_ascii=False,
                        )
                        yield f"id: {row.seq}\nevent: update\ndata: {payload}\n\n"
                    if task.status in TERMINAL | {"paused", "waiting_user"} and len(rows) < 100:
                        yield f"event: done\ndata: {json.dumps({'status': task.status})}\n\n"
                        return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.8)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/tasks/{task_id}/audit")
    async def audit(task_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            task = await own_task(session, task_id, user.id)
            runs = (
                await session.scalars(select(Run).where(Run.task_id == task_id).order_by(Run.started_at))
            ).all()
            return {
                "plans": (await safe_task_view(session, task))["state"]["plans"],
                "usage": task.state["usage"],
                "turn_usage": task.state["turn_usage"],
                "turn_number": task.state["turn_number"],
                "budget": task.state["budget"],
                "runs": [
                    {
                        "id": r.id,
                        "status": r.status,
                        "started_at": r.started_at,
                        "ended_at": r.ended_at,
                        "configuration": r.configuration,
                    }
                    for r in runs
                ],
            }

    @app.get("/api/tasks/{task_id}/evidence/{evidence_id}")
    async def evidence(task_id: str, evidence_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            await own_task(session, task_id, user.id)
            e = await session.scalar(
                select(Evidence).where(Evidence.id == evidence_id, Evidence.task_id == task_id)
            )
            if not e:
                raise HTTPException(404, "证据不存在")
            if not await access.references_allowed(session, user.id, e.result):
                raise HTTPException(403, "资料来源已失效，请重新检索")
            return {
                "id": e.id,
                "tool": e.tool,
                "arguments": e.arguments,
                "result": e.result,
                "created_at": e.created_at,
            }

    @app.get("/api/tasks/{task_id}/artifacts")
    async def artifacts(task_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            task = await own_task(session, task_id, user.id)
            rows = (
                await session.scalars(
                    select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.created_at.desc())
                )
            ).all()
            return [
                {
                    "id": a.id,
                    "title": a.title,
                    "format": a.format,
                    "version": a.version,
                    "content": a.content,
                    "evidence_ids": a.evidence_ids,
                }
                for a in rows
                if await artifact_allowed(session, task, a)
            ]

    @app.patch("/api/tasks/{task_id}/artifacts/{artifact_id}")
    async def edit_artifact(task_id: str, artifact_id: str, body: ArtifactEdit, user=Depends(current_user)):
        async with database.sessions() as session:
            task = await own_task(session, task_id, user.id)
            a = await session.scalar(
                select(Artifact).where(Artifact.task_id == task_id, Artifact.id == artifact_id)
            )
            if not a:
                raise HTTPException(404, "成果不存在")
            if not await artifact_allowed(session, task, a):
                raise HTTPException(403, "成果引用的资料已失效")
            copies = (
                await session.scalars(
                    select(Artifact.version).where(Artifact.task_id == task_id, Artifact.title == a.title)
                )
            ).all()
            new = Artifact(
                id="ar_" + uid(),
                task_id=task_id,
                title=a.title,
                format=a.format,
                content=body.content,
                evidence_ids=a.evidence_ids,
                version=max(copies) + 1,
            )
            session.add(new)
            await session.commit()
            return {"id": new.id, "version": new.version}

    @app.get("/api/tasks/{task_id}/artifacts/{artifact_id}/download")
    async def download(task_id: str, artifact_id: str, user=Depends(current_user)):
        async with database.sessions() as session:
            task = await own_task(session, task_id, user.id)
            a = await session.scalar(
                select(Artifact).where(Artifact.task_id == task_id, Artifact.id == artifact_id)
            )
            if not a:
                raise HTTPException(404, "成果不存在")
            if not await artifact_allowed(session, task, a):
                raise HTTPException(403, "成果引用的资料已失效")
            content = a.content
            if a.format == "csv":
                import csv
                import io

                output = io.StringIO()
                writer = csv.writer(output)
                for row in csv.reader(io.StringIO(content)):
                    writer.writerow(
                        [
                            "'" + cell if cell.lstrip().startswith(("=", "+", "-", "@")) else cell
                            for cell in row
                        ]
                    )
                content = "\ufeff" + output.getvalue()
            return Response(
                content,
                media_type="text/csv" if a.format == "csv" else "text/markdown",
                headers={
                    "Content-Disposition": f'attachment; filename="{a.id}.{"csv" if a.format == "csv" else "md"}"'
                },
            )

    from app.resource_api import router

    app.include_router(router(database, settings))

    return app


app = create_app()
