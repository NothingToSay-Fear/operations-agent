"""新增能力的权限、确认、版本、恢复与任务边界回归。"""

import copy
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app import access, background, knowledge, memory, task_context
from app.agent.runtime import AgentRuntime, ContextChanged, initial_state
from app.document_parser import parse_document
from app.extension_models import (
    BackgroundJob,
    DocumentScope,
    HistoryUnit,
    ModelContextSnapshot,
    TaskConstraintEvent,
    TaskMessage,
    TaskMemory,
)
from app.main import ensure_admin
from app.models import Document, Task, User
from app.extension_models import UserMemory


async def drain(database, settings, maximum=100):
    for _ in range(maximum):
        job = await background.claim(database, settings)
        if not job:
            return
        await background.process(database, settings, *job)
    raise AssertionError("后台任务没有收敛")


async def test_admin_public_sources_and_independent_user_settings(client, settings):
    db = client.test_app.state.db
    assert (await client.get("/api/auth/me")).json()["is_admin"] is False
    assert (
        await client.post(
            "/api/documents",
            data={"shared": "true"},
            files={"file": ("forbidden.md", "普通用户不能发布公共资料".encode())},
        )
    ).status_code == 403

    async with AsyncClient(transport=ASGITransport(app=client.test_app), base_url="http://test") as admin:
        signed_in = await admin.post("/api/auth/login", json={"username": "admin", "password": "adminadmin"})
        assert signed_in.status_code == 200
        assert signed_in.json()["is_admin"] is True
        doc = (
            await admin.post(
                "/api/documents",
                files={"file": ("rule.md", "公共资料暗号青松库存规则".encode())},
            )
        ).json()
        await drain(db, settings)

        sources = (await client.get("/api/documents")).json()
        assert sources[0]["shared"] is True
        assert sources[0]["enabled"] is False
        assert (await client.get(f"/api/documents/{doc['id']}/content")).status_code == 404
        assert (await client.patch(f"/api/documents/{doc['id']}", json={"enabled": True})).status_code == 200
        assert (await client.get(f"/api/documents/{doc['id']}/content")).status_code == 200
        alice_id = (await client.get("/api/auth/me")).json()["id"]
        async with db.sessions() as session, db.read_sessions() as commerce:
            alice_result = await knowledge.search(session, commerce, alice_id, "青松库存", settings)
            assert any(row["document_id"] == doc["id"] for row in alice_result["rows"])

        assert (await client.delete(f"/api/documents/{doc['id']}")).status_code == 403
        assert (await client.get("/api/teams")).status_code == 404

    async with AsyncClient(transport=ASGITransport(app=client.test_app), base_url="http://test") as bob:
        user = (
            await bob.post(
                "/api/auth/register", json={"username": "bob-user", "password": "test-password-123"}
            )
        ).json()
        sources = (await bob.get("/api/documents")).json()
        assert sources[0]["enabled"] is False
        assert (await bob.get(f"/api/documents/{doc['id']}/content")).status_code == 404
        async with db.sessions() as session, db.read_sessions() as commerce:
            bob_result = await knowledge.search(session, commerce, user["id"], "青松库存", settings)
            assert not any(row["document_id"] == doc["id"] for row in bob_result["rows"])

        async with AsyncClient(
            transport=ASGITransport(app=client.test_app), base_url="http://test"
        ) as admin_delete:
            await admin_delete.post("/api/auth/login", json={"username": "admin", "password": "adminadmin"})
            assert (await admin_delete.delete(f"/api/documents/{doc['id']}")).status_code == 200
        assert (await bob.get("/api/documents")).json() == []
        assert (await bob.get(f"/api/documents/{doc['id']}/content")).status_code == 404
        async with db.sessions() as session:
            assert not await access.references_allowed(session, alice_id, alice_result)


async def test_existing_admin_private_document_is_normalized(client, settings):
    database = client.test_app.state.db
    async with database.sessions() as session, session.begin():
        admin = await session.scalar(select(User).where(User.username == "admin"))
        document = Document(user_id=admin.id, title="管理员旧资料", content="旧内容")
        session.add(document)
        await session.flush()
        session.add(DocumentScope(document_id=document.id, shared=0))
        document_id = document.id

    await ensure_admin(database, settings)
    async with database.sessions() as session:
        scope = await session.get(DocumentScope, document_id)
        assert scope.shared == 1


async def test_confirmed_memory_edit_expiry_and_delete(client):
    proposal = (
        await client.post(
            "/api/memory-candidates", json={"content": "回答优先使用简短表格", "kind": "answer_preference"}
        )
    ).json()
    assert (await client.get("/api/memories")).json() == []
    task_id = (await client.post("/api/tasks", json={"goal": "分析库存"})).json()["id"]
    assert (await client.get(f"/api/tasks/{task_id}/memory")).json()["selected_memories"] == []
    approved = (
        await client.post(
            f"/api/memory-candidates/{proposal['id']}/confirm", json={"version": proposal["version"]}
        )
    ).json()
    selected = (await client.get(f"/api/tasks/{task_id}/memory")).json()["selected_memories"]
    assert selected[0]["content"] == "回答优先使用简短表格"
    assert (
        await client.post(f"/api/memory-candidates/{proposal['id']}/confirm", json={"version": 1})
    ).status_code == 409
    edit = (
        await client.post(
            "/api/memory-candidates",
            json={
                "content": "回答先说明假设",
                "kind": "answer_preference",
                "replaces_id": approved["id"],
                "replaces_version": 1,
            },
        )
    ).json()
    assert (await client.get("/api/memories")).json()[0]["content"] == "回答优先使用简短表格"
    await client.post(f"/api/memories/{approved['id']}/control", json={"action": "disable", "version": 1})
    assert (
        await client.post(f"/api/memory-candidates/{edit['id']}/confirm", json={"version": 1})
    ).status_code == 409
    await client.post(f"/api/memories/{approved['id']}/control", json={"action": "delete", "version": 2})
    assert (await client.get("/api/memories")).json() == []
    assert (await client.get(f"/api/tasks/{task_id}/memory")).json()["selected_memories"] == []
    audit = (await client.get("/api/memories/events")).json()
    assert "简短表格" not in str(audit)


async def test_explicit_remember_is_only_candidate(client):
    task = (await client.post("/api/tasks", json={"goal": "记住：以后回答先列出关键假设"})).json()
    candidates = (await client.get("/api/memory-candidates")).json()
    assert len(candidates) == 1 and candidates[0]["task_id"] == task["id"]
    assert (await client.get("/api/memories")).json() == []


async def test_preference_update_creates_replacement_without_restarting_task(client):
    original = (
        await client.post(
            "/api/memory-candidates",
            json={"content": "运营建议最多给三条", "kind": "answer_preference"},
        )
    ).json()
    accepted = (
        await client.post(
            f"/api/memory-candidates/{original['id']}/confirm",
            json={"version": original["version"]},
        )
    ).json()
    task = (await client.post("/api/tasks", json={"goal": "分析最近七天 GMV"})).json()

    response = await client.post(
        f"/api/tasks/{task['id']}/control",
        json={"action": "message", "message": "更新长期偏好：运营建议固定给两条，不再使用三条规则"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert "原分析任务不会重新执行" in response.json()["state"]["answer"]
    candidates = (await client.get("/api/memory-candidates")).json()
    update = next(item for item in candidates if item["task_id"] == task["id"])
    assert update["content"] == "运营建议固定给两条，不再使用三条规则"
    assert update["replaces_id"] == accepted["id"]
    assert update["replaces_version"] == 1


async def test_new_task_preference_command_does_not_start_business_workflow(client):
    response = await client.post(
        "/api/tasks",
        json={"goal": "长期偏好：运营建议最多给三条。请记录为长期记忆"},
    )

    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "completed"
    assert "已生成长期偏好候选" in task["state"]["answer"]
    candidates = await client.get("/api/memory-candidates")
    assert candidates.status_code == 200
    assert candidates.json()[0]["content"] == "运营建议最多给三条。请记录为长期记忆"


async def test_effective_constraints_replace_old_turn_and_full_messages_remain(client):
    goal = "分析数据截止日期之前七天的GMV，结合渠道和商品数据，给出三个有证据的运营建议。"
    task = (await client.post("/api/tasks", json={"goal": goal})).json()
    follow_up = "不看商品了，只分析渠道，建议改成两条。"
    response = await client.post(
        f"/api/tasks/{task['id']}/control",
        json={"action": "message", "message": follow_up},
    )
    assert response.status_code == 200

    context = (await client.get(f"/api/tasks/{task['id']}/memory")).json()
    business = context["effective_constraints"]["business_constraints"]
    assert business["metrics"] == ["paid_gmv"]
    assert business["dimensions"] == ["channel"]
    assert context["effective_constraints"]["output_requirements"]["recommendation_count"] == 2
    assert context["constraint_state_version"] == 2

    conversation = (await client.get(f"/api/tasks/{task['id']}/conversation")).json()
    assert [row["content"] for row in conversation if row["role"] == "user"] == [goal, follow_up]
    database = client.test_app.state.db
    async with database.sessions() as session:
        assert (
            len(list(await session.scalars(select(TaskMessage).where(TaskMessage.task_id == task["id"]))))
            == 2
        )
        event = await session.scalar(
            select(TaskConstraintEvent).where(TaskConstraintEvent.task_id == task["id"])
        )
        assert event.from_version == 1 and event.to_version == 2


async def test_recent_window_is_bounded_and_context_manifest_is_persisted(database, settings):
    settings = settings.model_copy(update={"memory_recent_turn_limit": 4})
    async with database.sessions() as session:
        task = Task(user_id="test-user", goal="分析GMV", state=initial_state("分析GMV", settings))
        session.add(task)
        await session.flush()
        await task_context.record_user_message(session, task, 0, task.goal, settings, kind="goal")
        for seq in range(1, 8):
            await task_context.record_user_message(session, task, seq, f"第{seq}轮只分析渠道", settings)
        built = await memory.build_context(session, task, settings)
        assert len(built["recent_turns"]) == 4
        assert built["recent_turns"][0]["content"] == "第4轮只分析渠道"
        snapshot = await task_context.persist_context_snapshot(
            session, task, 1, "plan", built["context_manifest"]
        )
        await session.commit()
        assert (
            len(list(await session.scalars(select(TaskMessage).where(TaskMessage.task_id == task.id)))) == 8
        )
        stored = await session.get(ModelContextSnapshot, snapshot.id)
        assert stored.manifest["constraint_state_version"] == 8
        assert stored.manifest["recent_message_ids"] == [row["message_id"] for row in built["recent_turns"]]


async def test_summary_cannot_overwrite_message_appended_during_compression(database, settings, monkeypatch):
    from app import auxiliary

    settings = settings.model_copy(
        update={"llm_model": "测试协议", "llm_api_key": "测试替身凭据", "memory_recent_turn_limit": 4}
    )
    async with database.sessions() as session:
        task = Task(user_id="test-user", goal="分析渠道", state=initial_state("分析渠道", settings))
        session.add(task)
        await session.flush()
        for seq in range(5):
            await task_context.record_user_message(session, task, seq, f"第{seq}轮分析渠道", settings)
        await session.commit()
        job = await session.scalar(
            select(BackgroundJob).where(BackgroundJob.task_id == task.id, BackgroundJob.kind == "summary")
        )
        job.status, job.lease_token, job.lease_until, job.attempts = (
            "running",
            "并发压缩令牌",
            time.time() + 120,
            1,
        )
        await session.commit()

    async def append_during_summary(_settings, kind, payload):
        async with database.sessions() as session:
            stored = await session.get(Task, task.id)
            await task_context.record_user_message(session, stored, 5, "压缩期间新增消息", settings)
            await session.commit()
        return {
            "summary": "旧快照摘要",
            "decisions": [],
            "open_questions": [],
            "source_ids": payload["source_ids"],
        }, {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(auxiliary, "call", append_during_summary)
    await background.process(database, settings, job.id, "并发压缩令牌")
    async with database.sessions() as session:
        stored_job = await session.get(BackgroundJob, job.id)
        stored_memory = await session.get(TaskMemory, task.id)
        assert stored_job.status == "queued"
        assert stored_memory.summary == {}
        assert stored_memory.recent_turns[-1]["content"] == "压缩期间新增消息"


async def test_memory_confirmation_revalidates_edited_secret(client):
    candidate = (
        await client.post(
            "/api/memory-candidates",
            json={"content": "回答使用表格", "kind": "answer_preference"},
        )
    ).json()
    response = await client.post(
        f"/api/memory-candidates/{candidate['id']}/confirm",
        json={"version": 1, "content": "API_KEY=sk-this-must-not-be-stored"},
    )
    assert response.status_code == 409


async def test_history_never_crosses_same_users_tasks(database, settings):
    async with database.sessions() as session:
        first = Task(user_id="test-user", goal="青松暗号", state=initial_state("青松暗号", settings))
        second = Task(user_id="test-user", goal="白鹭暗号", state=initial_state("白鹭暗号", settings))
        session.add_all([first, second])
        await session.flush()
        await memory.append_history(session, first, 0, "user", {"content": first.goal}, settings)
        await memory.append_history(session, second, 0, "user", {"content": second.goal}, settings)
        await session.commit()
        result = await memory.history_search(session, first, "暗号", settings)
        assert len(result["rows"]) == 1 and "白鹭" not in str(result)
        other = await session.scalar(select(HistoryUnit).where(HistoryUnit.task_id == second.id))
        with pytest.raises(ValueError):
            await memory.history_search(session, first, "", settings, unit_id=other.id)


async def test_old_index_cannot_overwrite_new_version_and_deleted_source(database, settings):
    async with database.sessions() as session:
        doc = Document(user_id="test-user", title="规则", content="")
        session.add(doc)
        await session.flush()
        session.add(DocumentScope(document_id=doc.id))
        one = await knowledge.enqueue_version(session, doc, "one.md", "旧促销规则".encode())
        two = await knowledge.enqueue_version(session, doc, "two.md", "新促销规则".encode())
        await session.commit()
        prepared = await knowledge.prepare_index(one, settings)
        await knowledge.publish_index(session, one, prepared)
        await session.commit()
        assert one.status == "superseded"
        assert (await session.get(DocumentScope, doc.id)).active_version_id is None
        await knowledge.publish_index(session, two, await knowledge.prepare_index(two, settings))
        await session.commit()
        assert (await session.get(DocumentScope, doc.id)).active_version_id == two.id
        scope = await session.get(DocumentScope, doc.id)
        scope.deleted = 1
        await session.commit()
        with pytest.raises(ValueError):
            await knowledge.publish_index(session, two, prepared)


async def test_background_recovers_expired_lease_and_fences_old_worker(database, settings):
    async with database.sessions() as session:
        doc = Document(user_id="test-user", title="恢复规则", content="")
        session.add(doc)
        await session.flush()
        session.add(DocumentScope(document_id=doc.id))
        version = await knowledge.enqueue_version(session, doc, "rules.md", "恢复后的库存规范".encode())
        await session.commit()
    job_id, old_token = await background.claim(database, settings)
    async with database.sessions() as session, session.begin():
        await session.execute(update(BackgroundJob).where(BackgroundJob.id == job_id).values(lease_until=0))
    _, new_token = await background.claim(database, settings)
    await background.process(database, settings, job_id, old_token)
    async with database.sessions() as session:
        assert (await session.get(DocumentScope, doc.id)).active_version_id is None
    await background.process(database, settings, job_id, new_token)
    async with database.sessions() as session:
        assert (await session.get(DocumentScope, doc.id)).active_version_id == version.id


async def test_memory_change_fences_model_commit(database, settings):
    async with database.sessions() as session:
        task = Task(user_id="test-user", goal="库存", state=initial_state("库存", settings))
        session.add(task)
        await session.flush()
        scope = await access.scope_revision(session, task.user_id)
        await session.commit()
    runtime = AgentRuntime(database, settings)
    assert await runtime.claim(task.id)
    task = await runtime.load(task.id)
    task._scope_revision = scope
    async with database.sessions() as session, session.begin():
        await access.bump_scope(session, task.user_id.split())
    with pytest.raises(ContextChanged):
        await runtime.commit(task, copy.deepcopy(task.state), "test", {})


async def test_missing_model_blocks_compaction_without_fake_summary(database, settings):
    async with database.sessions() as session:
        task = Task(user_id="test-user", goal="摘要", state=initial_state("摘要", settings))
        session.add(task)
        await session.flush()
        for seq in range(5):
            await task_context.record_user_message(
                session, task, seq, "摘要" if seq == 0 else f"第{seq}轮讨论", settings
            )
        session.add(BackgroundJob(key="summary:test", kind="summary", target_id=task.id, task_id=task.id))
        await session.commit()
    job = await background.claim(database, settings)
    await background.process(database, settings, *job)
    async with database.sessions() as session:
        assert (await session.get(BackgroundJob, job[0])).status == "blocked"
        assert (await session.get(TaskMemory, task.id)).summary == {}


def test_structured_parser_preserves_headings_tables_and_page_context():
    parsed = parse_document(
        "rules.md",
        "# 品牌规范\n\n## 库存\n库存覆盖至少十四天。\n\n|商品|规则|\n|---|---|\n|青松|提前补货|".encode(),
    )
    assert any("库存" in (c.heading_path or "") for c in parsed.chunks)
    assert any(c.content_type == "table" and "商品" in c.content for c in parsed.chunks)
    csv = parse_document("rules.csv", "商品,库存\n青松,10".encode())
    assert csv.chunks[0].content_type == "table"


async def test_summary_and_candidate_jobs_use_real_contracts_and_shared_budget(
    database, settings, monkeypatch
):
    from app import auxiliary

    settings = settings.model_copy(update={"llm_model": "测试协议", "llm_api_key": "测试替身凭据"})
    async with database.sessions() as session:
        task = Task(
            user_id="test-user", goal="我通常先看毛利", state=initial_state("我通常先看毛利", settings)
        )
        session.add(task)
        await session.flush()
        for seq in range(8):
            await task_context.record_user_message(
                session,
                task,
                seq,
                task.goal if seq == 0 else f"我通常先看毛利，讨论 {seq}",
                settings,
            )
            await memory.append_history(
                session, task, seq, "user", {"content": f"我通常先看毛利，讨论 {seq}"}, settings
            )
        job = BackgroundJob(key="summary:manual", kind="summary", target_id=task.id, task_id=task.id)
        session.add(job)
        await session.commit()

    async def structured_fixture(settings, kind, payload):
        if kind == "summary":
            return {
                "summary": "讨论时关注毛利，仍需查询实际经营数据。",
                "decisions": [],
                "open_questions": ["实际毛利是多少？"],
                "source_ids": payload["source_ids"],
            }, {"input_tokens": 12, "output_tokens": 8}
        return {
            "items": [
                {"kind": "analysis_preference", "content": "分析时先看毛利", "source_quote": "我通常先看毛利"}
            ]
        }, {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(auxiliary, "call", structured_fixture)
    # 定向处理压缩和一条候选，避免将其他待提取消息计为同一次验证。
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(BackgroundJob).where(BackgroundJob.kind == "candidates").values(status="blocked")
        )
        job = await session.scalar(select(BackgroundJob).where(BackgroundJob.key == "summary:manual"))
        job.status, job.lease_token, job.lease_until, job.attempts = (
            "running",
            "summary-token",
            time.time() + 120,
            1,
        )
    await background.process(database, settings, job.id, "summary-token")
    async with database.sessions() as session:
        summary = await session.get(TaskMemory, task.id)
        assert summary and summary.through_seq == 1
        assert summary.summary["source_ids"]
        candidate_job = await session.scalar(
            select(BackgroundJob).where(BackgroundJob.kind == "candidates").limit(1)
        )
        candidate_job.status, candidate_job.lease_token, candidate_job.lease_until, candidate_job.attempts = (
            "running",
            "candidate-token",
            time.time() + 120,
            1,
        )
        await session.commit()
    await background.process(database, settings, candidate_job.id, "candidate-token")
    async with database.sessions() as session:
        from app.extension_models import MemoryCandidate

        assert await session.scalar(select(MemoryCandidate).where(MemoryCandidate.user_id == "test-user"))
        assert not list(await session.scalars(select(UserMemory)))
        stored = await session.get(Task, task.id)
        assert stored.state["usage"]["model_calls"] == 2
        assert stored.state["usage"]["input_tokens"] == 22
        old_sources = set((await session.get(TaskMemory, task.id)).summary["source_ids"])
        for seq in (8, 9):
            await task_context.record_user_message(session, stored, seq, "继续讨论毛利", settings)
            await memory.append_history(session, stored, seq, "user", {"content": "继续讨论毛利"}, settings)
        second = BackgroundJob(
            key="summary:second",
            kind="summary",
            target_id=task.id,
            task_id=task.id,
            status="running",
            lease_token="second-token",
            lease_until=time.time() + 120,
            attempts=1,
        )
        session.add(second)
        await session.commit()
    await background.process(database, settings, second.id, "second-token")
    async with database.sessions() as session:
        assert (await session.get(BackgroundJob, second.id)).status == "completed"
        latest = await session.get(TaskMemory, task.id)
        assert old_sources < set(latest.summary["source_ids"])


async def test_expired_memory_and_explicit_forget_never_reactivate(client):
    c = (
        await client.post(
            "/api/memory-candidates", json={"content": "回答使用简短表格", "kind": "answer_preference"}
        )
    ).json()
    result = (
        await client.post(
            f"/api/memory-candidates/{c['id']}/confirm", json={"version": 1, "expires_at": time.time() + 60}
        )
    ).json()
    db = client.test_app.state.db
    async with db.sessions() as session, session.begin():
        await session.execute(
            update(UserMemory).where(UserMemory.id == result["id"]).values(expires_at=time.time() - 1)
        )
    task = (await client.post("/api/tasks", json={"goal": "分析库存"})).json()
    assert not (await client.get(f"/api/tasks/{task['id']}/memory")).json()["selected_memories"]
    forgotten = (await client.post("/api/tasks", json={"goal": "清除所有记忆"})).json()
    assert forgotten["status"] == "completed"
    assert not (await client.get("/api/memories")).json()


async def test_compacted_evidence_keeps_revocation_references():
    from app.agent.runtime import compact

    result = compact(
        {
            "result": {
                "data": {
                    "source_refs": [{"document_id": "doc-id", "version_id": "version-id"}],
                    "text": "长内容" * 10000,
                }
            }
        }
    )
    assert result["truncated"]
    assert result["source_refs"] == [{"document_id": "doc-id", "version_id": "version-id"}]
