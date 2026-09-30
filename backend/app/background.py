"""可恢复的资料索引、任务历史编码、摘要和记忆候选后台任务。"""

import asyncio
import copy
import json
import time

from sqlalchemy import or_, select, update

from app import access, auxiliary, knowledge, memory, retrieval
from app.extension_models import BackgroundJob, DocumentScope, DocumentVersion, HistoryUnit, TaskMemory
from app.models import Document, Task, uid


async def migrate_legacy(database):
    """既有上传资料保留原文并创建新索引，不推断或激活历史用户偏好。"""
    async with database.sessions() as session, session.begin():
        docs = (
            await session.scalars(
                select(Document).outerjoin(DocumentScope).where(DocumentScope.document_id.is_(None))
            )
        ).all()
        for doc in docs:
            session.add(DocumentScope(document_id=doc.id))
            await knowledge.enqueue_version(session, doc, doc.title + ".txt", doc.content.encode())


async def claim(database, settings):
    now = time.time()
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.attempts >= settings.background_max_attempts,
                or_(
                    BackgroundJob.status == "queued",
                    (BackgroundJob.status == "running") & (BackgroundJob.lease_until < now),
                ),
            )
            .values(status="failed", error="后台任务已达到重试上限，请检查原因后重新提交")
        )
        if settings.llm_enabled:
            await session.execute(
                update(BackgroundJob)
                .where(BackgroundJob.status == "blocked", BackgroundJob.kind.in_(["summary", "candidates"]))
                .values(status="queued")
            )
        eligible = or_(
            BackgroundJob.status == "queued",
            (BackgroundJob.status == "running") & (BackgroundJob.lease_until < now),
        )
        job = await session.scalar(
            select(BackgroundJob)
            .where(eligible, BackgroundJob.attempts < settings.background_max_attempts)
            .order_by(BackgroundJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return None
        token = uid()
        result = await session.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == job.id, eligible)
            .values(
                status="running",
                attempts=BackgroundJob.attempts + 1,
                lease_token=token,
                lease_until=now + settings.background_lease_seconds,
                progress=10,
                error="",
            )
        )
        if not result.rowcount:
            return None
        return job.id, token


async def heartbeat(database, settings, job_id, token):
    while True:
        await asyncio.sleep(max(1, settings.background_lease_seconds / 3))
        async with database.sessions() as session, session.begin():
            result = await session.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == job_id,
                    BackgroundJob.lease_token == token,
                    BackgroundJob.status == "running",
                )
                .values(lease_until=time.time() + settings.background_lease_seconds)
            )
            if not result.rowcount:
                return


async def reserve_model(database, job_id, token, settings):
    """调用前扣除持久化预算；失败、超时或进程退出也不会重置次数。"""
    async with database.sessions() as session, session.begin():
        job = await session.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.id == job_id, BackgroundJob.lease_token == token)
            .with_for_update()
        )
        if not job:
            raise ValueError("后台任务租约失效")
        jobs = list(await session.scalars(select(BackgroundJob).where(BackgroundJob.task_id == job.task_id)))
        if sum((j.usage or {}).get("model_calls", 0) for j in jobs) >= settings.maintenance_max_model_calls:
            raise ValueError("当前任务的后台模型调用预算已耗尽")
        task = await session.scalar(select(Task).where(Task.id == job.task_id).with_for_update())
        state = copy.deepcopy(task.state)
        if state["usage"]["model_calls"] >= state["budget"]["model_calls"]:
            raise ValueError("当前任务的共享模型调用预算已耗尽")
        if state["observations"] and state["budget"]["model_calls"] - state["usage"]["model_calls"] <= 2:
            raise ValueError("剩余额度保留给任务最终交付，暂停后台模型维护")
        state["usage"]["model_calls"] += 1
        # 维护用量不改变业务决策版本；前台在提交时按增量合并，避免丢弃在途模型结果。
        task.state = state
        job.usage = {**(job.usage or {}), "model_calls": (job.usage or {}).get("model_calls", 0) + 1}


async def prepare(database, settings, job, token):
    async with database.sessions() as session:
        if job.kind == "index":
            version = await session.get(DocumentVersion, job.target_id)
            if not version:
                raise ValueError("资料版本已删除")
            return await knowledge.prepare_index(version, settings)
        if job.kind == "history":
            unit = await session.get(HistoryUnit, job.target_id)
            if not unit:
                raise ValueError("历史单元已删除")
            try:
                vectors = await retrieval.encode([unit.content], settings)
                return {
                    "embedding": vectors[0] if vectors else None,
                    "error": "" if vectors else "历史当前仅支持词面检索",
                }
            except Exception:
                return {"embedding": None, "error": "历史向量编码失败，保留词面索引"}
        if not settings.llm_enabled:
            return {"blocked": True, "error": "等待配置真实对话模型"}
        task = await session.get(Task, job.task_id)
        revision = await access.scope_revision(session, task.user_id)
        if job.kind == "candidates":
            unit = await session.get(HistoryUnit, job.target_id)
            payload = json.loads(unit.content)
            text = payload.get("content", payload.get("message", ""))
            prompt = {"user_text": text}
            snapshot = {"text": text, "source_seq": unit.seq, "scope_revision": revision}
        else:
            previous = await session.get(TaskMemory, task.id)
            units = list(
                await session.scalars(
                    select(HistoryUnit).where(HistoryUnit.task_id == task.id).order_by(HistoryUnit.seq)
                )
            )
            units = [u for u in units if await memory.unit_allowed(session, task, u, revision)]
            valid_ids = {u.id for u in units}
            # 保留最近窗口，摘要只压缩较早且能够追溯的历史。
            units = units[:-6] if len(units) > 6 else units
            content, size = [], 0
            through = previous.through_seq if previous and previous.scope_revision == revision else 0
            for unit in units:
                if unit.seq <= through:
                    continue
                if size + len(unit.content) > settings.memory_context_char_budget:
                    break
                content.append({"id": unit.id, "text": unit.content})
                size += len(unit.content)
            ids = [row["id"] for row in content]
            previous_summary = previous.summary if previous and previous.scope_revision == revision else {}
            allowed_ids = sorted(set(ids) | (set(previous_summary.get("source_ids", [])) & valid_ids))
            prompt = {
                "previous": previous_summary,
                "history": content,
                "source_ids": allowed_ids,
            }
            snapshot = {
                "scope_revision": revision,
                "source_ids": allowed_ids,
                "through_seq": max((u.seq for u in units if u.id in ids), default=through),
                "previous_version": previous.version if previous else 0,
            }
    await reserve_model(database, job.id, token, settings)
    result, usage = await auxiliary.call(settings, job.kind, prompt)
    if job.kind == "summary" and not set(result["source_ids"]) <= set(snapshot["source_ids"]):
        raise ValueError("摘要引用了不存在的历史来源")
    return {"result": result, "usage": usage, **snapshot}


async def process(database, settings, job_id, token):
    started = time.monotonic()
    ticker = asyncio.create_task(heartbeat(database, settings, job_id, token))
    try:
        async with database.sessions() as session:
            job = await session.get(BackgroundJob, job_id)
        prepared = await asyncio.wait_for(
            prepare(database, settings, job, token), settings.maintenance_max_seconds
        )
        async with database.sessions() as session, session.begin():
            job = await session.scalar(
                select(BackgroundJob)
                .where(
                    BackgroundJob.id == job_id,
                    BackgroundJob.lease_token == token,
                    BackgroundJob.status == "running",
                    BackgroundJob.lease_until >= time.time(),
                )
                .with_for_update()
            )
            if not job:
                return
            if prepared.get("blocked"):
                job.status, job.error = "blocked", prepared["error"]
                job.attempts -= 1
                return
            if job.kind == "index":
                version = await session.get(DocumentVersion, job.target_id)
                await knowledge.publish_index(session, version, prepared)
            elif job.kind == "history":
                unit = await session.get(HistoryUnit, job.target_id)
                if unit:
                    unit.embedding = prepared["embedding"]
                    unit.model_id = settings.embedding_model_id if prepared["embedding"] else ""
            else:
                task = await session.get(Task, job.task_id)
                revision = await access.scope_revision(session, task.user_id, lock=True)
                task = await session.scalar(
                    select(Task)
                    .where(Task.id == job.task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                state = copy.deepcopy(task.state)
                for key in ["input_tokens", "output_tokens"]:
                    state["usage"][key] += prepared["usage"].get(key, 0)
                task.state = state
                if revision != prepared["scope_revision"]:
                    raise ValueError("上下文范围已变化，旧压缩或提取结果不再发布")
                if job.kind == "candidates":
                    for item in prepared["result"]["items"]:
                        if item["source_quote"] not in prepared["text"]:
                            continue
                        await memory.propose(
                            session,
                            task.user_id,
                            item["content"],
                            item["kind"],
                            task=task,
                            source=item["source_quote"],
                            source_seq=prepared["source_seq"],
                        )
                else:
                    current = await session.get(TaskMemory, task.id)
                    if current and current.version != prepared["previous_version"]:
                        raise ValueError("摘要版本已变化")
                    if not current:
                        current = TaskMemory(task_id=task.id, version=0)
                        session.add(current)
                    current.summary, current.through_seq = prepared["result"], prepared["through_seq"]
                    current.version += 1
                    current.scope_revision = revision
            job.status, job.progress, job.error = "completed", 100, prepared.get("error", "")
            job.usage = {
                **job.usage,
                **prepared.get("usage", {}),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        async with database.sessions() as session, session.begin():
            job = await session.scalar(
                select(BackgroundJob)
                .where(BackgroundJob.id == job_id, BackgroundJob.lease_token == token)
                .with_for_update()
            )
            if job:
                job.status = "queued" if job.attempts < settings.background_max_attempts else "failed"
                job.error = (
                    str(exc)[:250]
                    if isinstance(exc, ValueError)
                    else "后台处理失败或超时，请检查模型及资料后重试"
                )
                job.lease_until = 0
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)


async def worker(database, settings, stop):
    await migrate_legacy(database)
    while not stop.is_set():
        claimed = await claim(database, settings)
        if claimed:
            await process(database, settings, *claimed)
        else:
            try:
                await asyncio.wait_for(stop.wait(), settings.worker_poll_seconds)
            except TimeoutError:
                pass


async def main():
    from app.config import get_settings
    from app.db import Database

    settings = get_settings()
    database = Database(settings)
    try:
        await worker(database, settings, asyncio.Event())
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
