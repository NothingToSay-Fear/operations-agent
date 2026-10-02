from sqlalchemy import select

from app.agent.contracts import Action, Assessment, Criterion, Decision, Plan, Step
from app.agent.runtime import AgentRuntime, initial_state
from app.analytics import PromotionSnapshotQuery, build_promotion_snapshot
from app.models import Artifact, Evidence, Task


async def _promotion_task(database, settings):
    goal = "结合现有商品成本、库存与活动规则，制定未来一周的促销建议，说明参与商品、折扣、毛利约束和执行前需要验证的信息，保存一份方案。"
    async with database.sessions() as session:
        task = Task(user_id="test-user", goal=goal, state=initial_state(goal, settings))
        session.add(task)
        await session.commit()
        return task.id


async def test_promotion_snapshot_aggregates_full_campaign_data(database):
    async with database.read_sessions() as commerce:
        snapshot = await build_promotion_snapshot(commerce, PromotionSnapshotQuery())

    summary = snapshot["promotion_summary"]
    assert snapshot["campaign"]["id"] == "CAM-02"
    assert snapshot["campaign"]["source"] == "structured_campaign_configuration"
    assert summary["candidate_sku_count"] == 4
    assert summary["recommended_sku_count"] + sum(summary["excluded_counts"].values()) == 4
    assert len(snapshot["rows"]) == 4
    assert all("estimated_margin_rate" in row and "coverage_days" in row for row in snapshot["rows"])
    assert summary["verification_items"]


async def test_promotion_fast_path_aggregates_saves_and_finishes(database, settings):
    phases = []

    class PromotionModel:
        async def decide(self, phase, context):
            phases.append(phase)
            if phase == "plan":
                return Plan(
                    summary="形成可执行的未来一周促销方案",
                    criteria=[
                        Criterion(id="recommendation", description="给出有成本和库存依据的促销建议"),
                        Criterion(id="artifact", description="保存可追溯的促销方案"),
                    ],
                    steps=[
                        Step(
                            id="promotion",
                            objective="核对活动规则、毛利与库存后形成方案",
                            done_when="生成有证据的促销建议并保存方案",
                        )
                    ],
                    change_reason="初始规划",
                ), {}
            if phase == "execute":
                fast_path = context["promotion_fast_path"]
                assert fast_path["status"] == "snapshot_ready"
                assert [tool["name"] for tool in context["tools"]] == ["save_artifact"]
                evidence_id = fast_path["evidence_id"]
                return Action(
                    kind="tool",
                    tool="save_artifact",
                    arguments={
                        "title": "未来一周促销方案",
                        "content": (
                            "# 未来一周促销方案\n\n"
                            f"依据促销决策摘要 [证据](evidence:{evidence_id}) 制定。\n\n"
                            "执行前确认实时库存、活动叠加规则和供应交期。"
                        ),
                        "evidence_ids": [evidence_id],
                    },
                    summary="保存促销方案",
                ), {}
            fast_path = context["promotion_fast_path"]
            artifact_id = context["artifacts"][0]["artifact_id"]
            evidence_id = fast_path["evidence_id"]
            return Decision(
                kind="finish",
                reason="已完成聚合计算、方案保存和证据核对",
                answer=(
                    "促销方案已生成，建议基于摘要中的商品、折扣、毛利和库存约束执行；"
                    f"详见 [证据](evidence:{evidence_id}) 与 [成果](artifact:{artifact_id})。"
                ),
                assessments=[
                    Assessment(
                        criterion_id="recommendation",
                        satisfied=True,
                        evidence_ids=[evidence_id],
                        note="服务端已完成全量参与 SKU 的成本、毛利和库存计算。",
                    ),
                    Assessment(
                        criterion_id="artifact",
                        satisfied=True,
                        evidence_ids=[evidence_id],
                        artifact_ids=[artifact_id],
                        note="促销方案已保存并关联聚合证据。",
                    ),
                ],
            ), {}

    task_id = await _promotion_task(database, settings)
    await AgentRuntime(database, settings, PromotionModel()).run(task_id)

    async with database.sessions() as session:
        task = await session.get(Task, task_id)
        assert task.status == "completed", task.state
        assert task.state["promotion_fast_path"]["status"] == "artifact_saved"
        assert task.state["usage"]["model_calls"] == 3
        assert task.state["usage"]["tool_calls"] == 2
        assert await session.scalar(select(Artifact.id).where(Artifact.task_id == task_id))
        tools = (await session.scalars(select(Evidence.tool).where(Evidence.task_id == task_id))).all()
        assert tools == ["build_promotion_snapshot", "save_artifact"]
    assert phases == ["plan", "execute", "evaluate"]
