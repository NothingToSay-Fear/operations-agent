"""真实检索模型的隔离评测，输出逐题排名、无答案误命中和阶段耗时。"""

import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
import time

from sqlalchemy import select

from app import knowledge, knowledge_service, memory, retrieval
from app.agent.runtime import initial_state
from app.config import Settings
from app.db import Database
from app.evaluation_database import (
    evaluation_urls,
    grant_commerce_read_access,
    reset_application_database,
    reset_commerce_database,
)
from app.models import Document, Task, User
from app.extension_models import HistoryUnit
from app.seed import seed_database


DOCUMENTS = {
    "售后": "# 青松系列售后政策\n## 无理由退货\n青松系列未拆封商品自签收之日起七天内可申请无理由退货。已拆封食品不支持无理由退货。质量问题须保留批次号和照片。",
    "补货": "# 青松系列补货规则\n库存覆盖天数低于十四天时启动补货评估。标准采购交期为十天，最小订货量为二十件。计算时应扣除已确认的在途数量，隔离品不得计入可用库存。",
    "品牌": "# 文案规范\n青松系列标题必须包含品类、净含量和口味。没有检测依据时，禁止宣称治疗疾病或降低血糖。商品卖点应依据可验证属性，不得编造功效。",
    "促销": "# 青松系列促销约束\n活动折扣后商品毛利率不得低于百分之二十五。优惠券和直降可以叠加，但须合并计算实际到手价与毛利；发货运费另行评估。",
    "仓储": "# 仓库管理\n食品采用先到期先出。剩余保质期不足三十天的商品应单独隔离并审核，不直接参加常规促销。仓库盘点差异由仓库负责人核查。",
    "物流": "# 青松系列配送\n常规订单在付款后四十八小时内发货。偏远地区配送时效为五至七天。节假日延迟须在活动方案中说明。",
    "财务": "# 结算规范\n支付商品 GMV 不包含运费，也不扣退款。退款以实际到账日统计。周期访客应按访客标识去重，不能直接累加每天的访客数。",
    "投放": "# 广告归因\n广告平台展示的归因收入仅供渠道参考。多个渠道同时声称归因同一订单时，不得直接相加作为实际商品成交额。",
    "供应商": "# 供应商交期\n白鹭供应商提供的交期为二十一天。活动前需要向供应商确认产能，在未确认之前不得把预计到货当成已经到货。",
    "包装": "# 包装规范\n玻璃瓶发货使用独立缓冲套和防撞隔板。运输破损须保留外包装与商品照片。包装材料重量不计入商品净含量。",
}
CASES = [
    ("青松没拆封，收到后多少天还能无理由退？", "售后"),
    ("青松商品补货需要提前多久，起订数量是多少？", "补货"),
    ("库存不足两周时如何处理已经在路上的货？", "补货"),
    ("青松商品标题必须写出哪些信息？", "品牌"),
    ("文案能不能宣称降血糖？", "品牌"),
    ("优惠券与直降叠加后需要守住什么毛利底线？", "促销"),
    ("临近保质期的食品能否正常参加促销？", "仓储"),
    ("青松订单付款后多久发出？", "物流"),
    ("退款和运费在支付GMV里怎么计算？", "财务"),
    ("能把不同广告平台的归因收入直接加起来吗？", "投放"),
    ("白鹭供应商交货要等多少天？", "供应商"),
    ("玻璃瓶需要怎样防撞包装？", "包装"),
    ("火星探测器轨道倾角如何计算？", None),
    ("请提供量子计算机纠错码的证明。", None),
]


async def run(directory):
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
    results = []
    try:
        model_health = await retrieval.health(settings)
        if model_health["embedding"] != "ready" or model_health["reranker"] != "ready":
            raise RuntimeError("真实模型加载失败，请检查隔离依赖及模型目录")
        async with db.sessions() as session:
            user = User(username="rag-evaluation-" + str(time.time_ns()), password_hash="不可登录")
            session.add(user)
            await session.flush()
            ids = {}
            for label, content in DOCUMENTS.items():
                doc = Document(user_id=user.id, title=label, content=content)
                session.add(doc)
                await session.flush()
                await knowledge.index_document(session, doc, settings)
                ids[label] = doc.id
            await session.commit()
            eligible_rows = [
                knowledge_service.as_row(*row)
                for row in (await session.execute(knowledge_service.eligible(user.id))).all()
            ]
            eligible_by_segment = {row["id"]: row for row in eligible_rows}
            for query, label in CASES:
                async with db.read_sessions() as commerce:
                    result = await knowledge.search(session, commerce, user.id, query, settings, 5)
                    sparse = await knowledge.search(
                        session,
                        commerce,
                        user.id,
                        query,
                        settings.model_copy(update={"embedding_model_path": "", "reranker_model_path": ""}),
                        5,
                    )
                expected = ids.get(label)
                hits = [r["document_id"] for r in result["rows"]]
                sparse_hits = [r["document_id"] for r in sparse["rows"]]
                bm25_hits = [
                    eligible_by_segment[segment_id]["document_id"]
                    for segment_id in retrieval.lexical_rank(query, eligible_rows, limit=5)
                ]
                rank = hits.index(expected) + 1 if expected in hits else None
                results.append(
                    {
                        "query": query,
                        "expected": label,
                        "rank": rank,
                        "bm25_rank": bm25_hits.index(expected) + 1 if expected in bm25_hits else None,
                        "ts_rank_cd_rank": sparse_hits.index(expected) + 1
                        if expected in sparse_hits
                        else None,
                        "no_answer_false_positive": bool(hits) if label is None else None,
                        "bm25_no_answer_false_positive": bool(bm25_hits) if label is None else None,
                        "ts_rank_cd_no_answer_false_positive": bool(sparse_hits) if label is None else None,
                        "mode": result["retrieval"],
                        "elapsed_ms": result["elapsed_ms"],
                        "stage_ms": result["stage_ms"],
                        "trace": result["trace"],
                    }
                )
            first = Task(user_id=user.id, goal="青松历史", state=initial_state("青松历史", settings))
            second = Task(user_id=user.id, goal="白鹭历史", state=initial_state("白鹭历史", settings))
            session.add_all([first, second])
            await session.flush()
            await memory.append_history(
                session, first, 0, "user", {"content": "青松独立历史，库存优先考虑交期"}, settings
            )
            await memory.append_history(
                session, second, 0, "user", {"content": "白鹭独立历史，库存优先考虑交期"}, settings
            )
            await session.commit()
            history_units = list(
                await session.scalars(
                    select(HistoryUnit).where(HistoryUnit.task_id.in_([first.id, second.id]))
                )
            )
            history_vectors = await retrieval.encode([unit.content for unit in history_units], settings)
            for unit, vector in zip(history_units, history_vectors):
                unit.embedding, unit.model_id = vector, settings.embedding_model_id
            await session.commit()
            history = await memory.history_search(session, first, "库存交期", settings)
            assert history["rows"] and all(row["task_id"] == first.id for row in history["rows"])
            assert "白鹭" not in str(history)
        answered = [r for r in results if r["expected"]]
        recall = sum(r["rank"] is not None for r in answered) / len(answered)
        mrr = sum(1 / r["rank"] if r["rank"] else 0 for r in answered) / len(answered)
        bm25_recall = sum(r["bm25_rank"] is not None for r in answered) / len(answered)
        bm25_mrr = sum(1 / r["bm25_rank"] if r["bm25_rank"] else 0 for r in answered) / len(answered)
        ts_rank_cd_recall = sum(r["ts_rank_cd_rank"] is not None for r in answered) / len(answered)
        ts_rank_cd_mrr = sum(1 / r["ts_rank_cd_rank"] if r["ts_rank_cd_rank"] else 0 for r in answered) / len(
            answered
        )
        negatives = [r for r in results if not r["expected"]]
        report = {
            "database": "PostgreSQL",
            "models": model_health,
            "cases": results,
            "recall_at_5": recall,
            "mrr": mrr,
            "bm25_recall_at_5": bm25_recall,
            "bm25_mrr": bm25_mrr,
            "bm25_no_answer_false_positive_rate": sum(r["bm25_no_answer_false_positive"] for r in negatives)
            / len(negatives),
            "ts_rank_cd_recall_at_5": ts_rank_cd_recall,
            "ts_rank_cd_mrr": ts_rank_cd_mrr,
            "ts_rank_cd_no_answer_false_positive_rate": sum(
                r["ts_rank_cd_no_answer_false_positive"] for r in negatives
            )
            / len(negatives),
            "no_answer_false_positive_rate": sum(r["no_answer_false_positive"] for r in negatives)
            / len(negatives),
            "history_isolation": True,
            "passed": recall >= 0.9 and all(not r["no_answer_false_positive"] for r in negatives),
        }
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (directory / "report.md").write_text(
            f"# 真实 RAG 评测\n\n数据库：{report['database']}\n\n"
            f"混合检索 Recall@5：{recall:.3f}；MRR：{mrr:.3f}。\n\n"
            f"BM25 Recall@5：{bm25_recall:.3f}；MRR：{bm25_mrr:.3f}。\n\n"
            + f"ts_rank_cd Recall@5：{ts_rank_cd_recall:.3f}；MRR：{ts_rank_cd_mrr:.3f}。\n\n"
            + f"无答案误命中率：{report['no_answer_false_positive_rate']:.3f}\n\n"
            f"通过：{report['passed']}。逐题排名与轨迹见 report.json。\n",
            encoding="utf-8",
        )
        print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False))
        return report["passed"]
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="真实 RAG 与任务历史隔离评测")
    parser.add_argument("--output", default="evaluation-reports/rag-" + str(time.time_ns()))
    args = parser.parse_args()
    passed = asyncio.run(run(Path(args.output).resolve()))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
