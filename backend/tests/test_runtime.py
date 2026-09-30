import copy

import pytest
from sqlalchemy import func, select, update

from app.agent.contracts import Action, Criterion, Decision, Plan, Step, Assessment
from app.agent.runtime import AgentRuntime, LeaseLost, initial_state
from app.agent.tools import calculate
from app.models import Artifact, Evidence, Task, TaskEvent


class ProtocolModel:
    """用于协议测试的确定性替身，不能作为真实模型质量评测。"""

    async def decide(self, phase, context):
        observations = context["observations"]
        if phase == "plan":
            revising = bool(context["plan"])
            return Plan(
                summary="库存调查" if revising else "了解经营范围",
                criteria=[Criterion(id="c1", description="产出有据可查的建议")],
                steps=[
                    Step(
                        id="inventory" if revising else "discover",
                        objective="检查可用库存" if revising else "检查数据范围",
                        done_when="取得证据并保存报告",
                    )
                ],
                change_reason="新证据表明需要检查库存" if revising else "初始规划",
            ), {}
        refs = [o["evidence_id"] for o in observations if o["status"] == "success"]
        if phase == "execute":
            names = [o["tool"] for o in observations]
            if "inspect_data_capabilities" not in names:
                return Action(kind="tool", tool="inspect_data_capabilities", summary="确认可用范围"), {}
            if context["plan"]["steps"][0]["id"] == "discover":
                return Action(kind="replan", summary="已取得范围信息，继续调查库存"), {}
            if "query_inventory" not in names:
                return Action(
                    kind="tool", tool="query_inventory", arguments={"limit": 5}, summary="查看库存"
                ), {}
            if "save_artifact" not in names:
                return Action(
                    kind="tool",
                    tool="save_artifact",
                    arguments={
                        "title": "库存建议",
                        "content": "模拟数据：请结合交期评估库存。",
                        "evidence_ids": refs,
                    },
                    summary="保存建议",
                ), {}
            return Action(kind="step_done", summary="已交付报告", evidence_ids=refs), {}
        artifacts = [a["artifact_id"] for a in context["artifacts"]]
        return Decision(
            kind="finish",
            reason="已有报告与证据",
            answer="模拟数据分析已完成。",
            assessments=[
                Assessment(
                    criterion_id="c1",
                    satisfied=True,
                    evidence_ids=refs,
                    artifact_ids=artifacts,
                    note="已核对",
                )
            ],
        ), {}


async def new_task(database, settings, **state_updates):
    state = initial_state("分析库存并给出建议", settings)
    state.update(state_updates)
    async with database.sessions() as session:
        task = Task(user_id="test-user", goal="分析库存并给出建议", state=state)
        session.add(task)
        await session.commit()
        return task.id


async def test_provider_error_exposes_safe_category_without_request_content(database, settings):
    import httpx
    from openai import BadRequestError

    class RejectedModel:
        async def decide(self, phase, context):
            raise BadRequestError(
                "private-request sk-secret-fixture",
                response=httpx.Response(400, request=httpx.Request("POST", "https://fixture.invalid")),
                body={"error": {"message": "private-request sk-secret-fixture"}},
            )

    tid = await new_task(database, settings)
    await AgentRuntime(database, settings, RejectedModel()).run(tid)
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        event = await session.scalar(
            select(TaskEvent).where(TaskEvent.task_id == tid, TaskEvent.kind == "model_error")
        )
        assert task.status == "blocked"
        assert "HTTP 400" in task.state["answer"]
        assert event.payload["error_code"] == "invalid_request"
        assert event.payload["http_status"] == 400
        assert "sk-secret" not in str(task.state) + str(event.payload)
        assert "private-request" not in str(task.state) + str(event.payload)


async def test_invalid_decision_retries_preserve_usage_and_stop(database, settings):
    from app.agent.model import StructuredOutputError

    class InvalidModel:
        async def decide(self, phase, context):
            raise StructuredOutputError("kind: literal_error", {"input_tokens": 100, "output_tokens": 50})

    tid = await new_task(database, settings)
    await AgentRuntime(database, settings, InvalidModel()).run(tid)
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        assert task.status == "failed"
        assert task.state["invalid_outputs"] == 3
        assert task.state["usage"]["model_calls"] == 3
        assert task.state["turn_usage"]["model_calls"] == 3
        assert task.state["usage"]["input_tokens"] == 300
        assert task.state["usage"]["output_tokens"] == 150


async def test_background_usage_does_not_discard_inflight_decision(database, settings):
    import time
    from app import background
    from app.extension_models import BackgroundJob

    tid = await new_task(database, settings)
    runtime = AgentRuntime(database, settings)
    assert await runtime.claim(tid)
    task = await runtime.reserve(await runtime.load(tid), "model_calls")
    async with database.sessions() as session, session.begin():
        job = BackgroundJob(
            key="parallel-budget",
            kind="summary",
            target_id=tid,
            task_id=tid,
            status="running",
            lease_token="test-token",
            lease_until=time.time() + 60,
        )
        session.add(job)
    await background.reserve_model(database, job.id, "test-token", settings)
    state = copy.deepcopy(task.state)
    runtime.account(state, {"input_tokens": 100, "output_tokens": 50})
    await runtime.commit(task, state, "decision_verified", {})
    current = await runtime.load(tid)
    assert current.state["usage"]["model_calls"] == 2
    assert current.state["turn_usage"]["model_calls"] == 1
    assert current.state["usage"]["input_tokens"] == 100
    assert current.state["usage"]["output_tokens"] == 50
    assert current.revision == task.revision + 1


async def test_background_budget_does_not_consume_interactive_turn(database, settings):
    import time
    from app import background
    from app.extension_models import BackgroundJob

    settings = settings.model_copy(update={"max_model_calls": 1})
    tid = await new_task(database, settings)
    runtime = AgentRuntime(database, settings)
    assert await runtime.claim(tid)
    stale = await runtime.load(tid)
    async with database.sessions() as session, session.begin():
        job = BackgroundJob(
            key="last-budget",
            kind="summary",
            target_id=tid,
            task_id=tid,
            status="running",
            lease_token="test-token",
            lease_until=time.time() + 60,
        )
        session.add(job)
    await background.reserve_model(database, job.id, "test-token", settings)
    await runtime.reserve(stale, "model_calls")
    current = await runtime.load(tid)
    assert current.state["usage"]["model_calls"] == 2
    assert current.state["turn_usage"]["model_calls"] == 1
    assert await runtime.reserve(current, "model_calls") is None


async def test_long_evidence_pages_are_readable_without_repeated_truncation(database, settings):
    from app.agent.tools import invoke
    from app.agent.runtime import compact

    tid = await new_task(database, settings)
    rows = [
        {"product_id": f"SKU-{i:04}", "paid_gmv": i * 10, "description": "商品属性" * 60} for i in range(30)
    ]
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        session.add(
            Evidence(
                id="ev_longtable",
                task_id=tid,
                tool="query_metrics",
                arguments={},
                result={"status": "success", "constraint_version": 1, "data": {"rows": rows}},
            )
        )
        await session.commit()
        collected, offset = [], 0
        while offset is not None:
            page = await invoke(
                "read_evidence",
                {"evidence_id": "ev_longtable", "offset": offset},
                commerce=None,
                session=session,
                task=task,
                settings=settings,
                artifact_id="unused",
            )
            assert not compact(page).get("truncated")
            collected.extend(page["result"]["data"]["rows"])
            offset = page["pagination"]["next_offset"]
        assert collected == rows


async def test_step_budget_routes_to_replan_and_preserves_global_budget(database, settings):
    settings = settings.model_copy(update={"max_step_tools": 1})
    tid = await new_task(database, settings)
    runtime = AgentRuntime(database, settings)
    assert await runtime.claim(tid)
    task = await runtime.load(tid)
    plan = Plan(
        summary="核对范围",
        criteria=[Criterion(id="c1", description="取得证据")],
        steps=[Step(id="s1", objective="核对范围", done_when="取得证据")],
        change_reason="初始计划",
    )
    await runtime.apply_plan(task, copy.deepcopy(task.state), plan)
    task = await runtime.load(tid)
    state = copy.deepcopy(task.state)
    state["step_tools"]["s1"] = 1
    state["usage"]["tool_calls"] = 1
    await runtime.apply_action(task, state, Action(kind="tool", tool="query_metrics", summary="继续调查"))
    task = await runtime.load(tid)
    assert task.state["phase"] == "plan" and task.state["pending"] is None
    revised = plan.model_copy(deep=True)
    revised.steps[0].objective = "调查剩余目标"
    await runtime.apply_plan(task, copy.deepcopy(task.state), revised)
    task = await runtime.load(tid)
    assert task.state["step_tools"]["s1"] == 0
    assert task.state["usage"]["tool_calls"] == 1


async def test_last_calls_are_reserved_for_grounded_delivery(database, settings):
    settings = settings.model_copy(update={"max_model_calls": 2})

    class DeliveryModel:
        async def decide(self, phase, context):
            assert phase == "evaluate" and "最终交付" in context["feedback"]
            return Decision(
                kind="stop",
                reason="没有更多调查额度",
                answer="已取得现有数据；尚缺商品维度，不能声称完成全部分析。",
            ), {}

    plan = Plan(
        summary="继续调查",
        criteria=[Criterion(id="c1", description="核对贡献")],
        steps=[Step(id="s1", objective="核对商品", done_when="取得商品证据")],
        change_reason="初始计划",
    )
    tid = await new_task(
        database,
        settings,
        phase="execute",
        plan=plan.model_dump(),
        observations=[
            {"evidence_id": "ev_present", "status": "success", "data": {}, "constraint_version": 1}
        ],
    )
    await AgentRuntime(database, settings, DeliveryModel()).run(tid)
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        assert task.status == "partial"
        assert "尚缺商品维度" in task.state["answer"]
        assert task.state["usage"]["model_calls"] == 1
        assert task.state["usage"]["tool_calls"] == 0


async def test_maintenance_has_separate_budget_from_interactive_delivery(database, settings):
    import time
    from app import background
    from app.extension_models import BackgroundJob

    settings = settings.model_copy(update={"max_model_calls": 2})
    tid = await new_task(database, settings, observations=[{"status": "success", "data": {}}])
    async with database.sessions() as session, session.begin():
        job = BackgroundJob(
            key="delivery-budget",
            kind="summary",
            target_id=tid,
            task_id=tid,
            status="running",
            lease_token="test-token",
            lease_until=time.time() + 60,
        )
        session.add(job)
    await background.reserve_model(database, job.id, "test-token", settings)
    async with database.sessions() as session:
        state = (await session.get(Task, tid)).state
        assert state["usage"]["model_calls"] == 1
        assert state["turn_usage"]["model_calls"] == 0


async def test_real_tools_plan_revision_artifacts_and_finish(database, settings):
    tid = await new_task(database, settings)
    runtime = AgentRuntime(database, settings, ProtocolModel())
    await runtime.run(tid)
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        assert task.status == "completed", task.state
        assert len(task.state["plans"]) == 2
        assert task.state["plans"][-1]["execution_snapshot"]["inventory"]["status"] == "succeeded"
        assert task.state["usage"]["replans"] == 1
        assert await session.scalar(select(func.count()).select_from(Artifact)) == 1
        assert await session.scalar(select(func.count()).select_from(Evidence)) == 3
        assert task.state["usage"]["tool_calls"] == 3


async def test_only_one_worker_can_claim_task(database, settings):
    import asyncio

    tid = await new_task(database, settings)
    first, second = (
        AgentRuntime(database, settings, ProtocolModel()),
        AgentRuntime(database, settings, ProtocolModel()),
    )
    results = await asyncio.gather(first.claim(tid), second.claim(tid))
    assert sum(results) == 1


async def test_missing_model_blocks_without_fabricated_plan(database, settings):
    tid = await new_task(database, settings)
    await AgentRuntime(database, settings).run(tid)
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        assert task.status == "blocked"
        assert task.state["plan"] is None
        assert not task.state["observations"]


async def test_budget_is_not_reset_when_resumed(database, settings):
    tid = await new_task(database, settings)
    async with database.sessions() as session, session.begin():
        task = await session.get(Task, tid)
        state = copy.deepcopy(task.state)
        state["usage"]["model_calls"] = state["budget"]["model_calls"]
        state["turn_usage"]["model_calls"] = state["budget"]["model_calls"]
        task.state = state
    await AgentRuntime(database, settings, ProtocolModel()).run(tid)
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        assert task.status == "partial"
        assert task.state["usage"]["model_calls"] == settings.max_model_calls


async def test_revision_fencing_rejects_stale_model_result(database, settings):
    tid = await new_task(database, settings)
    runtime = AgentRuntime(database, settings, ProtocolModel())
    assert await runtime.claim(tid)
    stale = await runtime.load(tid)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Task).where(Task.id == tid).values(revision=stale.revision + 1, status="cancelled")
        )
    with pytest.raises(LeaseLost):
        await runtime.commit(stale, copy.deepcopy(stale.state), "bad", {})
    async with database.sessions() as session:
        assert (await session.get(Task, tid)).status == "cancelled"


async def test_resume_from_pending_tool_after_lease_loss(database, settings):
    tid = await new_task(database, settings)
    first = AgentRuntime(database, settings, ProtocolModel())
    assert await first.claim(tid)
    # 在工具动作持久化后、执行前中断，再由另一个执行器恢复。
    await first.advance(await first.load(tid))
    await first.advance(await first.load(tid))
    async with database.sessions() as session, session.begin():
        task = await session.get(Task, tid)
        assert task.state["phase"] == "tool"
        task.lease_until = 0
    await AgentRuntime(database, settings, ProtocolModel()).run(tid)
    async with database.sessions() as session:
        assert (await session.get(Task, tid)).status == "completed"
        assert await session.scalar(select(func.count()).select_from(Artifact)) == 1


async def test_fabricated_completion_rejected(database, settings):
    class Liar(ProtocolModel):
        async def decide(self, phase, context):
            if phase == "evaluate":
                return Decision(
                    kind="finish",
                    reason="claim",
                    answer="done",
                    assessments=[
                        Assessment(
                            criterion_id="c1", satisfied=True, evidence_ids=["ev_nonexistent"], note="fake"
                        )
                    ],
                ), {}
            return await super().decide(phase, context)

    tid = await new_task(database, settings)
    await AgentRuntime(database, settings, Liar()).run(tid)
    async with database.sessions() as session:
        assert (await session.get(Task, tid)).status == "failed"


@pytest.mark.parametrize("expr", ["__import__('os')", "x.__class__", "2**99999", "1/0"])
def test_calculator_rejects_unsafe_or_invalid_expressions(expr):
    with pytest.raises((ValueError, SyntaxError)):
        calculate(expr, {})


def test_decimal_calculation_and_plan_validation():
    assert calculate("a+b", {"a": "0.1", "b": "0.2"}) == "0.3"
    with pytest.raises(ValueError):
        Plan(
            summary="bad",
            criteria=[Criterion(id="c", description="goal")],
            change_reason="initial",
            steps=[
                Step(id="a", objective="x", done_when="y", depends_on=["b"]),
                Step(id="b", objective="x", done_when="y", depends_on=["a"]),
            ],
        )


async def test_active_deadline_bounds_inflight_model_call(database, settings):
    import asyncio

    class SlowModel:
        async def decide(self, phase, context):
            await asyncio.sleep(5)
            raise AssertionError("Model timeout must interrupt this call")

    tid = await new_task(database, settings)
    async with database.sessions() as session, session.begin():
        task = await session.get(Task, tid)
        state = copy.deepcopy(task.state)
        state["budget"]["active_seconds"] = 0.05
        task.state = state
    await asyncio.wait_for(AgentRuntime(database, settings, SlowModel()).run(tid), 3)
    async with database.sessions() as session:
        task = await session.get(Task, tid)
        assert task.status == "partial"
        assert task.state["usage"]["active_seconds"] < 3
