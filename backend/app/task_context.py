"""任务消息、结构化有效约束、摘要与近期上下文窗口。"""

from __future__ import annotations

import copy
import json
import re
import time

from sqlalchemy import select

from app.extension_models import (
    BackgroundJob,
    ModelContextSnapshot,
    TaskConstraintEvent,
    TaskConstraintState,
    TaskMemory,
    TaskMessage,
)
from app.models import uid


DIMENSIONS = {
    "channel": ("渠道", "流量来源"),
    "product": ("商品", "sku", "spu"),
    "inventory": ("库存", "缺货", "补货"),
}
METRICS = {
    "paid_gmv": ("gmv", "成交额", "支付金额"),
    "units": ("销量", "销售件数", "件数"),
    "refund_amount": ("退款额", "退款金额"),
    "gross_profit_before_refunds": ("毛利", "毛利润"),
}
CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
CONSTRAINT_MARKERS = ("必须", "不要", "不允许", "只能", "只使用", "只依据", "需要", "给出", "结合")


def _base_state(goal: str, message_id: str) -> dict:
    return {
        "goal": {"value": goal, "source_message_id": message_id},
        "business_constraints": {
            "time_range": None,
            "metrics": [],
            "dimensions": [],
        },
        "output_requirements": {},
        "source_policy": {"business_facts_require_tools": True},
        "general_constraints": [],
        "pending_constraints": [],
    }


def _mentioned(text: str, aliases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(alias.lower() in lowered for alias in aliases)


def _recommendation_count(text: str) -> int | None:
    match = re.search(
        r"(?:([1-9]|10|[一二两三四五六七八九十])\s*(?:条|个)\s*(?:有证据的)?(?:运营)?建议|建议\s*(?:改成|调整为|保留)?\s*([1-9]|10|[一二两三四五六七八九十])\s*(?:条|个))",
        text,
    )
    if not match:
        return None
    value = match.group(1) or match.group(2)
    return int(value) if value.isdigit() else CHINESE_NUMBERS.get(value)


def _raw_time_constraint(text: str) -> str | None:
    patterns = (
        r"数据截止日期[^，。；\n]{0,40}",
        r"(?:最近|此前|之前|过去)\s*[一二两三四五六七八九十\d]+\s*(?:天|周|月)",
        r"\d{4}[-年]\d{1,2}[-月]\d{1,2}日?(?:\s*(?:至|到|~|—|-)+\s*\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0).strip()
    return None


def _general_constraints(text: str, message_id: str) -> list[dict]:
    parts = [part.strip() for part in re.split(r"[。；;\n]+", text) if part.strip()]
    return [
        {
            "id": "constraint_" + uid(),
            "text": part[:1000],
            "source_message_id": message_id,
            "status": "active",
        }
        for part in parts
        if any(marker in part for marker in CONSTRAINT_MARKERS)
        and not any(_mentioned(part, aliases) for aliases in (*DIMENSIONS.values(), *METRICS.values()))
        and _recommendation_count(part) is None
        and _raw_time_constraint(part) is None
        and not any(word in part for word in ("知识库", "资料中出现"))
    ]


def apply_constraint_update(previous: dict | None, text: str, message_id: str) -> tuple[dict, list[dict]]:
    # 追问以覆盖式事件更新结构化约束；原始消息仍完整保留。
    """用确定性规则更新常见业务字段，同时保留无法类型化的用户原文约束。"""
    current = copy.deepcopy(previous) if previous else _base_state(text, message_id)
    changes: list[dict] = []
    business = current.setdefault("business_constraints", {})

    metrics = [name for name, aliases in METRICS.items() if _mentioned(text, aliases)]
    if metrics and business.get("metrics") != metrics:
        changes.append({"action": "replace", "field": "metrics", "value": metrics})
        business["metrics"] = metrics

    dimensions = list(business.get("dimensions") or [])
    only_match = re.search(r"只(?:分析|看|保留|按照)?([^。；，,\n]+)", text)
    mentioned_dimensions = [name for name, aliases in DIMENSIONS.items() if _mentioned(text, aliases)]
    if only_match:
        selected = [name for name, aliases in DIMENSIONS.items() if _mentioned(only_match.group(1), aliases)]
        if selected and dimensions != selected:
            dimensions = selected
            changes.append({"action": "replace", "field": "dimensions", "value": dimensions})
    else:
        for name, aliases in DIMENSIONS.items():
            denied = any(
                re.search(
                    rf"(?:不看|不分析|取消|移除|排除|不要)[^。；，,\n]{{0,8}}{re.escape(alias)}", text, re.I
                )
                for alias in aliases
            )
            if denied and name in dimensions:
                dimensions.remove(name)
                changes.append({"action": "remove", "field": "dimensions", "value": name})
        for name in mentioned_dimensions:
            if name not in dimensions:
                dimensions.append(name)
                changes.append({"action": "add", "field": "dimensions", "value": name})
    business["dimensions"] = dimensions

    raw_time = _raw_time_constraint(text)
    if raw_time and (business.get("time_range") or {}).get("raw") != raw_time:
        business["time_range"] = {"type": "user_expression", "raw": raw_time, "source_message_id": message_id}
        changes.append({"action": "replace", "field": "time_range", "value": raw_time})

    output = current.setdefault("output_requirements", {})
    count = _recommendation_count(text)
    if count is not None and output.get("recommendation_count") != count:
        output["recommendation_count"] = count
        changes.append({"action": "replace", "field": "recommendation_count", "value": count})
    if any(word in text for word in ("有证据", "引用证据", "引用对应", "依据")):
        if output.get("evidence_required") is not True:
            output["evidence_required"] = True
            changes.append({"action": "set", "field": "evidence_required", "value": True})

    policy = current.setdefault("source_policy", {"business_facts_require_tools": True})
    if any(word in text for word in ("取消只使用知识库", "不再只依据知识库", "可以使用经营数据")):
        if policy.pop("knowledge_only", None) is not None:
            changes.append({"action": "remove", "field": "knowledge_only", "value": True})
    elif any(word in text for word in ("只依据知识库", "只使用知识库", "只引用知识库", "不要使用未在资料")):
        if policy.get("knowledge_only") is not True:
            policy["knowledge_only"] = True
            changes.append({"action": "set", "field": "knowledge_only", "value": True})

    existing = current.setdefault("general_constraints", [])
    for item in _general_constraints(text, message_id):
        if not any(row.get("text") == item["text"] and row.get("status") == "active" for row in existing):
            existing.append(item)
            changes.append({"action": "add", "field": "general_constraints", "value": item["text"]})
    current["general_constraints"] = existing[-20:]
    return current, changes


async def ensure_task_context(
    session, task, settings, *, create_memory: bool = True
) -> tuple[TaskConstraintState, TaskMemory | None]:
    """为新旧任务建立可恢复的约束快照和近期窗口。"""
    constraint = await session.get(TaskConstraintState, task.id)
    memory = await session.get(TaskMemory, task.id)
    message = await session.scalar(
        select(TaskMessage).where(
            TaskMessage.task_id == task.id, TaskMessage.seq == 0, TaskMessage.role == "user"
        )
    )
    if message is None:
        message = TaskMessage(
            task_id=task.id,
            seq=0,
            role="user",
            kind="goal",
            content=task.goal,
            scope_revision=0,
        )
        session.add(message)
        await session.flush()
    if constraint is None:
        state, _ = apply_constraint_update(None, task.goal, message.id)
        constraint = TaskConstraintState(task_id=task.id, version=1, state=state)
        session.add(constraint)
    if memory is None and create_memory:
        memory = TaskMemory(
            task_id=task.id,
            recent_turns=[_turn_value(message, settings)],
            revision=1,
            scope_revision=0,
        )
        session.add(memory)
    return constraint, memory


def _turn_value(message: TaskMessage, settings) -> dict:
    limit = settings.memory_turn_char_limit
    content = message.content.strip()
    if len(content) > limit:
        content = content[:limit] + "…"
    return {
        "message_id": message.id,
        "seq": message.seq,
        "role": message.role,
        "kind": message.kind,
        "content": content,
        "scope_revision": message.scope_revision,
    }


async def _append_recent(session, task, message: TaskMessage, settings) -> TaskMemory:
    # 近期窗口按数量与字符双重上限裁剪，超出原文由摘要与 HistoryUnit 承接。
    _, state = await ensure_task_context(session, task, settings)
    state = await session.scalar(select(TaskMemory).where(TaskMemory.task_id == task.id).with_for_update())
    turns = list(state.recent_turns or [])
    if not any(row.get("message_id") == message.id for row in turns):
        turns.append(_turn_value(message, settings))
        state.recent_turns = turns
        state.revision += 1
        state.updated_at = time.time()
    size = len(json.dumps({"summary": state.summary, "turns": turns}, ensure_ascii=False))
    pending = await session.scalar(
        select(BackgroundJob.id).where(
            BackgroundJob.task_id == task.id,
            BackgroundJob.kind == "summary",
            BackgroundJob.status.in_(["queued", "running", "blocked"]),
        )
    )
    if (
        len(turns) > settings.memory_recent_turn_limit
        or (size >= settings.memory_compact_threshold and len(turns) > 4)
    ) and not pending:
        session.add(
            BackgroundJob(
                key=f"summary:{task.id}:{message.seq}:{message.id}",
                kind="summary",
                target_id=task.id,
                task_id=task.id,
            )
        )
    return state


async def record_user_message(
    session,
    task,
    seq: int,
    content: str,
    settings,
    *,
    kind: str = "message",
    update_constraints: bool = True,
    append_recent: bool = True,
) -> tuple[TaskMessage, TaskConstraintState]:
    constraint, _ = await ensure_task_context(session, task, settings, create_memory=append_recent)
    message = await session.scalar(
        select(TaskMessage).where(
            TaskMessage.task_id == task.id,
            TaskMessage.seq == seq,
            TaskMessage.role == "user",
        )
    )
    if message is None:
        message = TaskMessage(
            task_id=task.id,
            seq=seq,
            role="user",
            kind=kind,
            content=content.strip(),
            scope_revision=0,
        )
        session.add(message)
        await session.flush()
    if append_recent:
        await _append_recent(session, task, message, settings)
    if update_constraints:
        locked = await session.scalar(
            select(TaskConstraintState).where(TaskConstraintState.task_id == task.id).with_for_update()
        )
        previous_version = locked.version
        updated, changes = apply_constraint_update(locked.state, content, message.id)
        if seq != 0:
            locked.version += 1
        locked.state = updated
        locked.updated_at = time.time()
        if seq != 0:
            session.add(
                TaskConstraintEvent(
                    task_id=task.id,
                    from_version=previous_version,
                    to_version=locked.version,
                    source_message_id=message.id,
                    changes=changes or [{"action": "retain", "field": "current_instruction"}],
                )
            )
        constraint = locked
    return message, constraint


async def record_assistant_message(
    session,
    task,
    seq: int,
    content: str,
    settings,
    *,
    kind="answer",
    scope_revision=0,
):
    content = content.strip()
    if not content:
        return None
    message = await session.scalar(
        select(TaskMessage).where(
            TaskMessage.task_id == task.id,
            TaskMessage.seq == seq,
            TaskMessage.role == "assistant",
        )
    )
    if message is None:
        message = TaskMessage(
            task_id=task.id,
            seq=seq,
            role="assistant",
            kind=kind,
            content=content,
            scope_revision=scope_revision,
        )
        session.add(message)
        await session.flush()
    await _append_recent(session, task, message, settings)
    return message


async def latest_user_message(session, task) -> TaskMessage | None:
    return await session.scalar(
        select(TaskMessage)
        .where(TaskMessage.task_id == task.id, TaskMessage.role == "user")
        .order_by(TaskMessage.seq.desc())
        .limit(1)
    )


async def persist_context_snapshot(session, task, call_number: int, phase: str, manifest: dict):
    # 仅持久化上下文清单和版本引用，用于复盘，不复制完整提示词。
    existing = await session.scalar(
        select(ModelContextSnapshot).where(
            ModelContextSnapshot.task_id == task.id,
            ModelContextSnapshot.call_number == call_number,
        )
    )
    if existing:
        return existing
    row = ModelContextSnapshot(
        task_id=task.id,
        call_number=call_number,
        phase=phase,
        manifest=manifest,
    )
    session.add(row)
    await session.flush()
    return row
