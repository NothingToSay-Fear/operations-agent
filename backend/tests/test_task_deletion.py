from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.extension_models import (
    BackgroundJob,
    HistoryUnit,
    MemoryCandidate,
    MemoryEvent,
    ModelContextSnapshot,
    TaskConstraintEvent,
    TaskConstraintState,
    TaskMemory,
    TaskMessage,
    UserMemory,
)
from app.models import Artifact, Evidence, Run, Task, TaskEvent


async def test_delete_task_removes_all_related_content_and_preserves_unrelated_memory(client):
    task = (await client.post("/api/tasks", json={"goal": "分析本周经营变化"})).json()
    task_id = task["id"]
    user_id = (await client.get("/api/auth/me")).json()["id"]
    database = client.test_app.state.db

    async with database.sessions() as session, session.begin():
        stored = await session.get(Task, task_id)
        stored.status = "running"
        stored.lease_owner = "test-worker"
        stored.lease_until = 9999999999
        task_memory = await session.get(TaskMemory, task_id)
        task_memory.summary = {"summary": "待删除"}
        candidate = MemoryCandidate(
            user_id=user_id,
            task_id=task_id,
            kind="answer_preference",
            content="回答时优先给出表格",
            source="会话内容",
            source_seq=2,
            status="accepted",
        )
        independent_candidate = MemoryCandidate(
            user_id=user_id,
            kind="work_profile",
            content="负责商品运营",
            source="用户填写",
            status="accepted",
        )
        session.add_all([candidate, independent_candidate])
        await session.flush()
        derived_memory = UserMemory(
            user_id=user_id,
            kind="answer_preference",
            content="回答时优先给出表格",
            source_candidate_id=candidate.id,
        )
        independent_memory = UserMemory(
            user_id=user_id,
            kind="work_profile",
            content="负责商品运营",
            source_candidate_id=independent_candidate.id,
        )
        session.add_all([derived_memory, independent_memory])
        await session.flush()
        session.add_all(
            [
                Run(task_id=task_id, configuration={"model": "test"}),
                TaskEvent(task_id=task_id, seq=500, kind="test", payload={}),
                Evidence(
                    id="ev_delete_test",
                    task_id=task_id,
                    tool="query_metrics",
                    arguments={},
                    result={"data": {"rows": []}},
                ),
                Artifact(
                    id="ar_delete_test",
                    task_id=task_id,
                    title="测试成果",
                    content="待删除",
                ),
                MemoryEvent(
                    user_id=user_id,
                    memory_id=derived_memory.id,
                    task_id=task_id,
                    action="confirmed",
                ),
            ]
        )

    async with AsyncClient(transport=ASGITransport(app=client.test_app), base_url="http://test") as bob:
        await bob.post(
            "/api/auth/register",
            json={"username": "delete-test-bob", "password": "different-password"},
        )
        assert (await bob.delete(f"/api/tasks/{task_id}")).status_code == 404

    response = await client.delete(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": task_id}
    assert (await client.get(f"/api/tasks/{task_id}")).status_code == 404
    assert all(row["id"] != task_id for row in (await client.get("/api/tasks")).json())

    async with database.sessions() as session:
        assert await session.get(Task, task_id) is None
        for model in (
            Run,
            TaskEvent,
            Evidence,
            Artifact,
            BackgroundJob,
            TaskMemory,
            TaskMessage,
            TaskConstraintState,
            TaskConstraintEvent,
            ModelContextSnapshot,
            HistoryUnit,
        ):
            assert await session.scalar(select(model).where(model.task_id == task_id)) is None
        assert await session.scalar(select(MemoryCandidate).where(MemoryCandidate.task_id == task_id)) is None
        assert await session.scalar(select(MemoryEvent).where(MemoryEvent.task_id == task_id)) is None
        assert await session.get(UserMemory, derived_memory.id) is None
        assert await session.get(UserMemory, independent_memory.id) is not None
        assert await session.get(MemoryCandidate, independent_candidate.id) is not None
