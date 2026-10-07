"""Tavily 联网搜索的受控适配层。"""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx


class WebSearchError(ValueError):
    """联网搜索不可用或返回无效结果。"""


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_url(value: Any) -> str:
    url = str(value or "").strip()
    parts = urlsplit(url)
    return url if parts.scheme in {"http", "https"} and parts.netloc else ""


def _normalize_results(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows = []
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        url = _clean_url(item.get("url"))
        title = _clean_text(item.get("title"), 240)
        snippet = _clean_text(item.get("content"), 1600)
        if not url or not title or not snippet:
            continue
        row = {"title": title, "url": url, "snippet": snippet}
        published_date = _clean_text(item.get("published_date"), 80)
        if published_date:
            row["published_date"] = published_date
        score = item.get("score")
        if isinstance(score, (int, float)):
            row["score"] = round(float(score), 4)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


async def tavily_search(
    query: str,
    limit: int,
    settings,
    *,
    post: Callable[..., Awaitable[httpx.Response]] | None = None,
) -> dict[str, Any]:
    """调用 Tavily Search API，并仅返回可审计的检索元数据。"""
    if not settings.tavily_api_key:
        raise WebSearchError("未配置 Tavily API 密钥，无法执行联网搜索")

    request_body = {
        "query": query,
        "max_results": limit,
        "search_depth": settings.tavily_search_depth,
        "include_answer": False,
        "include_raw_content": False,
    }
    headers = {"Authorization": f"Bearer {settings.tavily_api_key}"}
    if post is None:
        timeout = httpx.Timeout(settings.tavily_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post("https://api.tavily.com/search", json=request_body, headers=headers)
    else:
        response = await post("https://api.tavily.com/search", json=request_body, headers=headers)

    if response.status_code in {401, 403}:
        raise WebSearchError("Tavily API 密钥无效或无权访问联网搜索")
    if response.status_code == 429:
        raise WebSearchError("Tavily 联网搜索请求过于频繁，请稍后重试")
    if response.status_code >= 500:
        raise WebSearchError("Tavily 联网搜索服务暂时不可用，请稍后重试")
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WebSearchError("Tavily 联网搜索返回无效结果") from exc
    if not isinstance(payload, dict):
        raise WebSearchError("Tavily 联网搜索返回无效结果")

    results = _normalize_results(payload, limit)
    return {
        "provider": "tavily",
        "query": query,
        "results": results,
        "absence_notice": "当前检索条件下未找到可用公开网页来源" if not results else None,
    }
