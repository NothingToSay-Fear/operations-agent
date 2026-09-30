"""集中清理一个会话及其直接产生的持久化内容。"""

from sqlalchemy import delete, or_, select

from app import access
from app.extension_models import (
    BackgroundJob,
    HistoryUnit,
    MemoryCandidate,
    MemoryEvent,
    TaskMemory,
    UserMemory,
)
from app.models import Artifact, Evidence, Run, Task, TaskEvent


async def delete_task_contents(session, task: Task) -> None:
    """在调用方持有任务行锁时，按外键依赖顺序删除会话内容。"""
    candidates = list(
        await session.scalars(select(MemoryCandidate).where(MemoryCandidate.task_id == task.id))
    )
    candidate_ids = [row.id for row in candidates]
    derived_memory_ids: list[str] = []
    if candidate_ids:
        derived_memory_ids = list(
            await session.scalars(
                select(UserMemory.id).where(
                    UserMemory.user_id == task.user_id,
                    UserMemory.source_candidate_id.in_(candidate_ids),
                )
            )
        )

    # 删除已确认记忆会改变其他任务可使用的上下文，必须推进用户范围版本。
    if derived_memory_ids:
        await access.bump_scope(session, [task.user_id])

    memory_event_filter = MemoryEvent.task_id == task.id
    if derived_memory_ids:
        memory_event_filter = or_(
            memory_event_filter,
            MemoryEvent.memory_id.in_(derived_memory_ids),
        )
    await session.execute(delete(MemoryEvent).where(memory_event_filter))
    if derived_memory_ids:
        await session.execute(delete(UserMemory).where(UserMemory.id.in_(derived_memory_ids)))
    await session.execute(delete(MemoryCandidate).where(MemoryCandidate.task_id == task.id))

    # 后台作业必须先删除，使已在途的 Worker 无法再发布处理结果。
    await session.execute(delete(BackgroundJob).where(BackgroundJob.task_id == task.id))
    await session.execute(delete(TaskMemory).where(TaskMemory.task_id == task.id))
    await session.execute(delete(HistoryUnit).where(HistoryUnit.task_id == task.id))
    await session.execute(delete(Artifact).where(Artifact.task_id == task.id))
    await session.execute(delete(Evidence).where(Evidence.task_id == task.id))
    await session.execute(delete(TaskEvent).where(TaskEvent.task_id == task.id))
    await session.execute(delete(Run).where(Run.task_id == task.id))
    await session.delete(task)
