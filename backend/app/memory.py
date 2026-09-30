"""当前任务的摘要与历史，以及用户确认后才生效的长期记忆。"""

import json
import re
import time

from sqlalchemy import func, or_, select

from app import access, retrieval, task_context
from app.extension_models import (
    BackgroundJob,
    HistoryUnit,
    MemoryCandidate,
    MemoryEvent,
    UserMemory,
)
from app.models import Evidence, uid


KINDS = {"work_profile", "analysis_preference", "answer_preference", "focus_direction", "stable_constraint"}
UNSET = object()


def _validate_memory_content(content):
    if re.search(r"(?:密码|密钥|api[_ -]?key)\s*[:：=]|\bsk-[\w-]{12,}", content, re.I):
        raise ValueError("请勿把访问凭据保存为记忆")


async def explicit_forget(session, user_id, text):
    """只处理用户明确的遗忘命令；不接受模型或资料伪造的授权。"""
    all_records = text.strip() in {"清除所有记忆", "清空所有记忆", "忘记所有记忆"}
    match = re.fullmatch(r"(?:请|帮我)?忘记[：:，,\s]*(.+)", text.strip())
    if not all_records and not match:
        return None
    await access.scope_revision(session, user_id, lock=True)
    rows = list(await session.scalars(select(UserMemory).where(UserMemory.user_id == user_id)))
    if not all_records:
        target = match.group(1).strip()
        rows = [row for row in rows if row.content == target or row.id == target]
        if len(rows) != 1:
            return "未找到唯一匹配的长期记忆，请在记忆中心选择需要忘记的内容。"
    await access.bump_scope(session, [user_id])
    from sqlalchemy import delete

    for row in rows:
        session.add(
            MemoryEvent(
                user_id=user_id, memory_id=row.id, action="delete", detail={"reason": "用户明确要求忘记"}
            )
        )
        await session.execute(
            delete(MemoryCandidate).where(
                MemoryCandidate.user_id == user_id,
                (MemoryCandidate.id == row.source_candidate_id) | (MemoryCandidate.replaces_id == row.id),
            )
        )
        await session.delete(row)
    if all_records:
        await session.execute(delete(MemoryCandidate).where(MemoryCandidate.user_id == user_id))
    return (
        "已清除长期记忆及待确认候选。原始任务讨论仍保留在各自任务内。"
        if all_records
        else "已忘记该长期记忆，后续模型调用不再采用。"
    )


async def propose(
    session,
    user_id,
    content,
    kind="answer_preference",
    *,
    task=None,
    source="用户填写",
    source_seq=0,
    expires_at=None,
    replaces_id=None,
    replaces_version=None,
    candidate_id=None,
):
    content = content.strip()
    if kind not in KINDS or not 2 <= len(content) <= 600:
        raise ValueError("记忆类型或长度无效")
    _validate_memory_content(content)
    if task and task.user_id != user_id:
        raise ValueError("任务不存在")
    if replaces_id:
        old = await session.scalar(
            select(UserMemory).where(UserMemory.id == replaces_id, UserMemory.user_id == user_id)
        )
        if not old or old.version != replaces_version:
            raise ValueError("待替代记忆已变化，请刷新后重新编辑")
    existing = await session.scalar(
        select(MemoryCandidate).where(
            MemoryCandidate.user_id == user_id,
            MemoryCandidate.task_id == (task.id if task else None),
            MemoryCandidate.content == content,
            MemoryCandidate.status == "pending",
        )
    )
    if existing:
        return existing
    candidate = MemoryCandidate(
        id=candidate_id or uid(),
        user_id=user_id,
        task_id=task.id if task else None,
        content=content,
        kind=kind,
        source=source[:1000],
        source_seq=source_seq,
        replaces_id=replaces_id,
        replaces_version=replaces_version,
        expires_at=expires_at
        if expires_at
        else (time.time() + 90 * 86400 if kind == "focus_direction" else None),
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def confirm(session, user_id, candidate_id, version, *, content=None, kind=None, expires_at=UNSET):
    await access.scope_revision(session, user_id, lock=True)
    candidate = await session.scalar(
        select(MemoryCandidate)
        .where(MemoryCandidate.id == candidate_id, MemoryCandidate.user_id == user_id)
        .with_for_update()
    )
    if not candidate or candidate.status != "pending" or candidate.version != version:
        raise ValueError("候选已处理或版本已变化")
    if candidate.expires_at and candidate.expires_at <= time.time():
        raise ValueError("候选已过期")
    value = (content or candidate.content).strip()
    category = kind or candidate.kind
    if category not in KINDS or not 2 <= len(value) <= 600:
        raise ValueError("确认内容无效")
    _validate_memory_content(value)
    await access.bump_scope(session, [user_id])
    if candidate.replaces_id:
        old = await session.scalar(
            select(UserMemory)
            .where(UserMemory.id == candidate.replaces_id, UserMemory.user_id == user_id)
            .with_for_update()
        )
        if not old or old.version != candidate.replaces_version:
            raise ValueError("原记忆已经变化，本次确认失效")
        old.status, old.version = "superseded", old.version + 1
        session.add(
            MemoryEvent(
                user_id=user_id,
                memory_id=old.id,
                task_id=candidate.task_id,
                action="superseded",
            )
        )
    item = UserMemory(
        user_id=user_id,
        kind=category,
        content=value,
        source_candidate_id=candidate.id,
        expires_at=candidate.expires_at if expires_at is UNSET else expires_at,
    )
    session.add(item)
    await session.flush()
    candidate.status, candidate.version = "accepted", candidate.version + 1
    session.add(
        MemoryEvent(
            user_id=user_id,
            memory_id=item.id,
            task_id=candidate.task_id,
            action="confirmed",
            detail={"candidate_id": candidate.id},
        )
    )
    return item


async def select_memories(session, user_id, query, settings=None):
    rows = list(
        await session.scalars(
            select(UserMemory)
            .where(
                UserMemory.user_id == user_id,
                UserMemory.status == "active",
                or_(UserMemory.expires_at.is_(None), UserMemory.expires_at > time.time()),
            )
            .order_by(UserMemory.created_at.desc())
        )
    )
    relevant = set(retrieval.lexical_rank(query, [{"id": r.id, "text": r.content} for r in rows]))
    if settings and rows:
        try:
            vectors = await retrieval.encode([query] + [r.content for r in rows], settings)
            if vectors:
                relevant.update(
                    r.id for i, r in enumerate(rows) if retrieval.cosine(vectors[0], vectors[i + 1]) >= 0.5
                )
        except Exception:
            # 向量不可用时保留词面相关项及通用表达偏好。
            pass
    rows.sort(
        key=lambda r: (
            r.id not in relevant,
            r.kind not in {"answer_preference", "work_profile"},
            -r.created_at,
        )
    )
    return [
        {
            "id": r.id,
            "version": r.version,
            "kind": r.kind,
            "content": r.content,
            "expires_at": r.expires_at,
            "reason": "与当前目标相关" if r.id in relevant else "通用表达偏好或工作背景",
        }
        for r in rows
        if r.id in relevant or r.kind in {"answer_preference", "work_profile"}
    ][:5]


async def append_history(session, task, seq, kind, payload, settings):
    """与任务事件同事务记录；业务查询原始大结果继续存放证据表。"""
    if kind not in {"user", "user_control", "plan_updated", "action", "evaluation", "tool_result"}:
        return
    if await session.scalar(
        select(HistoryUnit.id).where(HistoryUnit.task_id == task.id, HistoryUnit.seq == seq)
    ):
        return
    content = json.dumps(payload, ensure_ascii=False, default=str)[:10000]
    evidence_ids = re.findall(r"ev_[a-zA-Z0-9_-]+", content)
    revision = await access.scope_revision(session, task.user_id)
    unit = HistoryUnit(
        task_id=task.id,
        seq=seq,
        kind=kind,
        content=content,
        search_terms=" ".join(retrieval.tokens(content)),
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        message_ids=list(dict.fromkeys(payload.get("message_ids", []))),
        scope_revision=revision,
    )
    session.add(unit)
    await session.flush()
    session.add(BackgroundJob(key="history:" + unit.id, kind="history", target_id=unit.id, task_id=task.id))
    if kind in {"user", "user_control"} and (kind == "user" or payload.get("action") == "message"):
        text = payload.get("content", payload.get("message", ""))
        match = re.match(r"^(?:请|帮我)?记住[：:，,\s]*(.+)$", text)
        if match:
            await propose(session, task.user_id, match.group(1), task=task, source=text, source_seq=seq)
        session.add(
            BackgroundJob(key="candidates:" + unit.id, kind="candidates", target_id=unit.id, task_id=task.id)
        )


async def unit_allowed(session, task, unit, revision):
    if unit.task_id != task.id:
        return False
    if unit.kind not in {"user", "user_control"} and unit.scope_revision != revision:
        return False
    for eid in unit.evidence_ids:
        evidence = await session.scalar(
            select(Evidence).where(Evidence.id == eid, Evidence.task_id == task.id)
        )
        if evidence and not await access.references_allowed(session, task.user_id, evidence.result):
            return False
    return True


async def history_search(session, task, query, settings, limit=4, unit_id=None):
    # task_id 只取当前受信任任务，不允许模型传入或选择另一个任务。
    base = select(HistoryUnit).where(HistoryUnit.task_id == task.id)
    revision = await access.scope_revision(session, task.user_id)
    if unit_id:
        rows = list(await session.scalars(base.where(HistoryUnit.id == unit_id)))
        if not rows:
            raise ValueError("历史片段不属于当前任务")
    elif session.bind.dialect.name == "postgresql":
        terms = " | ".join(dict.fromkeys(retrieval.tokens(query)[:24]))
        rows, ranks = [], []
        if terms:
            vector, tsquery = (
                func.to_tsvector("simple", HistoryUnit.search_terms),
                func.to_tsquery("simple", terms),
            )
            sparse = list(
                await session.scalars(
                    base.where(vector.op("@@")(tsquery))
                    .order_by(func.ts_rank_cd(vector, tsquery).desc())
                    .limit(20)
                )
            )
            rows += sparse
            ranks.append([r.id for r in sparse])
        try:
            vectors = await retrieval.encode([query], settings)
            if vectors:
                distance = HistoryUnit.embedding.cosine_distance(vectors[0])
                dense = list(
                    await session.scalars(
                        base.where(
                            HistoryUnit.model_id == settings.embedding_model_id,
                            HistoryUnit.embedding.is_not(None),
                            distance <= 0.65,
                        )
                        .order_by(distance)
                        .limit(20)
                    )
                )
                rows += dense
                ranks.append([r.id for r in dense])
        except Exception:
            pass
        by_id = {r.id: r for r in rows}
        rows = [by_id[i] for i in retrieval.fuse(ranks)]
    else:
        all_rows = list(await session.scalars(base))
        ids = retrieval.lexical_rank(query, [{"id": r.id, "text": r.content} for r in all_rows])
        try:
            vectors = await retrieval.encode([query], settings)
            if vectors:
                dense = [
                    r.id
                    for r in sorted(all_rows, key=lambda r: -retrieval.cosine(r.embedding, vectors[0]))
                    if r.model_id == settings.embedding_model_id
                    and retrieval.cosine(r.embedding, vectors[0]) >= 0.35
                ][:20]
                ids = retrieval.fuse([ids, dense])
        except Exception:
            pass
        by_id = {r.id: r for r in all_rows}
        rows = [by_id[i] for i in ids]
    selected = []
    for row in rows:
        if await unit_allowed(session, task, row, revision):
            selected.append(
                {
                    "id": row.id,
                    "task_id": task.id,
                    "seq": row.seq,
                    "kind": row.kind,
                    "text": row.content,
                    "created_at": row.created_at,
                    "evidence_ids": row.evidence_ids,
                }
            )
    if not unit_id:
        try:
            selected, _ = await retrieval.rerank(query, selected[:20], settings, limit)
        except Exception:
            selected = selected[:limit]
    return {
        "rows": selected[:limit],
        "warning": "仅为当前任务历史背景；业务事实必须重新查询",
        "scope_revision": revision,
    }


async def build_context(session, task, settings):
    revision = await access.scope_revision(session, task.user_id)
    constraints, short_term = await task_context.ensure_task_context(session, task, settings)
    latest = await task_context.latest_user_message(session, task)
    current_instruction = latest.content if latest else task.goal
    long_term = await select_memories(session, task.user_id, task.goal + " " + current_instruction, settings)
    summary_value = short_term.summary if short_term.scope_revision == revision else {}
    recent_turns = [
        row
        for row in list(short_term.recent_turns or [])[-settings.memory_recent_turn_limit :]
        if row.get("role") == "user" or row.get("scope_revision", revision) == revision
    ]
    required = {
        "current_instruction": current_instruction,
        "effective_constraints": constraints.state,
        "task_memory": summary_value,
        "long_term_memories": long_term,
    }
    required_size = len(json.dumps(required, ensure_ascii=False))
    remaining = max(0, settings.memory_context_char_budget - required_size)
    selected_turns = []
    for turn in reversed(recent_turns):
        size = len(json.dumps(turn, ensure_ascii=False))
        if size > remaining:
            break
        selected_turns.insert(0, turn)
        remaining -= size

    observations, remaining = [], max(0, settings.memory_context_char_budget - required_size)
    remaining -= sum(len(json.dumps(turn, ensure_ascii=False)) for turn in selected_turns)
    for observation in reversed(task.state["observations"]):
        if not await access.references_allowed(session, task.user_id, observation.get("data", {})):
            continue
        size = len(json.dumps(observation, ensure_ascii=False))
        if remaining < size:
            break
        observations.insert(0, observation)
        remaining -= size
    # 明确约束不做有损摘要；约束本身过大时显式暂停，而不默默丢弃用户条件。
    if required_size > settings.memory_context_char_budget:
        raise ValueError("明确用户约束超出上下文上限，请整理当前目标后继续")
    evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for observation in observations
            for evidence_id in re.findall(r"ev_[a-zA-Z0-9_-]+", json.dumps(observation, ensure_ascii=False))
        )
    )
    history_unit_ids = list(
        dict.fromkeys(
            row.get("id")
            for observation in observations
            if observation.get("tool") in {"search_task_history", "read_task_history"}
            for row in observation.get("data", {}).get("rows", [])
            if row.get("id")
        )
    )
    return {
        "scope_revision": revision,
        "current_instruction": current_instruction,
        "effective_constraints": constraints.state,
        "constraint_state_version": constraints.version,
        "long_term_memories": long_term,
        "task_memory": summary_value,
        "memory_summary_version": short_term.version,
        "memory_state_revision": short_term.revision,
        "recent_turns": selected_turns,
        "messages": selected_turns,
        "observations": observations,
        "history_scope": "current_task_only",
        "context_manifest": {
            "scope_revision": revision,
            "constraint_state_version": constraints.version,
            "memory_summary_version": short_term.version,
            "memory_state_revision": short_term.revision,
            "current_message_id": latest.id if latest else None,
            "recent_message_ids": [row.get("message_id") for row in selected_turns],
            "long_term_memory_versions": [{"id": row["id"], "version": row["version"]} for row in long_term],
            "evidence_ids": evidence_ids,
            "history_unit_ids": history_unit_ids,
            "history_scope": "current_task_only",
        },
    }
