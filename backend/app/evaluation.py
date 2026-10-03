"""使用真实模型、隔离数据库和版本化案例执行 Plan-and-Execute 评测。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import select

from app.agent.runtime import AgentRuntime, initial_state
from app.agent_evaluation_dataset import (
    AgentEvaluationCase,
    AgentEvaluationDataset,
    load_agent_evaluation_dataset,
    load_baseline,
    write_baseline,
)
from app.config import get_settings
from app.db import Database
from app.evaluation_database import (
    evaluation_urls,
    grant_commerce_read_access,
    reset_application_database,
    reset_commerce_database,
)
from app.models import Artifact, Task, User
from app.seed import seed_database


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "agent" / "v1"


def score_case(case: AgentEvaluationCase, task: Task, artifacts: list[Artifact], runtime_error: str = "") -> dict:
    """以可复现规则检查任务结果、证据、成果、工具边界和子任务路由。"""

    state = task.state
    observations = list(state.get("observations", []))
    tools = [row.get("tool", "") for row in observations]
    evidence_ids = {
        row.get("evidence_id")
        for row in observations
        if row.get("status") == "success" and isinstance(row.get("evidence_id"), str)
    }
    subtasks = list((state.get("subtasks") or {}).values())
    roles = {row.get("role") for row in subtasks if isinstance(row.get("role"), str)}
    expected = case.expected
    forbidden = sorted(set(expected["forbidden_tools"]) & set(tools))
    required_roles = set(expected["required_subtask_roles"])
    checks = {
        "status": not runtime_error and task.status in set(expected["valid_statuses"]),
        "answer": bool(str(state.get("answer", "")).strip()),
        "evidence": len(evidence_ids) >= expected["min_evidence"],
        "artifact": not expected["require_artifact"] or bool(artifacts),
        "forbidden_tools": not forbidden,
        "routing": required_roles <= roles and len(subtasks) <= expected["max_subtasks"],
    }
    return {
        "checks": checks,
        "score": round(sum(checks.values()) / len(checks), 4),
        "passed": all(checks.values()),
        "runtime_error": runtime_error,
        "evidence_count": len(evidence_ids),
        "artifact_count": len(artifacts),
        "tools": tools,
        "forbidden_tools": forbidden,
        "subtask_roles": sorted(roles),
        "subtask_count": len(subtasks),
    }


def aggregate_metrics(reports: list[dict]) -> dict[str, float]:
    """按运行次数聚合自动评分结果，避免完成状态掩盖证据或路由失败。"""

    if not reports:
        return {}
    required_artifact = [row for row in reports if row["expected"]["require_artifact"]]
    return {
        "deterministic_pass_rate": _mean(row["automatic"]["passed"] for row in reports),
        "average_check_score": _mean(row["automatic"]["score"] for row in reports),
        "completion_rate": _mean(row["status"] == "completed" for row in reports),
        "evidence_coverage_rate": _mean(row["automatic"]["checks"]["evidence"] for row in reports),
        "artifact_success_rate": _mean(
            row["automatic"]["checks"]["artifact"] for row in required_artifact
        )
        if required_artifact
        else 1.0,
        "routing_success_rate": _mean(row["automatic"]["checks"]["routing"] for row in reports),
        "forbidden_tool_violation_rate": _mean(
            not row["automatic"]["checks"]["forbidden_tools"] for row in reports
        ),
        "p95_model_calls": _percentile([row["usage"].get("model_calls", 0) for row in reports], 0.95),
        "p95_tool_calls": _percentile([row["usage"].get("tool_calls", 0) for row in reports], 0.95),
        "p95_active_seconds": _percentile([row["usage"].get("active_seconds", 0) for row in reports], 0.95),
    }


def quality_gate_failures(metrics: dict[str, float], dataset: AgentEvaluationDataset) -> list[str]:
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


def baseline_failures(metrics: dict[str, float], dataset: AgentEvaluationDataset) -> list[str]:
    baseline = load_baseline(dataset)
    if baseline is None:
        return []
    values, tolerance = baseline
    failures = []
    for name, gate in dataset.quality_gates.items():
        if name not in metrics or name not in values:
            continue
        if "minimum" in gate and metrics[name] < values[name] - tolerance:
            failures.append(f"{name} 相比基线下降超过 {tolerance:.2f}")
        if "maximum" in gate and metrics[name] > values[name] + tolerance:
            failures.append(f"{name} 相比基线上升超过 {tolerance:.2f}")
    return failures


async def evaluate(args) -> dict:
    """运行选中的真实 Agent 案例并写入逐题报告、指标和门槛结论。"""

    settings = get_settings()
    if not settings.llm_enabled:
        raise SystemExit("未配置真实模型。请配置项目根目录 .env；协议测试不能替代真实模型评测。")
    dataset = load_agent_evaluation_dataset(args.dataset)
    selected = _select_cases(dataset, args.case, args.limit)
    repeat = args.repeat or dataset.default_repeat
    if not 1 <= repeat <= 10:
        raise SystemExit("repeat 必须是 1 至 10 的整数")
    output = Path(args.output).resolve() / f"agent-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    output.mkdir(parents=True, exist_ok=False)
    app_url, commerce_admin_url, commerce_reader_url = evaluation_urls("AGENT_EVALUATION", settings)
    reports: list[dict] = []
    for case in selected:
        for run_number in range(1, repeat + 1):
            report = await _run_case(
                case,
                run_number,
                settings,
                app_url,
                commerce_admin_url,
                commerce_reader_url,
                args,
            )
            reports.append(report)
            (output / f"{case.id}-{run_number}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"{case.id} run={run_number} status={report['status']} score={report['automatic']['score']}",
                flush=True,
            )
    metrics = aggregate_metrics(reports)
    gate_failures = quality_gate_failures(metrics, dataset)
    regression_failures = baseline_failures(metrics, dataset)
    result = {
        "dataset": {"version": dataset.version, "case_count": len(selected), "repeat": repeat},
        "model": {"provider": settings.llm_provider, "name": settings.llm_model},
        "metrics": metrics,
        "quality_gates": dataset.quality_gates,
        "gate_failures": gate_failures,
        "baseline_failures": regression_failures,
        "passed": not gate_failures and not regression_failures,
        "cases": reports,
    }
    _write_report(output, result)
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, ensure_ascii=False))
    return result


async def _run_case(
    case: AgentEvaluationCase,
    run_number: int,
    settings,
    app_url: str,
    commerce_admin_url: str,
    commerce_reader_url: str,
    args,
) -> dict:
    await reset_application_database(app_url)
    await reset_commerce_database(commerce_admin_url)
    await seed_database(commerce_admin_url, days=60, sku_count=32, order_target=2500, scenario=case.scenario)
    await grant_commerce_read_access(commerce_admin_url, commerce_reader_url)
    config = settings.model_copy(
        update={
            "app_database_url": app_url,
            "commerce_admin_url": commerce_admin_url,
            "commerce_database_url": commerce_reader_url,
            **({"max_model_calls": args.max_model_calls} if args.max_model_calls else {}),
            **({"max_tool_calls": args.max_tool_calls} if args.max_tool_calls else {}),
        }
    )
    database = Database(config)
    task_id = ""
    runtime_error = ""
    try:
        async with database.sessions() as session:
            user = User(id="evaluation-user", username="evaluation-user", password_hash="disabled")
            session.add(user)
            await session.flush()
            task = Task(user_id=user.id, goal=case.goal, state=initial_state(case.goal, config))
            session.add(task)
            await session.commit()
            task_id = task.id
        try:
            await AgentRuntime(database, config).run(task_id)
        except Exception as error:
            runtime_error = f"{type(error).__name__}: {error}"[:1200]
        async with database.sessions() as session:
            task = await session.get(Task, task_id)
            artifacts = list(await session.scalars(select(Artifact).where(Artifact.task_id == task_id)))
            automatic = score_case(case, task, artifacts, runtime_error)
            return {
                "case_id": case.id,
                "category": case.category,
                "repeat": run_number,
                "goal": case.goal,
                "rubric": case.rubric,
                "expected": case.expected,
                "status": task.status,
                "usage": task.state["usage"],
                "plan_versions": len(task.state.get("plans", [])),
                "observations": task.state.get("observations", []),
                "subtasks": task.state.get("subtasks", {}),
                "artifacts": [artifact.id for artifact in artifacts],
                "answer": task.state.get("answer", ""),
                "automatic": automatic,
                "human_review": {
                    "verdict": "pending",
                    "numeric_correctness": None,
                    "evidence_support": None,
                    "goal_coverage": None,
                    "notes": "",
                },
            }
    finally:
        await database.close()


def _select_cases(dataset: AgentEvaluationDataset, case_ids: str, limit: int) -> list[AgentEvaluationCase]:
    if case_ids:
        requested = {item.strip() for item in case_ids.split(",") if item.strip()}
        selected = [case for case in dataset.cases if case.id in requested]
        missing = requested - {case.id for case in selected}
        if missing:
            raise SystemExit(f"未找到评测案例：{', '.join(sorted(missing))}")
        return selected
    if limit < 0:
        raise SystemExit("limit 不能为负数")
    return list(dataset.cases[:limit] if limit else dataset.cases)


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return float(ordered[index])


def _write_report(output: Path, result: dict) -> None:
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Plan-and-Execute 真实模型评测",
        "",
        f"- 数据集：`{result['dataset']['version']}`；案例数：{result['dataset']['case_count']}；重复次数：{result['dataset']['repeat']}",
        f"- 结果：{'通过' if result['passed'] else '未通过'}",
        "",
        "## 自动评分指标",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        *[f"| {name} | {value:.4f} |" for name, value in result["metrics"].items()],
        "",
        "## 逐题结果",
        "",
        "| 案例 | 状态 | 自动评分 | 结果 |",
        "| --- | --- | ---: | --- |",
        *[
            f"| {row['case_id']} | {row['status']} | {row['automatic']['score']:.4f} | {'通过' if row['automatic']['passed'] else '未通过'} |"
            for row in result["cases"]
        ],
    ]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--case", default="")
    parser.add_argument("--output", default="evaluation-reports")
    parser.add_argument("--max-model-calls", type=int, default=0)
    parser.add_argument("--max-tool-calls", type=int, default=0)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()
    dataset = load_agent_evaluation_dataset(args.dataset)
    if args.list:
        for case in dataset.cases:
            print(f"{case.id} [{case.category}] {case.goal}")
        print(f"Total: {len(dataset.cases)}")
        return
    if args.write_baseline and (args.case or args.limit):
        parser.error("--write-baseline 只能对完整评测集执行，不能与 --case 或 --limit 一起使用")
    result = asyncio.run(evaluate(args))
    if args.write_baseline:
        if not result["passed"]:
            raise SystemExit("评测未通过，拒绝写入基线")
        print(f"已写入基线：{write_baseline(dataset, result['metrics'])}")


if __name__ == "__main__":
    main()
