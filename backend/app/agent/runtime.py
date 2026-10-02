"""可恢复的 Plan-and-Execute 运行时：以租约、版本和证据保证任务可审计推进。"""

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select, update

from app.agent.contracts import Action, Decision, Plan, SubtaskDecision, SubtaskSpec
from app.agent.model import ModelGateway, ModelUnavailable, PROMPT_VERSION, model_failure
from app.agent.tools import REGISTRY, catalog, invoke, supports_parallel
from app import access, auxiliary, memory, task_context
from app.extension_models import MemoryEvent
from app.models import Evidence, Run, Task, TaskEvent, uid

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "partial", "blocked", "failed", "cancelled"}
SUBTASK_TOOLS = {
    "inspect_data_capabilities",
    "get_metric_definitions",
    "query_metrics",
    "compare_metrics",
    "get_products",
    "query_order_facts",
    "query_inventory",
    "query_marketing",
    "search_knowledge",
    "read_document",
    "read_evidence",
    "calculate",
    "search_metric_definitions",
}


def initial_state(goal, settings):
    # 任务状态只保存可恢复的业务事实；模型上下文在调用前按需组装。
    return dict(
        phase="plan",
        messages=[{"role": "user", "content": goal}],
        plan=None,
        plans=[],
        steps={},
        observations=[],
        artifacts=[],
        seq=0,
        pending=None,
        answer="",
        waiting_question="",
        usage={
            "model_calls": 0,
            "tool_calls": 0,
            "replans": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "active_seconds": 0,
        },
        turn_usage={
            "model_calls": 0,
            "tool_calls": 0,
            "replans": 0,
            "active_seconds": 0,
        },
        turn_number=1,
        budget={
            "model_calls": settings.max_model_calls,
            "tool_calls": settings.max_tool_calls,
            "replans": settings.max_replans,
            "active_seconds": settings.max_active_seconds,
        },
        feedback="",
        invalid_outputs=0,
        step_tools={},
        fingerprints={},
        subtasks={},
        promotion_fast_path=None,
        constraint_version=1,
    )


def compact(value, maximum=4500):
    current = value
    observation = {}
    while isinstance(current, dict):
        if "evidence_id" in current:
            observation = {
                key: current[key]
                for key in (
                    "evidence_id",
                    "step_id",
                    "plan_version",
                    "tool",
                    "arguments",
                    "status",
                    "error_code",
                    "constraint_version",
                    "subtask_id",
                )
                if key in current
            }
        summary = current.get("risk_summary")
        if isinstance(summary, dict):
            rows = list(summary.get("rows") or [])
            compact_summary = {**summary, "rows": rows}
            result = {
                key: current[key]
                for key in ("as_of", "demand_days", "total_positions", "truncated", "warnings")
                if key in current
            }
            while rows and len(json.dumps({**result, "risk_summary": compact_summary}, ensure_ascii=False, default=str)) > maximum:
                rows.pop()
                compact_summary = {
                    **summary,
                    "rows": rows,
                    "shown_positions": len(rows),
                    "truncated": True,
                }
            compacted = {
                **result,
                "risk_summary": compact_summary,
                "instruction": "库存风险摘要已包含最高优先级仓位；仅在用户要求完整清单时调用 read_evidence。",
            }
            return {**observation, "data": compacted} if observation else compacted
        promotion_summary = current.get("promotion_summary")
        if isinstance(promotion_summary, dict):
            products = list(promotion_summary.get("recommended_products") or [])
            compact_summary = {**promotion_summary, "recommended_products": products}
            result = {
                key: current[key]
                for key in ("as_of", "planning_window", "campaign", "truncated", "simulated", "currency")
                if key in current
            }
            while products and len(
                json.dumps({**result, "promotion_summary": compact_summary}, ensure_ascii=False, default=str)
            ) > maximum:
                products.pop()
                compact_summary = {
                    **promotion_summary,
                    "recommended_products": products,
                    "shown_sku_count": len(products),
                }
            compacted = {
                **result,
                "promotion_summary": compact_summary,
                "instruction": "促销决策摘要已由服务端完成全量 SKU 的成本、库存和毛利计算；"
                "除非用户明确要求完整清单，不要读取原始明细或重新计算。",
            }
            return {**observation, "data": compacted} if observation else compacted
        current = current.get("data", current.get("result"))
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= maximum:
        return value
    references, current = [], value
    while isinstance(current, dict):
        references.extend(current.get("source_refs", []))
        current = current.get("data", current.get("result"))
    return {
        "excerpt": text[:maximum],
        "truncated": True,
        "source_refs": references,
        "instruction": "需要完整内容可调用 read_evidence",
    }


def compact_observations(rows, *, maximum_rows, maximum_chars):
    """按最近优先保留可审计观测，避免工具原始结果挤占主 Agent 上下文。"""
    selected = []
    for row in reversed(rows[-maximum_rows:]):
        data = row.get("data") if isinstance(row, dict) else None
        risk_summary = data.get("risk_summary") if isinstance(data, dict) else None
        promotion_summary = data.get("promotion_summary") if isinstance(data, dict) else None
        item_budget = (
            min(4800, maximum_chars)
            if isinstance(risk_summary, dict) or isinstance(promotion_summary, dict)
            else min(2200, max(700, maximum_chars // 2))
        )
        item = compact(row, maximum=item_budget)
        candidate = [item, *selected]
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) > maximum_chars:
            continue
        selected = candidate
    if not selected and rows:
        selected = [compact(rows[-1], maximum=max(400, maximum_chars - 100))]
    return selected


class LeaseLost(Exception):
    pass


class ContextChanged(LeaseLost):
    pass


class GraphState(TypedDict):
    task_id: str
    route: str


class AgentRuntime:
    def __init__(self, database, settings, model=None):
        self.db = database
        self.settings = settings
        self.model = model or ModelGateway(settings)
        self.owner = uid()
        builder = StateGraph(GraphState)
        builder.add_node("guard", self._guard_node)
        builder.add_node("plan", self._plan_node)
        builder.add_node("execute", self._execute_node)
        builder.add_node("tools", self._tools_node)
        builder.add_node("subtasks", self._subtasks_node)
        builder.add_node("evaluate", self._evaluate_node)
        builder.add_edge(START, "guard")
        builder.add_conditional_edges(
            "guard",
            lambda state: state["route"],
            {
                "plan": "plan",
                "execute": "execute",
                "tool": "tools",
                "subtasks": "subtasks",
                "evaluate": "evaluate",
                "end": END,
            },
        )
        for node in ("plan", "execute", "tools", "subtasks", "evaluate"):
            builder.add_edge(node, "guard")
        # 数据库状态是唯一持久化检查点，不另建一份可能冲突的运行状态。
        self.graph = builder.compile()

    async def claim(self, task_id):
        now = time.time()
        async with self.db.sessions() as session, session.begin():
            task = await session.get(Task, task_id)
            if task is None or task.status not in {"queued", "running"} or task.lease_until > now:
                return False
            result = await session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.revision == task.revision,
                    Task.lease_until <= now,
                    Task.status.in_(["queued", "running"]),
                )
                .values(
                    status="running",
                    lease_owner=self.owner,
                    lease_until=now + self.settings.lease_seconds,
                    revision=task.revision + 1,
                    updated_at=now,
                )
            )
            if not result.rowcount:
                return False
            await session.execute(
                update(Run)
                .where(Run.task_id == task_id, Run.status == "running")
                .values(status="interrupted", ended_at=now)
            )
            session.add(
                Run(
                    task_id=task_id,
                    configuration={
                        "provider": self.settings.llm_provider,
                        "model": self.settings.llm_model,
                        "prompt_version": PROMPT_VERSION,
                        "tools_version": "2.0",
                        "runtime": "langgraph",
                    },
                )
            )
        return True

    async def heartbeat(self, task_id):
        while True:
            await asyncio.sleep(max(1, self.settings.lease_seconds / 3))
            async with self.db.sessions() as session, session.begin():
                result = await session.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.lease_owner == self.owner, Task.status == "running")
                    .values(lease_until=time.time() + self.settings.lease_seconds)
                )
                if not result.rowcount:
                    return

    async def run(self, task_id):
        if not await self.claim(task_id):
            return
        heartbeat = asyncio.create_task(self.heartbeat(task_id))
        try:
            await self.graph.ainvoke({"task_id": task_id, "route": "plan"}, {"recursion_limit": 150})
        except LeaseLost:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent runtime failed: task_id=%s", task_id)
            try:
                task = await self.load(task_id)
                state = copy.deepcopy(task.state)
                state["answer"] = "执行发生内部错误，已保留计划与证据，可在排查后继续。"
                await self.commit(task, state, "error", {"message": state["answer"]}, status="failed")
            except LeaseLost:
                pass
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            async with self.db.sessions() as session, session.begin():
                task = await session.get(Task, task_id)
                if task and task.lease_owner == self.owner:
                    await session.execute(
                        update(Task)
                        .where(Task.id == task_id, Task.lease_owner == self.owner)
                        .values(lease_until=0, lease_owner=None)
                    )
                    await session.execute(
                        update(Run)
                        .where(Run.task_id == task_id, Run.status == "running")
                        .values(status=task.status, ended_at=time.time())
                    )

    async def load(self, task_id):
        async with self.db.sessions() as session:
            task = await session.get(Task, task_id)
            if (
                task is None
                or task.status != "running"
                or task.lease_owner != self.owner
                or task.lease_until < time.time()
            ):
                raise LeaseLost()
            return task

    async def commit(
        self, task, state, kind, payload, *, status="running", evidence=None, evidences=None, session=None
    ):
        owned_session = session is None
        session = session or self.db.sessions()
        try:
            revision = await access.scope_revision(session, task.user_id, lock=True)
            expected = getattr(task, "_scope_revision", task.state.get("context_scope_revision"))
            if not getattr(task, "_skip_scope_checks", False) and expected is not None:
                if revision != expected or not await access.memories_valid(
                    session, task.user_id, state.get("used_memories", [])
                ):
                    raise ContextChanged()
            state["seq"] = task.state.get("seq", 0) + 1
            now = time.time()
            baseline = task.state["usage"].copy()
            expected_revision = task.revision
            current = await session.scalar(
                select(Task)
                .where(Task.id == task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current is None or current.revision != expected_revision:
                raise LeaseLost()
            # 后台只追加维护用量；业务版本仍由用户控制和前台决策递增。
            for key in ("model_calls", "input_tokens", "output_tokens"):
                state["usage"][key] += max(0, current.state["usage"].get(key, 0) - baseline.get(key, 0))
            if (
                kind == "model_started"
                and state["turn_usage"]["model_calls"] > state["budget"]["model_calls"]
            ):
                # 维护任务先占用了最后一次额度，重新加载状态后按原预算停止。
                raise LeaseLost()
            a, b = self.settings.input_price_per_million, self.settings.output_price_per_million
            state["usage"]["estimated_cost"] = (
                (state["usage"]["input_tokens"] * a + state["usage"]["output_tokens"] * b) / 1e6
                if a is not None and b is not None
                else None
            )
            result = await session.execute(
                update(Task)
                .where(
                    Task.id == task.id,
                    Task.revision == expected_revision,
                    Task.lease_owner == self.owner,
                    Task.lease_until >= now,
                    Task.status == "running",
                )
                .values(state=state, status=status, revision=expected_revision + 1, updated_at=now)
            )
            if not result.rowcount:
                await session.rollback()
                raise LeaseLost()
            session.add(
                TaskEvent(
                    task_id=task.id,
                    seq=state["seq"],
                    kind=kind,
                    payload={**payload, "scope_revision": revision},
                )
            )
            assistant_content = ""
            assistant_kind = "answer"
            if kind == "evaluation" and payload.get("kind") in {"finish", "stop", "ask_user"}:
                assistant_content = str(payload.get("answer") or payload.get("reason") or "").strip()
                assistant_kind = "question" if payload.get("kind") == "ask_user" else "answer"
            elif kind == "action" and payload.get("kind") == "ask_user":
                assistant_content = str(payload.get("summary") or "").strip()
                assistant_kind = "question"
            elif kind in {"error", "blocked", "budget_exhausted", "model_error"}:
                assistant_content = str(payload.get("message") or "").strip()
                assistant_kind = "notice"
            elif kind == "decision_invalid" and status == "failed":
                assistant_content = str(state.get("answer") or payload.get("message") or "").strip()
                assistant_kind = "notice"
            history_payload = dict(payload)
            if assistant_content:
                task_message = await task_context.record_assistant_message(
                    session,
                    task,
                    state["seq"],
                    assistant_content,
                    self.settings,
                    kind=assistant_kind,
                    scope_revision=revision,
                )
                history_payload["message_ids"] = [task_message.id]
            if kind in {"plan_updated", "action", "evaluation"}:
                for stamp in state.get("used_memories", []):
                    session.add(
                        MemoryEvent(
                            user_id=task.user_id,
                            memory_id=stamp["id"],
                            task_id=task.id,
                            action="used",
                            detail={
                                "version": stamp["version"],
                                "reason": stamp.get("reason", ""),
                                "seq": state["seq"],
                            },
                        )
                    )
            evidence_rows = ([evidence] if evidence else []) + list(evidences or [])
            for evidence_row in evidence_rows:
                if not await access.references_allowed(session, task.user_id, evidence_row.result):
                    raise ContextChanged()
                session.add(evidence_row)
            await memory.append_history(session, task, state["seq"], kind, history_payload, self.settings)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            if owned_session:
                await session.close()

    async def _guard_node(self, graph_state):
        async with self.db.sessions() as session:
            task = await session.get(Task, graph_state["task_id"])
        if task is None or task.status != "running":
            return {"task_id": graph_state["task_id"], "route": "end"}
        if task.lease_owner != self.owner or task.lease_until < time.time():
            raise LeaseLost()
        async with self.db.sessions() as session:
            revision = await access.scope_revision(session, task.user_id)
            valid = await access.memories_valid(session, task.user_id, task.state.get("used_memories", []))
            if not valid:
                # 自然到期也使旧摘要和历史生成内容失效，防止偏好经压缩后继续生效。
                await access.bump_scope(session, [task.user_id])
                revision = await access.scope_revision(session, task.user_id)
            await session.commit()
        if task.state.get("context_scope_revision", revision) != revision or not valid:
            state = copy.deepcopy(task.state)
            state.update(
                context_scope_revision=revision,
                used_memories=[],
                pending=None,
                phase="plan",
                plan=None,
                steps={},
                step_tools={},
                subtasks={},
                answer="",
                feedback="资料权限、版本或长期记忆已变化，请重新核对当前约束和证据。",
                finalizing=False,
            )
            state["constraint_version"] += 1
            state["observations"] = []
            task._skip_scope_checks = True
            await self.commit(task, state, "context_changed", {"message": state["feedback"]})
            return {"task_id": task.id, "route": "plan"}
        if not await self.prepare(task):
            return {"task_id": task.id, "route": "end"}
        current = await self.load(task.id)
        return {"task_id": task.id, "route": current.state["phase"]}

    async def _plan_node(self, graph_state):
        await self.decide(await self.load(graph_state["task_id"]), "plan")
        return graph_state

    async def _execute_node(self, graph_state):
        await self.decide(await self.load(graph_state["task_id"]), "execute")
        return graph_state

    async def _tools_node(self, graph_state):
        await self.execute_tool(await self.load(graph_state["task_id"]))
        return graph_state

    async def _subtasks_node(self, graph_state):
        await self.execute_subtasks(await self.load(graph_state["task_id"]))
        return graph_state

    async def _evaluate_node(self, graph_state):
        await self.decide(await self.load(graph_state["task_id"]), "evaluate")
        return graph_state

    def current_step(self, state):
        if not state.get("plan"):
            return None
        for step in state["plan"]["steps"]:
            if state["steps"].get(step["id"], {}).get("status") == "succeeded":
                continue
            if all(state["steps"].get(dep, {}).get("status") == "succeeded" for dep in step["depends_on"]):
                return step
        return None

    def context(self, task):
        # 统一从任务、记忆和已固化证据构建上下文，各阶段共享同一版本边界。
        s = task.state
        phase = s["phase"]
        promotion_fast_path = s.get("promotion_fast_path")
        promotion_save_only = isinstance(promotion_fast_path, dict) and promotion_fast_path.get("status") == "snapshot_ready"
        summaries = [
            {
                "id": item["id"],
                "role": item["role"],
                "objective": item["objective"],
                "status": item["status"],
                "findings": item.get("findings", []),
                "limitations": item.get("limitations", []),
                "evidence_ids": item.get("evidence_ids", []),
            }
            for item in s.get("subtasks", {}).values()
            if item.get("constraint_version") == s["constraint_version"]
        ]
        return {
            "goal": task.goal,
            "plan": s["plan"],
            "step_results": s["steps"],
            "current_step": self.current_step(s),
            "observations": compact_observations(
                s["observations"],
                maximum_rows=self.settings.main_context_observation_limit,
                maximum_chars=self.settings.main_context_observation_char_budget,
            ),
            "subtask_results": summaries,
            "artifacts": s["artifacts"],
            "remaining_budget": {key: s["budget"][key] - s["turn_usage"].get(key, 0) for key in s["budget"]},
            "feedback": s.get("feedback", ""),
            "inventory_risk_fast_path": s.get("inventory_risk_fast_path"),
            "promotion_fast_path": promotion_fast_path,
            "tools": catalog(
                names=["save_artifact"] if promotion_save_only else None,
                include_schema=phase == "execute",
            ),
            "constraint_version": s["constraint_version"],
        }

    @staticmethod
    def _inventory_fast_path_allowed(task):
        goal = task.goal.lower()
        asks_complete_list = any(token in goal for token in ("全部", "全量", "所有", "完整清单", "导出"))
        inventory_intent = any(token in goal for token in ("缺货", "补货", "库存风险"))
        return inventory_intent and not asks_complete_list

    @staticmethod
    def _promotion_fast_path_allowed(task):
        """只将同时涉及活动、毛利和库存的促销决策路由到领域聚合工具。"""

        goal = task.goal.lower()
        asks_complete_list = any(token in goal for token in ("全部", "全量", "所有", "完整清单", "导出"))
        has_activity = any(token in goal for token in ("促销", "活动", "折扣", "优惠"))
        has_economics = any(token in goal for token in ("成本", "毛利", "利润"))
        has_inventory = any(token in goal for token in ("库存", "在途", "交期"))
        return has_activity and has_economics and has_inventory and not asks_complete_list

    @staticmethod
    def _promotion_artifact_requested(task):
        return any(token in task.goal.lower() for token in ("保存", "方案", "报告", "文档"))

    async def dispatch_promotion_fast_path(self, task):
        """为促销组合决策直接安排一次服务端聚合，阻止模型进入长表分页循环。"""

        state = copy.deepcopy(task.state)
        if (
            state["phase"] != "execute"
            or state.get("promotion_fast_path")
            or not self._promotion_fast_path_allowed(task)
        ):
            return False
        step = self.current_step(state)
        remaining_tools = state["budget"]["tool_calls"] - state["turn_usage"]["tool_calls"]
        if (
            step is None
            or remaining_tools < 1
            or state["step_tools"].get(step["id"], 0) >= self.settings.max_step_tools
        ):
            return False
        call_id = uid()
        state["promotion_fast_path"] = {
            "status": "dispatched",
            "requires_artifact": self._promotion_artifact_requested(task),
            "step_id": step["id"],
        }
        state["pending"] = {
            "kind": "tool",
            "tool": "build_promotion_snapshot",
            "arguments": {},
            "summary": "汇总活动参与商品、成本、毛利与库存约束",
            "id": call_id,
            "step_id": step["id"],
        }
        state["phase"] = "tool"
        state["feedback"] = "已识别为促销组合决策，先由服务端完成全量 SKU 聚合计算。"
        await self.commit(
            task,
            state,
            "promotion_fast_path_dispatched",
            {"step_id": step["id"], "requires_artifact": state["promotion_fast_path"]["requires_artifact"]},
        )
        return True

    def _activate_inventory_fast_path(self, task, state, observations):
        if not self._inventory_fast_path_allowed(task):
            return False
        for observation in reversed(observations):
            if observation.get("status") != "success" or observation.get("tool") != "query_inventory":
                continue
            summary = observation.get("data", {}).get("risk_summary")
            if not isinstance(summary, dict):
                continue
            state["inventory_risk_fast_path"] = {
                "evidence_id": observation["evidence_id"],
                "risk_positions": summary.get("risk_positions"),
                "shown_positions": summary.get("shown_positions"),
                "horizon_end": summary.get("horizon_end"),
            }
            state["phase"] = "evaluate"
            state["feedback"] = (
                "库存风险摘要已按全量仓位计算并含需求、可靠在途、交期、MOQ 与建议量。"
                "当前用户未要求完整导出清单，直接基于该证据完成优先级建议，不再扩展查询、分页或重规划。"
            )
            return True
        return False

    def _advance_promotion_fast_path(self, state, observations):
        """将聚合证据、方案保存和最终评估串成受控的三段式收尾流程。"""

        fast_path = state.get("promotion_fast_path")
        if not isinstance(fast_path, dict):
            return False
        for observation in observations:
            if (
                fast_path.get("status") == "dispatched"
                and observation.get("status") == "success"
                and observation.get("tool") == "build_promotion_snapshot"
                and isinstance(observation.get("data", {}).get("promotion_summary"), dict)
            ):
                fast_path.update(status="snapshot_ready", evidence_id=observation["evidence_id"])
                if fast_path.get("requires_artifact"):
                    state["phase"] = "execute"
                    state["feedback"] = (
                        "促销决策摘要已完成。用户要求保存方案；下一步必须仅调用 save_artifact，"
                        "并引用该摘要证据，不得读取活动明细、重规划或发起其他查询。"
                    )
                else:
                    state["phase"] = "evaluate"
                    state["feedback"] = "促销决策摘要已完成，请仅基于该证据完成最终评估。"
                return True
            if (
                fast_path.get("status") == "snapshot_ready"
                and observation.get("status") == "success"
                and observation.get("tool") == "save_artifact"
            ):
                fast_path.update(status="artifact_saved", artifact_id=observation.get("data", {}).get("artifact_id"))
                state["phase"] = "evaluate"
                state["feedback"] = "促销方案已保存，请基于聚合证据和已保存成果完成最终评估。"
                return True
        return False

    async def reserve(self, task, kind):
        state = copy.deepcopy(task.state)
        now = time.time()
        if state.get("active_started_at"):
            elapsed = min(now - state["active_started_at"], self.settings.lease_seconds)
            state["usage"]["active_seconds"] += elapsed
            state["turn_usage"]["active_seconds"] += elapsed
        state["active_started_at"] = now
        usage, turn_usage, budget = state["usage"], state["turn_usage"], state["budget"]
        if (
            turn_usage.get(kind, 0) >= budget[kind]
            or turn_usage["active_seconds"] >= budget["active_seconds"]
        ):
            state["answer"] = "已达到本轮执行预算。已保存现有证据和成果，未完成的工作保留在计划中。"
            state.pop("active_started_at", None)
            await self.commit(task, state, "budget_exhausted", {"message": state["answer"]}, status="partial")
            return None
        usage[kind] += 1
        turn_usage[kind] += 1
        pending = state.get("pending") or {}
        tool_name = pending.get("tool")
        if pending.get("kind") == "batch":
            tool_name = [call["tool"] for call in pending.get("calls", [])]
        await self.commit(
            task,
            state,
            "model_started" if kind == "model_calls" else "tool_started",
            {
                "phase": state["phase"],
                "call": turn_usage[kind],
                "turn": state["turn_number"],
                "tool": tool_name,
            },
        )
        return await self.load(task.id)

    async def reserve_many(self, task, kind, count, *, phase, subtask_ids):
        """为并发子 Agent 波次一次性预留共享预算，避免并发提交相互覆盖。"""
        if count < 1:
            return task
        state = copy.deepcopy(task.state)
        now = time.time()
        if state.get("active_started_at"):
            elapsed = min(now - state["active_started_at"], self.settings.lease_seconds)
            state["usage"]["active_seconds"] += elapsed
            state["turn_usage"]["active_seconds"] += elapsed
        state["active_started_at"] = now
        budget = state["budget"]
        if (
            state["turn_usage"].get(kind, 0) + count > budget[kind]
            or state["turn_usage"]["active_seconds"] >= budget["active_seconds"]
        ):
            state["answer"] = "已达到本轮执行预算。已保存现有证据和成果，未完成的工作保留在计划中。"
            state.pop("active_started_at", None)
            await self.commit(task, state, "budget_exhausted", {"message": state["answer"]}, status="partial")
            return None
        state["usage"][kind] += count
        state["turn_usage"][kind] += count
        await self.commit(
            task,
            state,
            "model_started" if kind == "model_calls" else "tool_started",
            {
                "phase": phase,
                "count": count,
                "call": state["turn_usage"][kind],
                "turn": state["turn_number"],
                "subtask_ids": subtask_ids,
            },
        )
        return await self.load(task.id)

    def account(self, state, usage=None):
        elapsed = max(0, time.time() - state.pop("active_started_at", time.time()))
        state["usage"]["active_seconds"] += elapsed
        state["turn_usage"]["active_seconds"] += elapsed
        for key in ["input_tokens", "output_tokens"]:
            state["usage"][key] += (usage or {}).get(key, 0)
        a, b = self.settings.input_price_per_million, self.settings.output_price_per_million
        state["usage"]["estimated_cost"] = (
            (state["usage"]["input_tokens"] * a + state["usage"]["output_tokens"] * b) / 1e6
            if a is not None and b is not None
            else None
        )

    async def prepare(self, task):
        state = task.state
        remaining = state["budget"]["model_calls"] - state["turn_usage"]["model_calls"]
        tools_exhausted = state["turn_usage"]["tool_calls"] >= state["budget"]["tool_calls"]
        if (
            state["phase"] != "subtasks"
            and state["observations"]
            and not state.get("finalizing")
            and (0 < remaining <= 2 or tools_exhausted)
        ):
            state = copy.deepcopy(state)
            state.update(
                phase="evaluate",
                pending=None,
                finalizing=True,
                feedback="剩余额度用于最终交付。根据现有证据核对原始用户目标；足够则 finish，不足则 stop 并给出可用结论和明确缺口。不要继续查询或扩展计划。",
            )
            await self.commit(task, state, "finalizing", {"message": "正在根据已有证据完成最终交付"})
            return True
        if state["phase"] == "execute" and not self.current_step(state):
            state = copy.deepcopy(state)
            state["phase"] = "evaluate"
            await self.commit(task, state, "evaluating", {"message": "正在检查任务完成条件"})
            return True
        if state["phase"] == "execute":
            dispatched = await self.dispatch_promotion_fast_path(task)
            if dispatched:
                return True
            dispatched = await self.dispatch_explicit_multidomain_work(task)
            if dispatched:
                return True
        return True

    async def advance(self, task):
        """兼容单步执行测试；正式运行由显式LangGraph节点路由。"""
        if not await self.prepare(task):
            return False
        task = await self.load(task.id)
        phase = task.state["phase"]
        if phase == "tool":
            return await self.execute_tool(task)
        return await self.decide(task, phase)

    async def decide(self, task, phase):
        # 每次决策都基于最新持久化状态，避免租约切换后使用过期内存。
        if task.state["phase"] != phase:
            raise LeaseLost()
        task = await self.reserve(task, "model_calls")
        if not task:
            return False
        state = copy.deepcopy(task.state)
        phase = state["phase"]
        try:
            async with self.db.sessions() as session:
                built = await memory.build_context(session, task, self.settings)
                model_observations = compact_observations(
                    built.get("observations", []),
                    maximum_rows=self.settings.main_context_observation_limit,
                    maximum_chars=self.settings.main_context_observation_char_budget,
                )
                context = {
                    **self.context(task),
                    **{
                        key: value
                        for key, value in built.items()
                        if key not in {"context_manifest", "observations"}
                    },
                    "observations": model_observations,
                }
                snapshot = await task_context.persist_context_snapshot(
                    session,
                    task,
                    state["usage"]["model_calls"],
                    phase,
                    {
                        **built["context_manifest"],
                        "context_chars": len(json.dumps(context, ensure_ascii=False, default=str)),
                        "tool_count": len(context.get("tools", [])),
                        "observation_count": len(context.get("observations", [])),
                    },
                )
                await session.commit()
            state["last_context_snapshot_id"] = snapshot.id
            built.pop("context_manifest", None)
            task._scope_revision = built["scope_revision"]
            state["context_scope_revision"] = built["scope_revision"]
            state["effective_constraints"] = built["effective_constraints"]
            state["used_memories"] = [
                {"id": m["id"], "version": m["version"], "reason": m["reason"]}
                for m in built["long_term_memories"]
            ]
            remaining_seconds = state["budget"]["active_seconds"] - state["turn_usage"]["active_seconds"]
            decision, usage = await asyncio.wait_for(
                self.model.decide(phase, context),
                timeout=min(self.settings.llm_timeout, max(0.01, remaining_seconds)),
            )
            self.account(state, usage)
            state["invalid_outputs"] = 0
            state["feedback"] = ""
            if phase == "plan":
                return await self.apply_plan(task, state, Plan.model_validate(decision))
            if phase == "execute":
                return await self.apply_action(task, state, Action.model_validate(decision))
            return await self.apply_decision(task, state, Decision.model_validate(decision))
        except ModelUnavailable as exc:
            self.account(state)
            state["answer"] = str(exc)
            await self.commit(task, state, "blocked", {"message": str(exc)}, status="blocked")
            return False
        except LeaseLost:
            raise
        except (ValueError, TypeError) as exc:
            if "active_started_at" in state:
                self.account(state, getattr(exc, "usage", None))
            state["invalid_outputs"] = task.state.get("invalid_outputs", 0) + 1
            state["feedback"] = str(exc)[:1200]
            failed = state["invalid_outputs"] > 2
            if failed:
                state["answer"] = "模型连续返回无效决策，已停止并保留状态：" + state["feedback"]
            await self.commit(
                task,
                state,
                "decision_invalid",
                {"message": state["feedback"]},
                status="failed" if failed else "running",
            )
            return not failed
        except Exception as exc:
            if "active_started_at" in state:
                self.account(state)
            if state["turn_usage"]["active_seconds"] >= state["budget"]["active_seconds"]:
                state["answer"] = "已达到任务活跃执行时间预算，现有证据与成果已保留。"
                await self.commit(
                    task, state, "budget_exhausted", {"message": state["answer"]}, status="partial"
                )
                return False
            # 供应商异常可能包含凭据或请求正文，不直接写入用户可见记录。
            failure = model_failure(exc)
            state["answer"] = failure["message"]
            await self.commit(task, state, "model_error", failure, status="blocked")
            return False

    async def apply_plan(self, task, state, plan):
        # 计划只描述目标和完成条件；工具选择留给 Execute 阶段根据观察决定。
        if state["plan"]:
            if state["turn_usage"]["replans"] >= state["budget"]["replans"]:
                state["answer"] = "已达到重规划上限，保留现有成果和待完成步骤。"
                await self.commit(
                    task, state, "budget_exhausted", {"message": state["answer"]}, status="partial"
                )
                return False
            state["usage"]["replans"] += 1
            state["turn_usage"]["replans"] += 1
        old = {s["id"]: s for s in (state["plan"] or {}).get("steps", [])}
        statuses = state["steps"]
        if state["plans"]:
            state["plans"][-1]["execution_snapshot"] = copy.deepcopy(statuses)
        state["steps"] = {
            s.id: statuses[s.id]
            if s.id in statuses and old.get(s.id) == s.model_dump()
            else {"status": "pending"}
            for s in plan.steps
        }
        state["plan"] = plan.model_dump()
        state["step_tools"] = {
            s.id: state["step_tools"].get(s.id, 0) if old.get(s.id) == s.model_dump() else 0
            for s in plan.steps
        }
        state["plans"].append(
            {
                "version": len(state["plans"]) + 1,
                "plan": plan.model_dump(),
                "constraint_version": state["constraint_version"],
                "execution_snapshot": copy.deepcopy(state["steps"]),
                "at": time.time(),
            }
        )
        state["phase"] = "execute"
        await self.commit(task, state, "plan_updated", state["plans"][-1])
        return True

    def check_evidence(self, state, ids):
        allowed = {
            o["evidence_id"]
            for o in state["observations"]
            if o["status"] == "success" and o.get("constraint_version") == state["constraint_version"]
        }
        if not set(ids) <= allowed:
            raise ValueError("引用必须属于当前约束版本的成功证据，不能引用过期或失败观察")

    def queue_subtasks(self, state, step, specs):
        """校验并持久化子任务；模型分派和规则分派共用同一条安全边界。"""
        if not specs or len(specs) > self.settings.max_subtasks:
            raise ValueError("子任务数量不符合当前配置上限")
        remaining_tools = state["budget"]["tool_calls"] - state["turn_usage"]["tool_calls"]
        allocated_tools = sum(item.max_tool_calls for item in specs)
        used = state["step_tools"].get(step["id"], 0)
        if used + allocated_tools > self.settings.max_step_tools or allocated_tools > remaining_tools:
            raise ValueError("子任务工具预算超过当前步骤或任务限制")
        existing = state.setdefault("subtasks", {})
        for spec in specs:
            if spec.id in existing and existing[spec.id].get("status") not in {"failed", "partial"}:
                raise ValueError("子任务 ID 已存在，请使用新的 ID 或直接使用既有结果")
            if (
                not set(spec.allowed_tools) <= SUBTASK_TOOLS
                or not set(spec.allowed_tools) <= set(REGISTRY)
                or any(not supports_parallel(name) for name in spec.allowed_tools)
            ):
                raise ValueError("子任务只能使用已登记的只读工具")
            if spec.max_model_calls > self.settings.subtask_max_model_calls:
                raise ValueError("子任务模型调用预算超过系统限制")
            if spec.max_tool_calls > self.settings.subtask_max_tool_calls:
                raise ValueError("子任务工具调用预算超过系统限制")
            existing[spec.id] = {
                **spec.model_dump(),
                "parent_step_id": step["id"],
                "status": "queued",
                "observations": [],
                "evidence_ids": [],
                "findings": [],
                "limitations": [],
                "usage": {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
                "budget": {"model_calls": spec.max_model_calls, "tool_calls": spec.max_tool_calls},
                "constraint_version": state["constraint_version"],
                "created_at": time.time(),
            }
        state["phase"] = "subtasks"

    async def dispatch_explicit_multidomain_work(self, task):
        """对用户明确点名的独立维度强制分派，避免依赖模型是否主动输出 delegate。"""
        state = copy.deepcopy(task.state)
        step = self.current_step(state)
        dispatched_versions = set(state.get("auto_delegated_constraint_versions", []))
        if step is None or state["constraint_version"] in dispatched_versions:
            return False
        # 先完成数据口径确认，子 Agent 才能直接在同一数据边界内调查。
        if not any(row.get("tool") == "inspect_data_capabilities" for row in state["observations"]):
            return False
        async with self.db.sessions() as session:
            constraints, _ = await task_context.ensure_task_context(session, task, self.settings)
            dimensions = set((constraints.state.get("business_constraints") or {}).get("dimensions") or [])
            state["effective_constraints"] = constraints.state
            await session.commit()
        if not {"channel", "product"} <= dimensions:
            return False
        existing = [
            item
            for item in state.get("subtasks", {}).values()
            if item.get("constraint_version") == state["constraint_version"]
        ]
        if existing:
            return False
        specs = [
            SubtaskSpec(
                id=f"channel_{uid()[:8]}", role="channel",
                objective="在已确认的数据截止口径内，量化渠道对目标指标变化的贡献，返回可复核证据。",
                done_when="给出主要正负渠道贡献项、数值和证据 ID；无法确认时说明缺口。",
                allowed_tools=["get_metric_definitions", "compare_metrics", "query_metrics"],
                max_model_calls=min(4, self.settings.subtask_max_model_calls), max_tool_calls=min(3, self.settings.subtask_max_tool_calls),
            ),
            SubtaskSpec(
                id=f"product_{uid()[:8]}", role="product_inventory",
                objective="在已确认的数据截止口径内，量化商品/SKU 对目标指标变化的贡献，返回可复核证据。",
                done_when="给出主要正负商品贡献项、数值和证据 ID；无法确认时说明缺口。",
                allowed_tools=["get_metric_definitions", "compare_metrics", "query_metrics"],
                max_model_calls=min(4, self.settings.subtask_max_model_calls), max_tool_calls=min(3, self.settings.subtask_max_tool_calls),
            ),
        ]
        try:
            self.queue_subtasks(state, step, specs)
        except ValueError:
            return False
        state.setdefault("auto_delegated_constraint_versions", []).append(state["constraint_version"])
        state["subtask_completion_policy"] = "evaluate"
        state["feedback"] = "用户明确要求同时分析渠道和商品，已按独立只读维度分派子任务；主 Agent 将汇总并核验结果。"
        await self.commit(task, state, "subtasks_dispatched", {"mode": "explicit_multidomain", "step_id": step["id"], "subtasks": [spec.model_dump() for spec in specs]})
        return True

    async def apply_action(self, task, state, action):
        # Action 先经过预算、重复调用和证据依赖校验，再写入待执行工具队列。
        step = self.current_step(state)
        if step is None:
            raise ValueError("没有可执行步骤，应评估总目标")
        promotion_fast_path = state.get("promotion_fast_path")
        if isinstance(promotion_fast_path, dict) and promotion_fast_path.get("status") == "snapshot_ready":
            if action.kind != "tool" or action.tool != "save_artifact":
                raise ValueError("促销决策摘要已完成；用户要求保存方案时下一步必须调用 save_artifact")
            snapshot_id = promotion_fast_path.get("evidence_id")
            if snapshot_id not in action.arguments.get("evidence_ids", []):
                raise ValueError("保存促销方案必须引用促销决策摘要证据")
        self.check_evidence(state, action.evidence_ids)
        if action.kind in {"tool", "tools"}:
            calls = (
                [{"tool": action.tool, "arguments": action.arguments, "summary": action.summary}]
                if action.kind == "tool"
                else [call.model_dump() for call in action.tool_calls]
            )
            if action.kind == "tools" and any(not supports_parallel(call["tool"]) for call in calls):
                raise ValueError("批量动作只允许相互独立的只读工具；写入工具必须单独执行")
            used = state["step_tools"].get(step["id"], 0)
            remaining_tools = state["budget"]["tool_calls"] - state["turn_usage"]["tool_calls"]
            if used + len(calls) > self.settings.max_step_tools or len(calls) > remaining_tools:
                state["phase"] = "plan"
                state["feedback"] = (
                    "当前批次超过步骤或本轮工具预算，请缩小查询批次，并基于已有证据重新规划。"
                )
            else:
                if action.kind == "tool":
                    state["pending"] = {**action.model_dump(), "id": uid(), "step_id": step["id"]}
                else:
                    state["pending"] = {
                        "kind": "batch",
                        "summary": action.summary,
                        "step_id": step["id"],
                        "calls": [
                            {**call, "id": uid(), "step_id": step["id"]}
                            for call in calls
                        ],
                    }
                state["phase"] = "tool"
        elif action.kind == "delegate":
            self.queue_subtasks(state, step, action.subtasks)
        elif action.kind == "step_done":
            state["steps"][step["id"]] = {
                "status": "succeeded",
                "summary": action.summary,
                "evidence_ids": action.evidence_ids,
            }
            state["plans"][-1]["execution_snapshot"] = copy.deepcopy(state["steps"])
            # 中间步骤完成后直接进入下一个可执行步骤，只在计划结束时做全局评估。
            state["phase"] = "execute" if self.current_step(state) else "evaluate"
        elif action.kind == "replan":
            state["phase"] = "plan"
            state["feedback"] = action.summary
        else:
            state["waiting_question"] = action.summary
        await self.commit(
            task,
            state,
            "action",
            {**action.model_dump(), "step_id": step["id"], "plan_version": len(state["plans"])},
            status="waiting_user" if action.kind == "ask_user" else "running",
        )
        return action.kind != "ask_user"

    async def apply_decision(self, task, state, decision):
        # Evaluate 的结论只能推进、交付、追问或阻塞，不能绕过工具结果修改事实。
        if state.get("finalizing") and decision.kind in {"continue", "replan"}:
            raise ValueError(
                "当前预算只够收尾，请返回 finish 或 stop 并交付已有证据支持的结论；信息不足时明确说明缺口。"
            )
        if decision.answer:
            self.check_evidence(state, re.findall(r"evidence:(ev_[a-zA-Z0-9_-]+)", decision.answer))
            valid_artifacts = {
                a["artifact_id"]
                for a in state["artifacts"]
                if a.get("constraint_version") == state["constraint_version"]
            }
            if not set(re.findall(r"artifact:(ar_[a-zA-Z0-9_-]+)", decision.answer)) <= valid_artifacts:
                raise ValueError("最终回答引用了不存在或过期的成果")
        status = "running"
        if decision.kind == "continue":
            if not self.current_step(state):
                raise ValueError("没有待执行步骤，应finish、replan或stop")
            state["phase"] = "execute"
        elif decision.kind == "replan":
            state["phase"] = "plan"
            state["feedback"] = decision.reason
        elif decision.kind == "ask_user":
            status = "waiting_user"
            state["waiting_question"] = decision.answer or decision.reason
        elif decision.kind == "finish":
            expected = {c["id"] for c in state["plan"]["criteria"]}
            if {c.criterion_id for c in decision.assessments} != expected or len(decision.assessments) != len(
                expected
            ):
                raise ValueError("完成检查必须逐项覆盖所有成功标准")
            artifacts = {
                a["artifact_id"]
                for a in state["artifacts"]
                if a.get("constraint_version") == state["constraint_version"]
            }
            for assessment in decision.assessments:
                self.check_evidence(state, assessment.evidence_ids)
                if (
                    not assessment.satisfied
                    or not set(assessment.artifact_ids) <= artifacts
                    or not (assessment.evidence_ids or assessment.artifact_ids)
                ):
                    raise ValueError("尚有成功标准缺少真实有效的证据/成果，应继续或部分完成")
            if not decision.answer.strip():
                raise ValueError("完成时必须给出用户可读的最终交付")
            status = "completed"
            state["answer"] = decision.answer
            state["assessments"] = [a.model_dump() for a in decision.assessments]
            for step in state["plan"]["steps"]:
                if state["steps"].get(step["id"], {}).get("status") != "succeeded":
                    state["steps"][step["id"]] = {"status": "skipped", "summary": "目标已满足，无需继续执行"}
            state["plans"][-1]["execution_snapshot"] = copy.deepcopy(state["steps"])
        else:
            status = "partial" if state["observations"] or state["artifacts"] else "blocked"
            state["answer"] = decision.answer or decision.reason
        await self.commit(task, state, "evaluation", decision.model_dump(), status=status)
        return status == "running"

    @staticmethod
    def _subtask_terminal(subtask):
        return subtask.get("status") in {"succeeded", "partial", "failed"}

    def _subtask_evidence(self, subtask):
        return {
            row["evidence_id"]
            for row in subtask.get("observations", [])
            if row.get("status") == "success"
        }

    def _validate_subtask_decision(self, subtask, decision):
        calls = (
            [{"tool": decision.tool, "arguments": decision.arguments, "summary": decision.summary}]
            if decision.kind == "tool"
            else [call.model_dump() for call in decision.tool_calls]
        )
        if decision.kind in {"tool", "tools"}:
            if not set(call["tool"] for call in calls) <= set(subtask["allowed_tools"]):
                raise ValueError("子任务调用了未授权工具")
            if any(not supports_parallel(call["tool"]) for call in calls):
                raise ValueError("子任务只能调用只读工具")
            remaining = subtask["budget"]["tool_calls"] - subtask["usage"]["tool_calls"]
            if remaining < 1:
                raise ValueError("子任务工具预算不足")
            # 模型偶尔会在最后一轮一次请求多个工具；保留最靠前的独立调用，避免整项调查因超额批次失败。
            return calls[:remaining]
        allowed = self._subtask_evidence(subtask)
        cited = set(decision.evidence_ids) | {
            evidence_id for finding in decision.findings for evidence_id in finding.evidence_ids
        }
        if not cited <= allowed:
            raise ValueError("子任务只能引用自身成功工具返回的证据")
        if decision.kind == "finish" and decision.findings and not cited:
            raise ValueError("子任务发现必须关联自身证据")
        return []

    def _subtask_scope_capsule(self, state):
        """只向子任务传递已确认的数据范围与指标口径，不泄露父计划或其他业务结论。"""
        trusted_tools = {"inspect_data_capabilities", "get_metric_definitions"}
        rows = [
            {
                "tool": row["tool"],
                "result": compact(row.get("data", row.get("result", {})), maximum=1800),
            }
            for row in state["observations"]
            if row.get("status") == "success"
            and row.get("constraint_version") == state["constraint_version"]
            and row.get("tool") in trusted_tools
        ]
        return rows[-2:]

    def _subtask_context(self, built, subtask, shared_scope=None):
        """构造子任务专用上下文；收尾回合只保留其自身证据，避免示例或父任务证据越界。"""
        finish_only = subtask["usage"]["tool_calls"] >= subtask["budget"]["tool_calls"]
        available_evidence_ids = sorted(self._subtask_evidence(subtask))
        return {
            **{key: value for key, value in built.items() if key not in {"context_manifest", "scope_revision"}},
            "shared_scope": shared_scope or [],
            "observations": subtask.get("observations", []),
            "tools": [] if finish_only else catalog(names=subtask["allowed_tools"], include_schema=True),
            "subtask_remaining_budget": {
                "model_calls": subtask["budget"]["model_calls"] - subtask["usage"]["model_calls"],
                "tool_calls": subtask["budget"]["tool_calls"] - subtask["usage"]["tool_calls"],
            },
            "finish_only": finish_only,
            "subtask_available_evidence_ids": available_evidence_ids,
            "subtask_result_contract": "返回局部发现、限制和证据 ID，不得输出面向用户的结论。",
        }

    async def execute_subtasks(self, task):
        # 子任务只接收最小共享范围，结果回收为摘要和证据编号。
        """并发推进同一主步骤的临时子 Agent；每个子 Agent 只看到自己的调查上下文。"""
        state = copy.deepcopy(task.state)
        active = [
            item
            for item in state.get("subtasks", {}).values()
            if item.get("constraint_version") == state["constraint_version"] and not self._subtask_terminal(item)
        ]
        if not active:
            state["phase"] = "execute"
            state["feedback"] = "子任务已返回局部发现，请核对证据并完成当前步骤。"
            await self.commit(task, state, "subtasks_completed", {"subtasks": []})
            return True

        budget_changed = False
        for item in active:
            if not item.get("pending") and item["usage"]["model_calls"] >= item["budget"]["model_calls"]:
                item["status"] = "partial"
                item["limitations"] = [
                    "子 Agent 已达到模型调用预算，主 Agent 将根据其已返回的证据独立汇总与核验。"
                ]
                item["finished_at"] = time.time()
                budget_changed = True
        ready = [
            item
            for item in state.get("subtasks", {}).values()
            if item.get("constraint_version") == state["constraint_version"]
            and not self._subtask_terminal(item)
            and not item.get("pending")
        ]
        if not ready and budget_changed:
            await self.commit(task, state, "subtask_budget_exhausted", {"message": "部分子任务达到自身预算"})
            task = await self.load(task.id)
            state = task.state
        if ready:
            ready = ready[: self.settings.max_subtasks]
            task = await self.reserve_many(
                task,
                "model_calls",
                len(ready),
                phase="subtask",
                subtask_ids=[item["id"] for item in ready],
            )
            if not task:
                return False
            state = copy.deepcopy(task.state)
            contexts = []
            try:
                async with self.db.sessions() as session:
                    start_call = state["usage"]["model_calls"] - len(ready) + 1
                    for offset, item in enumerate(ready):
                        current = state["subtasks"][item["id"]]
                        built = await memory.build_subtask_context(session, task, current, self.settings)
                        context = self._subtask_context(
                            built, current, self._subtask_scope_capsule(state)
                        )
                        snapshot = await task_context.persist_context_snapshot(
                            session,
                            task,
                            start_call + offset,
                            "subtask",
                            {
                                **built["context_manifest"],
                                "context_chars": len(json.dumps(context, ensure_ascii=False, default=str)),
                                "tool_count": len(context.get("tools", [])),
                                "observation_count": len(context.get("observations", [])),
                            },
                        )
                        current["last_context_snapshot_id"] = snapshot.id
                        contexts.append((current["id"], context, built["scope_revision"]))
                    await session.commit()
            except Exception as exc:
                self.account(state)
                state["answer"] = "子任务上下文准备失败，已保留现有证据。"
                await self.commit(task, state, "subtask_error", {"message": str(exc)[:300]}, status="partial")
                return False
            task._scope_revision = contexts[0][2] if contexts else None
            results = await asyncio.gather(
                *(
                    asyncio.wait_for(
                        self.model.decide("subtask", context),
                        timeout=min(
                            self.settings.llm_timeout,
                            max(0.01, state["budget"]["active_seconds"] - state["turn_usage"]["active_seconds"]),
                        ),
                    )
                    for _, context, _ in contexts
                ),
                return_exceptions=True,
            )
            usage_total = {"input_tokens": 0, "output_tokens": 0}
            summaries = []
            for (subtask_id, _, _), result in zip(contexts, results):
                subtask = state["subtasks"][subtask_id]
                subtask["status"] = "running"
                subtask["usage"]["model_calls"] += 1
                if isinstance(result, Exception):
                    usage = getattr(result, "usage", {}) or {}
                    usage_total["input_tokens"] += usage.get("input_tokens", 0)
                    usage_total["output_tokens"] += usage.get("output_tokens", 0)
                    subtask["status"] = "failed"
                    subtask["limitations"] = ["子任务模型调用失败或返回无效结构，主 Agent 将根据其余证据继续。"]
                    summaries.append({"id": subtask_id, "status": "failed"})
                    continue
                raw_decision, usage = result
                usage_total["input_tokens"] += usage.get("input_tokens", 0)
                usage_total["output_tokens"] += usage.get("output_tokens", 0)
                subtask["usage"]["input_tokens"] += usage.get("input_tokens", 0)
                subtask["usage"]["output_tokens"] += usage.get("output_tokens", 0)
                try:
                    decision = SubtaskDecision.model_validate(raw_decision)
                    calls = self._validate_subtask_decision(subtask, decision)
                    if calls:
                        subtask["pending"] = [
                            {
                                **call,
                                "id": uid(),
                                "step_id": subtask["parent_step_id"],
                                "subtask_id": subtask_id,
                            }
                            for call in calls
                        ]
                    else:
                        subtask["findings"] = [item.model_dump() for item in decision.findings]
                        subtask["limitations"] = decision.limitations
                        subtask["evidence_ids"] = sorted(
                            set(decision.evidence_ids)
                            | {eid for item in decision.findings for eid in item.evidence_ids}
                        )
                        subtask["status"] = "succeeded" if decision.kind == "finish" else "partial"
                        subtask["finished_at"] = time.time()
                    summaries.append({"id": subtask_id, "kind": decision.kind, "status": subtask["status"]})
                except (ValueError, TypeError) as exc:
                    subtask["status"] = "partial"
                    subtask["evidence_ids"] = sorted(self._subtask_evidence(subtask))
                    subtask["limitations"] = [
                        f"子任务收尾未通过证据边界校验：{str(exc)[:300]}；主 Agent 将仅根据已保留的本地证据核验。"
                    ]
                    subtask["finished_at"] = time.time()
                    summaries.append({"id": subtask_id, "status": "partial"})
            self.account(state, usage_total)
            await self.commit(task, state, "subtask_decision", {"subtasks": summaries})
            task = await self.load(task.id)

        state = copy.deepcopy(task.state)
        pending = [
            (item, call)
            for item in state.get("subtasks", {}).values()
            if item.get("constraint_version") == state["constraint_version"]
            for call in item.get("pending", [])
        ]
        if not pending:
            relevant = [
                item
                for item in state.get("subtasks", {}).values()
                if item.get("constraint_version") == state["constraint_version"]
            ]
            if relevant and all(self._subtask_terminal(item) for item in relevant):
                if state.get("subtask_completion_policy") == "evaluate":
                    state["phase"] = "evaluate"
                    state["feedback"] = "子任务调查已结束；请只基于保留证据完成评估，不要重规划或发起更多工具调用。"
                else:
                    state["phase"] = "execute"
                    state["feedback"] = "子任务已返回局部发现，请核对证据并完成当前步骤。"
                await self.commit(
                    task,
                    state,
                    "subtasks_completed",
                    {"subtasks": [{"id": item["id"], "status": item["status"]} for item in relevant]},
                )
            return True

        task = await self.reserve_many(
            task,
            "tool_calls",
            len(pending),
            phase="subtask_tools",
            subtask_ids=[item["id"] for item, _ in pending],
        )
        if not task:
            return False
        state = copy.deepcopy(task.state)
        pending = [
            (item, call)
            for item in state.get("subtasks", {}).values()
            if item.get("constraint_version") == state["constraint_version"]
            for call in item.get("pending", [])
        ]
        remaining = max(1, state["budget"]["active_seconds"] - state["turn_usage"]["active_seconds"])
        fingerprints = [self.tool_fingerprint(call, state["constraint_version"]) for _, call in pending]
        outcomes = await asyncio.gather(
            *(self.invoke_read_tool(task, call, remaining) for _, call in pending), return_exceptions=True
        )
        observations, evidences = [], []
        for (subtask, call), fingerprint, outcome in zip(pending, fingerprints, outcomes):
            if isinstance(outcome, Exception):
                outcome = ("failed", "tool_unavailable", {"message": "子任务工具执行失败或超时。", "retryable": True})
            observation, evidence = self.record_subtask_outcome(
                state, task, subtask["id"], call, fingerprint, *outcome
            )
            observations.append(observation)
            evidences.append(evidence)
        self.account(state)
        for subtask in state["subtasks"].values():
            subtask.pop("pending", None)
        await self.commit(
            task,
            state,
            "subtask_tool_result",
            {"count": len(observations), "subtasks": sorted({row["subtask_id"] for row in observations})},
            evidences=evidences,
        )
        return True

    def record_subtask_outcome(self, state, task, subtask_id, call, fingerprint, status, error, result):
        observation, evidence = self.record_tool_outcome(state, task, call, fingerprint, status, error, result)
        observation["subtask_id"] = subtask_id
        evidence.result["subtask_id"] = subtask_id
        subtask = state["subtasks"][subtask_id]
        subtask["usage"]["tool_calls"] += 1
        subtask["observations"].append(observation)
        if status == "success":
            subtask["evidence_ids"].append(observation["evidence_id"])
        return observation, evidence

    async def execute_tool(self, task):
        # 原始工具结果固化为 Evidence；下一次模型调用仅使用受限的 Observation。
        pending = task.state["pending"]
        if (
            pending.get("kind") != "batch"
            and pending["tool"] == "search_knowledge"
            and "_queries" not in pending
            and self.settings.llm_enabled
            and self.settings.query_expansion_limit
        ):
            task = await self.reserve(task, "model_calls")
            if not task:
                return False
            state = copy.deepcopy(task.state)
            try:
                remaining = max(
                    0.01,
                    state["budget"]["active_seconds"] - state["turn_usage"]["active_seconds"],
                )
                result, usage = await asyncio.wait_for(
                    auxiliary.call(self.settings, "expand", {"query": pending["arguments"].get("query", "")}),
                    timeout=min(self.settings.llm_timeout, remaining),
                )
                state["pending"]["_queries"] = [q[:500] for q in result["queries"]][
                    : self.settings.query_expansion_limit
                ]
                self.account(state, usage)
            except Exception:
                state["pending"]["_queries"] = []
                self.account(state)
            await self.commit(task, state, "query_expansion", {"queries": state["pending"]["_queries"]})
            return True
        calls = pending["calls"] if pending.get("kind") == "batch" else [pending]
        for _ in calls:
            task = await self.reserve(task, "tool_calls")
            if not task:
                return False
        state = copy.deepcopy(task.state)
        pending = state["pending"]
        calls = pending["calls"] if pending.get("kind") == "batch" else [pending]
        fingerprints = [self.tool_fingerprint(call, state["constraint_version"]) for call in calls]
        seen = {}
        rejected = []
        for fingerprint in fingerprints:
            seen[fingerprint] = seen.get(fingerprint, 0) + 1
            rejected.append(state["fingerprints"].get(fingerprint, 0) + seen[fingerprint] > 2)

        remaining = max(1, state["budget"]["active_seconds"] - state["turn_usage"]["active_seconds"])
        if len(calls) > 1:
            outcomes = await asyncio.gather(
                *(
                    self.invoke_read_tool(task, call, remaining, reject_duplicate=duplicate)
                    for call, duplicate in zip(calls, rejected)
                )
            )
            self.account(state)
            observations, evidences = [], []
            for call, fingerprint, outcome in zip(calls, fingerprints, outcomes):
                observation, evidence = self.record_tool_outcome(
                    state, task, call, fingerprint, *outcome
                )
                observations.append(observation)
                evidences.append(evidence)
            state["pending"] = None
            if not self._activate_inventory_fast_path(task, state, observations) and not self._advance_promotion_fast_path(
                state, observations
            ):
                state["phase"] = "execute"
                state["feedback"] = (
                    ""
                    if all(row["status"] == "success" for row in observations)
                    else "批量工具存在失败或空结果，请根据观察修正行动。"
                )
            await self.commit(
                task,
                state,
                "tool_result",
                {"batch": observations, "count": len(observations)},
                evidences=evidences,
            )
            return True

        call, fingerprint = calls[0], fingerprints[0]
        status, error = "success", None
        async with self.db.sessions() as session:
            try:
                if rejected[0]:
                    raise ValueError("相同参数已重复两次且未增加信息；请换方法或停止")
                async with self.db.read_sessions() as commerce:
                    result = await asyncio.wait_for(
                        invoke(
                            call["tool"],
                            call["arguments"],
                            commerce=commerce,
                            session=session,
                            task=task,
                            settings=self.settings,
                            artifact_id="ar_" + call["id"],
                        ),
                        timeout=min(30, remaining),
                    )
                if "rows" in result and not result["rows"]:
                    status = "empty"
            except (ValueError, SyntaxError, ArithmeticError) as exc:
                await session.rollback()
                result = {"message": str(exc)[:1200], "retryable": False}
                status, error = "failed", "invalid_tool_request"
            except Exception:
                await session.rollback()
                result = {"message": "工具执行失败或超时，可检查数据源后有限重试。", "retryable": True}
                status, error = "failed", "tool_unavailable"
            self.account(state)
            observation, evidence = self.record_tool_outcome(
                state, task, call, fingerprint, status, error, result
            )
            state["pending"] = None
            if not self._activate_inventory_fast_path(task, state, [observation]) and not self._advance_promotion_fast_path(
                state, [observation]
            ):
                state["phase"] = "execute"
                state["feedback"] = "" if status == "success" else f"工具返回 {status}，请根据观察修正行动。"
            await self.commit(task, state, "tool_result", observation, evidence=evidence, session=session)
        return True

    @staticmethod
    def tool_fingerprint(call, constraint_version):
        return hashlib.sha256(
            json.dumps([call["tool"], call["arguments"], constraint_version], sort_keys=True).encode()
        ).hexdigest()

    async def invoke_read_tool(self, task, call, remaining, *, reject_duplicate=False):
        """批量动作仅执行只读工具，每个调用使用独立会话以支持安全并行。"""
        if reject_duplicate:
            return (
                "failed",
                "invalid_tool_request",
                {"message": "相同参数已重复两次且未增加信息；请换方法或停止", "retryable": False},
            )
        status, error = "success", None
        async with self.db.sessions() as session:
            try:
                async with self.db.read_sessions() as commerce:
                    result = await asyncio.wait_for(
                        invoke(
                            call["tool"],
                            call["arguments"],
                            commerce=commerce,
                            session=session,
                            task=task,
                            settings=self.settings,
                            artifact_id="ar_" + call["id"],
                        ),
                        timeout=min(30, remaining),
                    )
                if "rows" in result and not result["rows"]:
                    status = "empty"
            except (ValueError, SyntaxError, ArithmeticError) as exc:
                await session.rollback()
                result = {"message": str(exc)[:1200], "retryable": False}
                status, error = "failed", "invalid_tool_request"
            except Exception:
                await session.rollback()
                result = {"message": "工具执行失败或超时，可检查数据源后有限重试。", "retryable": True}
                status, error = "failed", "tool_unavailable"
        return status, error, result

    def record_tool_outcome(self, state, task, call, fingerprint, status, error, result):
        eid = "ev_" + call["id"]
        state["fingerprints"][fingerprint] = state["fingerprints"].get(fingerprint, 0) + 1
        sid = call["step_id"]
        state["step_tools"][sid] = state["step_tools"].get(sid, 0) + 1
        observation = {
            "evidence_id": eid,
            "step_id": sid,
            "plan_version": len(state["plans"]),
            "tool": call["tool"],
            "arguments": call["arguments"],
            "status": status,
            "data": compact(result),
            "error_code": error,
            "constraint_version": state["constraint_version"],
        }
        state["observations"].append(observation)
        if status == "success" and "artifact_id" in result:
            state["artifacts"].append({**result, "constraint_version": state["constraint_version"]})
        evidence = Evidence(
            id=eid,
            task_id=task.id,
            tool=call["tool"],
            arguments=call["arguments"],
            result={
                "status": status,
                "data": result,
                "error_code": error,
                "constraint_version": state["constraint_version"],
            },
        )
        return observation, evidence


async def worker(database, settings, stop: asyncio.Event, model=None):
    runtime = AgentRuntime(database, settings, model)
    while not stop.is_set():
        async with database.sessions() as session:
            task_id = await session.scalar(
                select(Task.id)
                .where(Task.status.in_(["queued", "running"]), Task.lease_until <= time.time())
                .order_by(Task.created_at)
                .limit(1)
            )
        if task_id:
            await runtime.run(task_id)
        else:
            try:
                await asyncio.wait_for(stop.wait(), settings.worker_poll_seconds)
            except TimeoutError:
                pass
