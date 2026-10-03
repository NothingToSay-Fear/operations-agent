"""使用真实 PostgreSQL、向量模型与可选真实对话模型运行记忆评测。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from pydantic import BaseModel, Field
from sqlalchemy import select

from app import access, background, memory, retrieval, task_context
from app.agent.model import ModelGateway, invoke_structured
from app.agent.runtime import initial_state
from app.config import Settings
from app.db import Database
from app.evaluation_database import evaluation_urls, reset_application_database
from app.extension_models import BackgroundJob, HistoryUnit, MemoryCandidate, TaskMemory, UserMemory
from app.memory_evaluation_dataset import (
    MemoryEvaluationCase,
    MemoryEvaluationDataset,
    load_baseline,
    load_memory_evaluation_dataset,
    write_baseline,
)
from app.models import Task, User


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "memory" / "v1"


class SummaryClaim(BaseModel):
    claim: str
    retained: bool


class SummaryJudgment(BaseModel):
    """摘要语义评审的结构化输出。"""

    required_claims: list[SummaryClaim] = Field(default_factory=list, max_length=20)
    invented_claims: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(default="", max_length=1200)


SUMMARY_JUDGE_PROMPT = """你是严格的记忆摘要评审器。仅根据原始对话检查摘要和结构化 decisions/open_questions。
逐项判断 must_retain 是否被保留，允许同义表达；只把原始对话没有依据的具体事实列为 invented_claims。
不要因摘要未复述无关细节而判错，也不要把用户明确的待确认项当作事实。返回完整合法 JSON。"""


async def run(output: Path, dataset_path: Path = DEFAULT_DATASET) -> dict:
    """运行版本化记忆评测并写入逐题报告。"""

    dataset = load_memory_evaluation_dataset(dataset_path)
    settings = Settings()
    if not settings.embedding_model_path or not settings.reranker_model_path:
        raise RuntimeError("记忆评测要求配置真实向量与精排模型目录")
    output.mkdir(parents=True, exist_ok=False)
    app_url, _, _ = evaluation_urls("MEMORY_EVALUATION", settings)
    await reset_application_database(app_url, retrieval_indexes=True)
    settings = settings.model_copy(update={"app_database_url": app_url, "memory_recent_turn_limit": 4})
    db = Database(settings)
    try:
        health = await retrieval.health(settings)
        if health["embedding"] != "ready" or health["reranker"] != "ready":
            raise RuntimeError("真实向量或精排模型加载失败")
        cases: list[dict] = []
        async with db.sessions() as session:
            user = User(id="memory-evaluation-user", username="memory-evaluation-user", password_hash="disabled")
            session.add(user)
            await session.commit()
        for case in dataset.cases:
            if case.kind == "context":
                cases.append(await _evaluate_context_case(db, settings, case))
            elif case.kind == "history":
                cases.append(await _evaluate_history_case(db, settings, case, dataset.history_top_k))
            elif case.kind == "summary":
                cases.append(await _evaluate_summary_case(db, settings, case))
            else:
                cases.append(await _evaluate_lifecycle_case(db, settings, case))
        metrics = _metrics(cases)
        failures = _gate_failures(metrics, dataset)
        baseline_failures = _baseline_failures(metrics, dataset)
        report = {
            "database": "PostgreSQL",
            "dataset": {"version": dataset.version, "case_count": len(cases)},
            "models": {**health, "summary_model": settings.llm_model or "未配置"},
            "metrics": metrics,
            "quality_gates": dataset.quality_gates,
            "gate_failures": failures,
            "baseline_failures": baseline_failures,
            "cases": cases,
            "passed": not failures and not baseline_failures,
        }
        _write_report(output, report)
        print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
        return report
    finally:
        await db.close()


async def _new_task(session, user_id: str, goal: str, settings: Settings) -> Task:
    task = Task(user_id=user_id, goal=goal, state=initial_state(goal, settings))
    session.add(task)
    await session.flush()
    return task


async def _evaluate_context_case(db: Database, settings: Settings, case: MemoryEvaluationCase) -> dict:
    payload, expected = case.payload, case.payload["expected"]
    async with db.sessions() as session:
        task = await _new_task(session, "memory-evaluation-user", payload["messages"][0], settings)
        for index, item in enumerate(payload["memories"], start=1):
            if item["status"] == "active":
                session.add(
                    UserMemory(
                        id=item["id"],
                        user_id=task.user_id,
                        kind=item["kind"],
                        content=item["content"],
                        source_candidate_id=f"fixture-{index}",
                    )
                )
            else:
                session.add(
                    MemoryCandidate(
                        id=item["id"],
                        user_id=task.user_id,
                        task_id=task.id,
                        kind=item["kind"],
                        content=item["content"],
                        status=item["status"],
                    )
                )
        await session.flush()
        for seq, message in enumerate(payload["messages"]):
            await task_context.record_user_message(session, task, seq, message, settings, kind="goal" if seq == 0 else "message")
        built = await memory.build_context(session, task, settings)
        await task_context.persist_context_snapshot(session, task, 1, "memory_evaluation", built["context_manifest"])
        await session.commit()
    actual = built["effective_constraints"]
    constraint_results = {}
    for key, value in expected.get("constraints", {}).items():
        section = "business_constraints" if key in {"metrics", "dimensions", "time_range"} else "output_requirements"
        constraint_results[key] = actual.get(section, {}).get(key) == value
    selected = {item["id"] for item in built["long_term_memories"]}
    required = set(expected.get("required_memory_ids", []))
    excluded = set(expected.get("excluded_memory_ids", []))
    forbidden_terms = set(expected.get("excluded_constraint_terms", []))
    return {
        "id": case.id,
        "kind": case.kind,
        "constraint_results": constraint_results,
        "required_memory_ids": sorted(required),
        "selected_memory_ids": sorted(selected),
        "required_memory_hits": sorted(required & selected),
        "leaked_memory_ids": sorted(excluded & selected),
        "forbidden_constraint_terms": sorted(
            term for term in forbidden_terms if term in json.dumps(actual, ensure_ascii=False)
        ),
        "context_manifest": built["context_manifest"],
    }


async def _evaluate_history_case(
    db: Database, settings: Settings, case: MemoryEvaluationCase, top_k: int
) -> dict:
    payload, expected = case.payload, case.payload["expected"]
    async with db.sessions() as session:
        task = await _new_task(session, "memory-evaluation-user", payload["messages"][0], settings)
        other = await _new_task(session, "memory-evaluation-user", "无关历史任务", settings)
        for seq, text in enumerate(payload["messages"]):
            await memory.append_history(session, task, seq, "user", {"content": text}, settings)
        await memory.append_history(
            session,
            other,
            0,
            "user",
            {"content": expected.get("cross_task_anchor", "完全无关的其他任务内容")},
            settings,
        )
        await session.flush()
        units = list(
            await session.scalars(select(HistoryUnit).where(HistoryUnit.task_id.in_([task.id, other.id])))
        )
        vectors = await retrieval.encode([unit.content for unit in units], settings)
        for unit, vector in zip(units, vectors, strict=True):
            unit.embedding, unit.model_id = vector, settings.embedding_model_id
        await session.commit()
        result = await memory.history_search(session, task, payload["query"], settings, limit=top_k)
    rows = result["rows"]
    return {
        "id": case.id,
        "kind": case.kind,
        "query": payload["query"],
        "anchor": expected["anchor"],
        "anchor_hit": any(expected["anchor"] in row["text"] for row in rows),
        "cross_task_leak": any(
            row["task_id"] != task.id
            or (
                bool(expected.get("cross_task_anchor"))
                and expected["cross_task_anchor"] in row["text"]
            )
            for row in rows
        ),
        "returned_unit_ids": [row["id"] for row in rows],
        "rows": rows,
    }


async def _evaluate_summary_case(db: Database, settings: Settings, case: MemoryEvaluationCase) -> dict:
    payload, expected = case.payload, case.payload["expected"]
    if not settings.llm_enabled:
        return {"id": case.id, "kind": case.kind, "status": "skipped", "reason": "未配置真实对话模型"}
    async with db.sessions() as session:
        task = await _new_task(session, "memory-evaluation-user", payload["messages"][0], settings)
        for seq, text in enumerate(payload["messages"]):
            await task_context.record_user_message(session, task, seq, text, settings, kind="goal" if seq == 0 else "message")
        job = await session.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.task_id == task.id, BackgroundJob.kind == "summary")
            .with_for_update()
        )
        if not job:
            raise RuntimeError("摘要评测未创建摘要后台作业")
        job.status, job.lease_token, job.lease_until, job.attempts = (
            "running",
            "memory-evaluation-summary",
            time.time() + settings.background_lease_seconds,
            1,
        )
        await session.commit()
        task_id, job_id = task.id, job.id
    await background.process(db, settings, job_id, "memory-evaluation-summary")
    async with db.sessions() as session:
        stored = await session.get(TaskMemory, task_id)
        job = await session.get(BackgroundJob, job_id)
        if job.status != "completed" or not stored or not stored.summary:
            return {
                "id": case.id,
                "kind": case.kind,
                "status": "failed",
                "reason": job.error or "摘要后台作业没有发布结果",
            }
        summary = stored.summary
    judgment, _ = await invoke_structured(
        ModelGateway(settings).build(),
        SummaryJudgment,
        settings,
        SUMMARY_JUDGE_PROMPT,
        {
            "messages": payload["messages"],
            "summary": summary,
            "must_retain": expected["must_retain"],
            "must_not_invent": expected["must_not_invent"],
        },
    )
    judged = judgment.model_dump()
    retained = {item["claim"]: item["retained"] for item in judged["required_claims"]}
    return {
        "id": case.id,
        "kind": case.kind,
        "status": "completed",
        "summary": summary,
        "judgment": judged,
        "required_coverage": [bool(retained.get(item)) for item in expected["must_retain"]],
        "invented_claims": judged["invented_claims"],
    }


async def _evaluate_lifecycle_case(
    db: Database, settings: Settings, case: MemoryEvaluationCase
) -> dict:
    payload = case.payload
    fixture = payload["memory"]
    async with db.sessions() as session:
        task = await _new_task(session, "memory-evaluation-user", payload["messages"][0], settings)
        candidate = await memory.propose(
            session,
            task.user_id,
            fixture["content"],
            fixture["kind"],
            task=task,
            source=fixture["content"],
        )
        before = await memory.build_context(session, task, settings)
        confirmed = await memory.confirm(session, task.user_id, candidate.id, candidate.version)
        after_confirm = await memory.build_context(session, task, settings)
        row = await session.get(UserMemory, confirmed.id)
        row.status, row.version = "disabled", row.version + 1
        await access.bump_scope(session, [task.user_id])
        after_disable = await memory.build_context(session, task, settings)
        await session.commit()
    before_ids = {item["id"] for item in before["long_term_memories"]}
    confirmed_ids = {item["id"] for item in after_confirm["long_term_memories"]}
    disabled_ids = {item["id"] for item in after_disable["long_term_memories"]}
    return {
        "id": case.id,
        "kind": "lifecycle",
        "success": candidate.id not in before_ids and confirmed.id in confirmed_ids and confirmed.id not in disabled_ids,
    }


def _metrics(cases: list[dict]) -> dict[str, float]:
    context = [case for case in cases if case["kind"] == "context"]
    history = [case for case in cases if case["kind"] == "history"]
    lifecycle = [case for case in cases if case["kind"] == "lifecycle"]
    summary = [case for case in cases if case["kind"] == "summary" and case.get("status") == "completed"]
    constraints = [value for case in context for value in case["constraint_results"].values()]
    required_memories = [
        identifier in case["selected_memory_ids"]
        for case in context
        for identifier in case["required_memory_ids"]
    ]
    required_context = constraints + required_memories
    leakage = [
        bool(case["leaked_memory_ids"] or case["forbidden_constraint_terms"]) for case in context
    ]
    result = {
        "constraint_accuracy": _mean(constraints),
        "context_required_recall": _mean(required_context),
        "context_leakage_rate": _mean(leakage),
        "long_term_memory_recall": _mean(required_memories),
        "history_unit_recall_at_4": _mean(case["anchor_hit"] for case in history),
        "history_anchor_recall_at_4": _mean(case["anchor_hit"] for case in history),
        "history_cross_task_leakage_rate": _mean(case["cross_task_leak"] for case in history),
        "lifecycle_success_rate": _mean(case["success"] for case in lifecycle),
    }
    if summary:
        coverage = [value for case in summary for value in case["required_coverage"]]
        result["summary_required_coverage"] = _mean(coverage)
        result["summary_invention_rate"] = _mean(bool(case["invented_claims"]) for case in summary)
    return result


def _gate_failures(metrics: dict[str, float], dataset: MemoryEvaluationDataset) -> list[str]:
    failures = []
    for name, gate in dataset.quality_gates.items():
        value = metrics.get(name)
        if value is None:
            failures.append(f"缺少质量指标：{name}")
        elif "minimum" in gate and value < gate["minimum"]:
            failures.append(f"{name}={value:.4f} 低于门槛 {gate['minimum']:.4f}")
        elif "maximum" in gate and value > gate["maximum"]:
            failures.append(f"{name}={value:.4f} 高于门槛 {gate['maximum']:.4f}")
    return failures


def _baseline_failures(metrics: dict[str, float], dataset: MemoryEvaluationDataset) -> list[str]:
    baseline = load_baseline(dataset)
    if baseline is None:
        return []
    values, tolerance = baseline
    failures = []
    for name, gate in dataset.quality_gates.items():
        if name not in values or name not in metrics:
            continue
        if "minimum" in gate and metrics[name] < values[name] - tolerance:
            failures.append(f"{name} 相比基线下降超过 {tolerance:.2f}")
        if "maximum" in gate and metrics[name] > values[name] + tolerance:
            failures.append(f"{name} 相比基线上升超过 {tolerance:.2f}")
    return failures


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _write_report(output: Path, report: dict) -> None:
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 真实记忆评测",
        "",
        f"- 数据集：`{report['dataset']['version']}`；案例数：{report['dataset']['case_count']}",
        f"- 结果：{'通过' if report['passed'] else '未通过'}",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        *[f"| {key} | {value:.4f} |" for key, value in report["metrics"].items()],
        "",
        "## 逐题结果",
        "",
        "| ID | 类型 | 结果 |",
        "| --- | --- | --- |",
        *[
            f"| {case['id']} | {case['kind']} | {'失败' if case.get('status') == 'failed' else '完成'} |"
            for case in report["cases"]
        ],
    ]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", default="evaluation-reports/memory-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(Path(args.output).resolve(), args.dataset))
    if args.write_baseline:
        if not report["passed"]:
            raise SystemExit("评测未通过，拒绝写入基线")
        dataset = load_memory_evaluation_dataset(args.dataset)
        print(f"已写入基线：{write_baseline(dataset, report['metrics'])}")


if __name__ == "__main__":
    main()
