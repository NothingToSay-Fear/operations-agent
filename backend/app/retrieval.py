"""分词、向量编码、融合和精排的检索公共能力。"""

import asyncio
from functools import lru_cache
import math
import re

import jieba
from rank_bm25 import BM25Okapi


def tokens(text):
    return [w.lower() for w in jieba.lcut(text) if re.fullmatch(r"[\w\u4e00-\u9fff]+", w)]


@lru_cache(maxsize=2)
def embedding_model(path):
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(2)
    return SentenceTransformer(path, local_files_only=True, device="cpu")


@lru_cache(maxsize=1)
def reranker_model(path):
    import torch
    from sentence_transformers import CrossEncoder

    torch.set_num_threads(2)
    return CrossEncoder(
        path,
        local_files_only=True,
        device="cpu",
        max_length=512,
        default_activation_function=torch.nn.Identity(),
    )


async def encode(texts, settings):
    if not settings.embedding_model_path:
        return None
    vectors = await asyncio.to_thread(
        lambda: (
            embedding_model(settings.embedding_model_path)
            .encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
            .tolist()
        )
    )
    if any(len(vector) != 512 for vector in vectors):
        raise ValueError("当前向量索引要求 512 维；更换维度前必须迁移索引")
    return vectors


def lexical_rank(query, rows, text_key="text", limit=40):
    # 本地兼容路径使用词面排序；PostgreSQL 正式链路的词面召回由 ts_rank_cd 执行。
    query_tokens = tokens(query)
    corpus = [tokens(row[text_key]) or ["空"] for row in rows]
    if not rows or not query_tokens:
        return []
    scores = BM25Okapi(corpus).get_scores(query_tokens)
    overlap = set(query_tokens)
    return [
        rows[i]["id"]
        for i in sorted(range(len(rows)), key=lambda i: (-scores[i], rows[i]["id"]))
        if overlap.intersection(corpus[i])
    ][:limit]


def fuse(rankings):
    # RRF 只融合候选排名，不混入不同检索器不可比的原始分数。
    scores = {}
    for ranking in rankings:
        for rank, item_id in enumerate(dict.fromkeys(ranking), 1):
            scores[item_id] = scores.get(item_id, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return -1
    return sum(x * y for x, y in zip(a, b)) / max(
        1e-9, math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    )


async def rerank(query, rows, settings, limit=5):
    # 精排只处理有限候选集，控制单次模型推理延迟和上下文成本。
    if not rows or not settings.reranker_model_path:
        return rows[:limit], False
    values = await asyncio.to_thread(
        lambda: (
            reranker_model(settings.reranker_model_path)
            .predict([(query, r["text"]) for r in rows], batch_size=8, show_progress_bar=False)
            .tolist()
        )
    )
    # BGE 输出原始分数，转为概率后应用可评测阈值。
    scored = [(1 / (1 + math.exp(-max(-80, min(80, float(v))))), row) for v, row in zip(values, rows)]
    scored.sort(key=lambda pair: -pair[0])
    return [{**row, "relevance": score} for score, row in scored if score >= settings.retrieval_min_score][
        :limit
    ], True


async def health(settings):
    result = {"embedding": "未配置", "reranker": "未配置", "mode": "词面降级"}
    try:
        if settings.embedding_model_path:
            await encode(["模型健康检查"], settings)
            result["embedding"] = "ready"
        if settings.reranker_model_path:
            await rerank("库存", [{"text": "库存补货规则"}], settings)
            result["reranker"] = "ready"
    except Exception:
        result["error"] = "本地模型未就绪，请检查目录及依赖"
    if result["embedding"] == result["reranker"] == "ready":
        result["mode"] = "混合召回与精排"
    return result
