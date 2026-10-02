"""使用真实解析、索引与 PostgreSQL 召回链路的 RAG 评测。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
import time

from sqlalchemy import select

from app import knowledge, memory, retrieval
from app.agent.runtime import initial_state
from app.config import Settings
from app.db import Database
from app.evaluation_database import (
    evaluation_urls,
    grant_commerce_read_access,
    reset_application_database,
    reset_commerce_database,
)
from app.extension_models import BackgroundJob, DocumentScope, HistoryUnit
from app.models import Document, Task, User
from app.rag_evaluation_dataset import (
    RagEvaluationCase,
    RagEvaluationDataset,
    load_baseline,
    load_rag_evaluation_dataset,
    write_baseline,
)
from app.seed import seed_database


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "rag" / "v1"


async def run(directory: Path, dataset_path: Path = DEFAULT_DATASET) -> dict:
    """运行真实文件上传、结构化索引和混合检索评测。"""
    dataset = load_rag_evaluation_dataset(dataset_path)
    settings = Settings()
    if not settings.embedding_model_path or not settings.reranker_model_path:
        raise RuntimeError("评测要求配置真实向量与精排模型目录")
    directory.mkdir(parents=True, exist_ok=False)
    app_url, commerce_admin_url, commerce_reader_url = evaluation_urls("RAG_EVALUATION", settings)
    await reset_application_database(app_url, retrieval_indexes=True)
    await reset_commerce_database(commerce_admin_url)
    settings = settings.model_copy(
        update={
            "app_database_url": app_url,
            "commerce_admin_url": commerce_admin_url,
            "commerce_database_url": commerce_reader_url,
        }
    )
    await seed_database(
        commerce_admin_url,
        days=28,
        sku_count=12,
        order_target=100,
        as_of=date(2026, 9, 28),
    )
    await grant_commerce_read_access(commerce_admin_url, commerce_reader_url)
    db = Database(settings)
    try:
        model_health = await retrieval.health(settings)
        if model_health["embedding"] != "ready" or model_health["reranker"] != "ready":
            raise RuntimeError("真实模型加载失败，请检查隔离依赖及模型目录")
        async with db.sessions() as session:
            user = User(username="rag-evaluation-" + str(time.time_ns()), password_hash="不可登录")
            session.add(user)
            await session.flush()
            document_ids = await _index_dataset_documents(session, user, dataset, settings)
            await session.commit()
            results = [
                await _evaluate_case(
                    session,
                    db,
                    user.id,
                    case,
                    document_ids,
                    settings,
                    dataset.top_k,
                )
                for case in dataset.cases
            ]
            history_isolation = await _verify_history_isolation(session, user.id, settings)
            await session.commit()
        metrics = _aggregate_metrics(results)
        metrics["history_isolation"] = float(history_isolation)
        gate_failures = _quality_gate_failures(metrics, dataset)
        baseline_failures = _baseline_regression_failures(metrics, dataset)
        report = {
            "database": "PostgreSQL",
            "dataset": {
                "version": dataset.version,
                "path": str(dataset.root),
                "document_count": len(dataset.documents),
                "case_count": len(dataset.cases),
                "top_k": dataset.top_k,
            },
            "models": model_health,
            "metrics": metrics,
            "quality_gates": dataset.quality_gates,
            "gate_failures": gate_failures,
            "baseline_failures": baseline_failures,
            "cases": results,
            "passed": not gate_failures and not baseline_failures and history_isolation,
        }
        _write_report(directory, report)
        print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
        return report
    finally:
        await db.close()


async def _index_dataset_documents(
    session, user: User, dataset: RagEvaluationDataset, settings: Settings
) -> dict[str, str]:
    """复用正式上传后的版本化索引流程，避免评测绕过文件解析。"""
    document_ids: dict[str, str] = {}
    for source in dataset.documents:
        document = Document(user_id=user.id, title=source.title, content="")
        session.add(document)
        await session.flush()
        session.add(DocumentScope(document_id=document.id))
        version = await knowledge.enqueue_version(
            session, document, source.path.name, source.path.read_bytes()
        )
        prepared = await knowledge.prepare_index(version, settings)
        await knowledge.publish_index(session, version, prepared)
        job = await session.scalar(select(BackgroundJob).where(BackgroundJob.key == "index:" + version.id))
        if job is None:
            raise RuntimeError(f"评测资料缺少索引任务：{source.id}")
        job.status, job.progress, job.error = "completed", 100, prepared["error"]
        document_ids[source.id] = document.id
    return document_ids


async def _evaluate_case(
    session,
    db: Database,
    user_id: str,
    case: RagEvaluationCase,
    document_ids: dict[str, str],
    settings: Settings,
    top_k: int,
) -> dict:
    """记录混合检索与 PostgreSQL 全文检索两条路径的逐题结果。"""
    async with db.read_sessions() as commerce:
        result = await knowledge.search(session, commerce, user_id, case.question, settings, top_k)
        sparse = await knowledge.search(
            session,
            commerce,
            user_id,
            case.question,
            settings.model_copy(update={"embedding_model_path": "", "reranker_model_path": ""}),
            top_k,
        )
    expected_ids = {document_ids[item] for item in case.expected_document_ids}
    rows = result["rows"]
    hits = [row["document_id"] for row in rows]
    sparse_hits = [row["document_id"] for row in sparse["rows"]]
    first_rank = min(
        (index + 1 for index, identifier in enumerate(hits) if identifier in expected_ids), default=None
    )
    anchor_matches = {
        anchor: any(row["document_id"] in expected_ids and anchor in row["text"] for row in rows)
        for anchor in case.expected_anchors
    }
    return {
        "id": case.id,
        "question": case.question,
        "expected_document_ids": list(case.expected_document_ids),
        "expected_anchors": list(case.expected_anchors),
        "rank": first_rank,
        "ts_rank_cd_rank": min(
            (index + 1 for index, identifier in enumerate(sparse_hits) if identifier in expected_ids),
            default=None,
        ),
        "document_ok": expected_ids.issubset(set(hits)) if expected_ids else not hits,
        "anchor_matches": anchor_matches,
        "anchor_ok": all(anchor_matches.values()),
        "no_answer_false_positive": bool(hits) if not expected_ids else None,
        "ts_rank_cd_no_answer_false_positive": bool(sparse_hits) if not expected_ids else None,
        "mode": result["retrieval"],
        "elapsed_ms": result["elapsed_ms"],
        "stage_ms": result["stage_ms"],
        "trace": result["trace"],
    }


async def _verify_history_isolation(session, user_id: str, settings: Settings) -> bool:
    """保留任务内历史召回隔离校验，防止 RAG 评测掩盖越权召回。"""
    first = Task(user_id=user_id, goal="青松历史", state=initial_state("青松历史", settings))
    second = Task(user_id=user_id, goal="白鹭历史", state=initial_state("白鹭历史", settings))
    session.add_all([first, second])
    await session.flush()
    await memory.append_history(
        session, first, 0, "user", {"content": "青松独立历史，库存优先考虑交期"}, settings
    )
    await memory.append_history(
        session, second, 0, "user", {"content": "白鹭独立历史，库存优先考虑交期"}, settings
    )
    await session.flush()
    units = list(
        await session.scalars(select(HistoryUnit).where(HistoryUnit.task_id.in_([first.id, second.id])))
    )
    vectors = await retrieval.encode([unit.content for unit in units], settings)
    for unit, vector in zip(units, vectors, strict=True):
        unit.embedding, unit.model_id = vector, settings.embedding_model_id
    history = await memory.history_search(session, first, "库存交期", settings)
    return (
        bool(history["rows"])
        and all(row["task_id"] == first.id for row in history["rows"])
        and "白鹭" not in str(history)
    )


def _aggregate_metrics(results: list[dict]) -> dict[str, float]:
    answered = [result for result in results if result["expected_document_ids"]]
    negatives = [result for result in results if not result["expected_document_ids"]]
    anchors = [matched for result in answered for matched in result["anchor_matches"].values()]
    return {
        "recall_at_5": _mean(result["rank"] is not None for result in answered),
        "mrr": _mean(1 / result["rank"] if result["rank"] else 0 for result in answered),
        "anchor_recall_at_5": _mean(anchors),
        "ts_rank_cd_recall_at_5": _mean(result["ts_rank_cd_rank"] is not None for result in answered),
        "ts_rank_cd_mrr": _mean(
            1 / result["ts_rank_cd_rank"] if result["ts_rank_cd_rank"] else 0 for result in answered
        ),
        "ts_rank_cd_no_answer_false_positive_rate": _mean(
            result["ts_rank_cd_no_answer_false_positive"] for result in negatives
        ),
        "no_answer_false_positive_rate": _mean(result["no_answer_false_positive"] for result in negatives),
        "p95_elapsed_ms": _percentile([result["elapsed_ms"] for result in results], 0.95),
    }


def _quality_gate_failures(metrics: dict[str, float], dataset: RagEvaluationDataset) -> list[str]:
    failures: list[str] = []
    for name, gate in dataset.quality_gates.items():
        value = metrics.get(name)
        if value is None:
            failures.append(f"缺少质量指标：{name}")
            continue
        if "minimum" in gate and value < gate["minimum"]:
            failures.append(f"{name}={value:.4f} 低于最低门槛 {gate['minimum']:.4f}")
        if "maximum" in gate and value > gate["maximum"]:
            failures.append(f"{name}={value:.4f} 高于最高门槛 {gate['maximum']:.4f}")
    return failures


def _baseline_regression_failures(metrics: dict[str, float], dataset: RagEvaluationDataset) -> list[str]:
    baseline = load_baseline(dataset)
    if baseline is None:
        return []
    baseline_metrics, tolerance = baseline
    failures: list[str] = []
    for name, gate in dataset.quality_gates.items():
        if name not in baseline_metrics or name not in metrics:
            continue
        value, previous = metrics[name], baseline_metrics[name]
        if "minimum" in gate and value < previous - tolerance:
            failures.append(f"{name}={value:.4f} 相比基线 {previous:.4f} 下降超过 {tolerance:.4f}")
        if "maximum" in gate and value > previous + tolerance:
            failures.append(f"{name}={value:.4f} 相比基线 {previous:.4f} 上升超过 {tolerance:.4f}")
    return failures


def _write_report(directory: Path, report: dict) -> None:
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    metrics = report["metrics"]
    failures = [*report["gate_failures"], *report["baseline_failures"]]
    failure_lines = [f"- {failure}" for failure in failures] or ["- 无"]
    lines = [
        "# 真实 RAG 评测",
        "",
        f"- 数据集：`{report['dataset']['version']}`",
        f"- 资料数：{report['dataset']['document_count']}；问题数：{report['dataset']['case_count']}；Top-K：{report['dataset']['top_k']}",
        f"- 数据库：{report['database']}",
        f"- 结果：{'通过' if report['passed'] else '未通过'}",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        *[f"| {name} | {value:.4f} |" for name, value in metrics.items()],
        "",
        "## 未通过项",
        "",
        *failure_lines,
        "",
        "## 逐题结果",
        "",
        "| ID | 文档命中 | 文本锚点 | 排名 |",
        "| --- | --- | --- | ---: |",
        *[
            f"| {item['id']} | {'是' if item['document_ok'] else '否'} | {'是' if item['anchor_ok'] else '否'} | {item['rank'] or '-'} |"
            for item in report["cases"]
        ],
        "",
    ]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _mean(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return float(ordered[index])


def main() -> None:
    parser = argparse.ArgumentParser(description="真实文件上传、混合检索与文本锚点隔离评测")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="版本化评测集目录")
    parser.add_argument("--output", default="evaluation-reports/rag-" + str(time.time_ns()))
    parser.add_argument("--write-baseline", action="store_true", help="将通过门槛的本次结果写为基线")
    args = parser.parse_args()
    report = asyncio.run(run(Path(args.output).resolve(), args.dataset))
    if args.write_baseline:
        if not report["passed"]:
            raise SystemExit("评测未通过，拒绝写入基线")
        dataset = load_rag_evaluation_dataset(args.dataset)
        print(f"已写入基线：{write_baseline(dataset, report['metrics'])}")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
