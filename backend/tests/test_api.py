import copy
import json

from httpx import ASGITransport, AsyncClient

from app.models import Task, TaskEvent


async def test_auth_task_and_cross_user_isolation(client):
    created = await client.post("/api/tasks", json={"goal": "分析销售变化"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert (await client.get("/api/overview")).status_code == 200
    async with AsyncClient(transport=ASGITransport(app=client.test_app), base_url="http://test") as bob:
        assert (await bob.get(f"/api/tasks/{task_id}")).status_code == 401
        await bob.post("/api/auth/register", json={"username": "bob", "password": "different-password"})
        for suffix in ["", "/conversation", "/events", "/artifacts", "/audit", "/evidence/ev_fake"]:
            assert (await bob.get(f"/api/tasks/{task_id}{suffix}")).status_code == 404
        assert (await bob.post(f"/api/tasks/{task_id}/control", json={"action": "cancel"})).status_code == 404


async def test_control_preserves_budget_and_invalidates_old_work(client):
    task = (await client.post("/api/tasks", json={"goal": "分析库存"})).json()
    task_id = task["id"]
    async with client.test_app.state.db.sessions() as session, session.begin():
        stored = await session.get(Task, task_id)
        state = copy.deepcopy(stored.state)
        state["usage"]["model_calls"] = 29
        state["turn_usage"]["model_calls"] = 29
        stored.state = state
    paused = (await client.post(f"/api/tasks/{task_id}/control", json={"action": "pause"})).json()
    assert paused["status"] == "paused"
    revised = (
        await client.post(
            f"/api/tasks/{task_id}/control", json={"action": "message", "message": "只分析SKU-0001"}
        )
    ).json()
    assert revised["state"]["constraint_version"] == 2
    assert revised["state"]["budget"] == task["state"]["budget"]
    assert revised["state"]["usage"]["model_calls"] == 29
    assert revised["state"]["turn_usage"]["model_calls"] == 0
    assert revised["state"]["turn_number"] == 2
    assert revised["state"]["messages"][-1]["content"] == "只分析SKU-0001"
    await client.post(f"/api/tasks/{task_id}/control", json={"action": "cancel"})
    events = await client.get(f"/api/tasks/{task_id}/events?after=1")
    assert "id: 1\n" not in events.text
    assert "event: done" in events.text
    assert (await client.post(f"/api/tasks/{task_id}/control", json={"action": "resume"})).status_code == 409


async def test_conversation_restores_all_user_and_assistant_turns(client):
    task_id = (await client.post("/api/tasks", json={"goal": "第一轮问题"})).json()["id"]
    database = client.test_app.state.db
    async with database.sessions() as session, session.begin():
        task = await session.get(Task, task_id)
        state = {**task.state, "seq": 3, "answer": "第二轮回答"}
        state["messages"] = [
            {"role": "user", "content": "第一轮问题"},
            {"role": "user", "content": "第二轮追问"},
        ]
        task.state = state
        task.status = "completed"
        session.add_all(
            [
                TaskEvent(
                    task_id=task_id,
                    seq=1,
                    kind="evaluation",
                    payload={"kind": "finish", "answer": "第一轮回答", "scope_revision": 0},
                ),
                TaskEvent(
                    task_id=task_id,
                    seq=2,
                    kind="user_control",
                    payload={"action": "message", "message": "第二轮追问"},
                ),
                TaskEvent(
                    task_id=task_id,
                    seq=3,
                    kind="evaluation",
                    payload={"kind": "finish", "answer": "第二轮回答", "scope_revision": 0},
                ),
            ]
        )

    turns = (await client.get(f"/api/tasks/{task_id}/conversation")).json()
    assert [(turn["role"], turn["content"]) for turn in turns] == [
        ("user", "第一轮问题"),
        ("assistant", "第一轮回答"),
        ("user", "第二轮追问"),
        ("assistant", "第二轮回答"),
    ]


async def test_documents_and_origin_protection(client):
    response = await client.post(
        "/api/documents",
        files={"file": ("brand.md", "只使用已知商品属性。禁止夸大宣传。".encode(), "text/markdown")},
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    assert len((await client.get("/api/documents")).json()) == 1
    assert (await client.patch(f"/api/documents/{document_id}", json={"enabled": False})).status_code == 200
    assert (
        await client.post("/api/tasks", json={"goal": "恶意请求"}, headers={"origin": "https://evil.test"})
    ).status_code == 403
    assert (await client.delete(f"/api/documents/{document_id}")).status_code == 200
    assert (await client.get("/api/documents")).json() == []
    assert "password" not in json.dumps((await client.get("/api/auth/me")).json())
