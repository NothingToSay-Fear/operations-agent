"""可恢复的规划、执行和评估循环，通过任务版本与租约阻止过期提交。"""

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

from app.agent.contracts import Action, Decision, Plan
from app.agent.model import ModelGateway, ModelUnavailable, PROMPT_VERSION, model_failure
from app.agent.tools import catalog, invoke
from app import access, auxiliary, memory
from app.extension_models import MemoryEvent
from app.models import Evidence, Run, Task, TaskEvent, uid

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "partial", "blocked", "failed", "cancelled"}


def initial_state(goal, settings):
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
        constraint_version=1,
    )


def compact(value, maximum=4500):
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


class LeaseLost(Exception):
    pass


class ContextChanged(LeaseLost):
    pass


class GraphState(TypedDict):
    task_id: str
    more: bool


class AgentRuntime:
    def __init__(self, database, settings, model=None):
        self.db = database
        self.settings = settings
        self.model = model or ModelGateway(settings)
        self.owner = uid()
        builder = StateGraph(GraphState)
        builder.add_node("advance", self._node)
        builder.add_edge(START, "advance")
        builder.add_conditional_edges("advance", lambda state: "advance" if state["more"] else END)
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
                        "tools_version": "1.0",
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
            await self.graph.ainvoke({"task_id": task_id, "more": True}, {"recursion_limit": 150})
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

    async def commit(self, task, state, kind, payload, *, status="running", evidence=None, session=None):
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
            if kind == "model_started" and state["usage"]["model_calls"] > state["budget"]["model_calls"]:
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
            if evidence:
                if not await access.references_allowed(session, task.user_id, evidence.result):
                    raise ContextChanged()
                session.add(evidence)
            await memory.append_history(session, task, state["seq"], kind, payload, self.settings)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            if owned_session:
                await session.close()

    async def _node(self, graph_state):
        task = await self.load(graph_state["task_id"])
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
                answer="",
                feedback="资料权限、版本或长期记忆已变化，请重新核对当前约束和证据。",
                finalizing=False,
            )
            state["constraint_version"] += 1
            state["observations"] = []
            task._skip_scope_checks = True
            await self.commit(task, state, "context_changed", {"message": state["feedback"]})
            return {"task_id": task.id, "more": True}
        more = await self.advance(task)
        return {"task_id": task.id, "more": more}

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
        s = task.state
        return {
            "goal": task.goal,
            "messages": s["messages"],
            "plan": s["plan"],
            "step_results": s["steps"],
            "current_step": self.current_step(s),
            "observations": s["observations"][-20:],
            "artifacts": s["artifacts"],
            "remaining_budget": {k: s["budget"][k] - s["usage"].get(k, 0) for k in s["budget"]},
            "feedback": s.get("feedback", ""),
            "tools": catalog(),
            "constraint_version": s["constraint_version"],
        }

    async def reserve(self, task, kind):
        state = copy.deepcopy(task.state)
        now = time.time()
        if state.get("active_started_at"):
            state["usage"]["active_seconds"] += min(
                now - state["active_started_at"], self.settings.lease_seconds
            )
        state["active_started_at"] = now
        usage, budget = state["usage"], state["budget"]
        if usage.get(kind, 0) >= budget[kind] or usage["active_seconds"] >= budget["active_seconds"]:
            state["answer"] = "已达到任务执行预算。已保存现有证据和成果，未完成的工作保留在计划中。"
            state.pop("active_started_at", None)
            await self.commit(task, state, "budget_exhausted", {"message": state["answer"]}, status="partial")
            return None
        usage[kind] += 1
        await self.commit(
            task,
            state,
            "model_started" if kind == "model_calls" else "tool_started",
            {"phase": state["phase"], "call": usage[kind], "tool": (state.get("pending") or {}).get("tool")},
        )
        return await self.load(task.id)

    def account(self, state, usage=None):
        state["usage"]["active_seconds"] += max(0, time.time() - state.pop("active_started_at", time.time()))
        for key in ["input_tokens", "output_tokens"]:
            state["usage"][key] += (usage or {}).get(key, 0)
        a, b = self.settings.input_price_per_million, self.settings.output_price_per_million
        state["usage"]["estimated_cost"] = (
            (state["usage"]["input_tokens"] * a + state["usage"]["output_tokens"] * b) / 1e6
            if a is not None and b is not None
            else None
        )

    async def advance(self, task):
        state = task.state
        remaining = state["budget"]["model_calls"] - state["usage"]["model_calls"]
        tools_exhausted = state["usage"]["tool_calls"] >= state["budget"]["tool_calls"]
        if state["observations"] and not state.get("finalizing") and (0 < remaining <= 2 or tools_exhausted):
            state = copy.deepcopy(state)
            state.update(
                phase="evaluate",
                pending=None,
                finalizing=True,
                feedback="剩余额度用于最终交付。根据现有证据核对原始用户目标；足够则 finish，不足则 stop 并给出可用结论和明确缺口。不要继续查询或扩展计划。",
            )
            await self.commit(task, state, "finalizing", {"message": "正在根据已有证据完成最终交付"})
            return True
        if state["phase"] == "tool":
            return await self.execute_tool(task)
        if state["phase"] == "execute" and not self.current_step(state):
            state = copy.deepcopy(state)
            state["phase"] = "evaluate"
            await self.commit(task, state, "evaluating", {"message": "正在检查任务完成条件"})
            return True
        task = await self.reserve(task, "model_calls")
        if not task:
            return False
        state = copy.deepcopy(task.state)
        phase = state["phase"]
        try:
            async with self.db.sessions() as session:
                built = await memory.build_context(session, task, self.settings)
                await session.commit()
            task._scope_revision = built["scope_revision"]
            state["context_scope_revision"] = built["scope_revision"]
            state["used_memories"] = [
                {"id": m["id"], "version": m["version"], "reason": m["reason"]}
                for m in built["long_term_memories"]
            ]
            context = {**self.context(task), **built}
            remaining_seconds = state["budget"]["active_seconds"] - state["usage"]["active_seconds"]
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
            if state["usage"]["active_seconds"] >= state["budget"]["active_seconds"]:
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
        if state["plan"]:
            if state["usage"]["replans"] >= state["budget"]["replans"]:
                state["answer"] = "已达到重规划上限，保留现有成果和待完成步骤。"
                await self.commit(
                    task, state, "budget_exhausted", {"message": state["answer"]}, status="partial"
                )
                return False
            state["usage"]["replans"] += 1
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

    async def apply_action(self, task, state, action):
        step = self.current_step(state)
        if step is None:
            raise ValueError("没有可执行步骤，应评估总目标")
        self.check_evidence(state, action.evidence_ids)
        if action.kind == "tool":
            if state["step_tools"].get(step["id"], 0) >= self.settings.max_step_tools:
                state["phase"] = "plan"
                state["feedback"] = (
                    "当前步骤已达到工具预算，请基于已有证据重新规划剩余子目标，避免重复已完成的查询。"
                )
            else:
                state["pending"] = {**action.model_dump(), "id": uid(), "step_id": step["id"]}
                state["phase"] = "tool"
        elif action.kind == "step_done":
            state["steps"][step["id"]] = {
                "status": "succeeded",
                "summary": action.summary,
                "evidence_ids": action.evidence_ids,
            }
            state["plans"][-1]["execution_snapshot"] = copy.deepcopy(state["steps"])
            state["phase"] = "evaluate"
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

    async def execute_tool(self, task):
        pending = task.state["pending"]
        if (
            pending["tool"] == "search_knowledge"
            and "_queries" not in pending
            and self.settings.llm_enabled
            and self.settings.query_expansion_limit
        ):
            task = await self.reserve(task, "model_calls")
            if not task:
                return False
            state = copy.deepcopy(task.state)
            try:
                remaining = max(0.01, state["budget"]["active_seconds"] - state["usage"]["active_seconds"])
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
        task = await self.reserve(task, "tool_calls")
        if not task:
            return False
        state = copy.deepcopy(task.state)
        pending = state["pending"]
        eid, aid = "ev_" + pending["id"], "ar_" + pending["id"]
        fingerprint = hashlib.sha256(
            json.dumps(
                [pending["tool"], pending["arguments"], state["constraint_version"]], sort_keys=True
            ).encode()
        ).hexdigest()
        status, error = "success", None
        async with self.db.sessions() as session:
            try:
                if state["fingerprints"].get(fingerprint, 0) >= 2:
                    raise ValueError("相同参数已重复两次且未增加信息；请换方法或停止")
                async with self.db.read_sessions() as commerce:
                    remaining = max(1, state["budget"]["active_seconds"] - state["usage"]["active_seconds"])
                    result = await asyncio.wait_for(
                        invoke(
                            pending["tool"],
                            pending["arguments"],
                            commerce=commerce,
                            session=session,
                            task=task,
                            settings=self.settings,
                            artifact_id=aid,
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
            state["fingerprints"][fingerprint] = state["fingerprints"].get(fingerprint, 0) + 1
            sid = pending["step_id"]
            state["step_tools"][sid] = state["step_tools"].get(sid, 0) + 1
            observation = {
                "evidence_id": eid,
                "step_id": sid,
                "plan_version": len(state["plans"]),
                "tool": pending["tool"],
                "arguments": pending["arguments"],
                "status": status,
                "data": compact(result),
                "error_code": error,
                "constraint_version": state["constraint_version"],
            }
            state["observations"].append(observation)
            if status == "success" and "artifact_id" in result:
                state["artifacts"].append({**result, "constraint_version": state["constraint_version"]})
            state["pending"] = None
            state["phase"] = "execute"
            state["feedback"] = "" if status == "success" else f"工具返回 {status}，请根据观察修正行动。"
            evidence = Evidence(
                id=eid,
                task_id=task.id,
                tool=pending["tool"],
                arguments=pending["arguments"],
                result={
                    "status": status,
                    "data": result,
                    "error_code": error,
                    "constraint_version": state["constraint_version"],
                },
            )
            await self.commit(task, state, "tool_result", observation, evidence=evidence, session=session)
        return True


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
