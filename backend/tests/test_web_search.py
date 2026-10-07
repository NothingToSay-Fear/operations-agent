import httpx
import pytest

from app.agent.tools import catalog
from app.agent.runtime import compact
from app.config import Settings
from app.web_search import WebSearchError, tavily_search


async def test_tavily_search_only_keeps_auditable_results():
    received = {}

    async def post(url, *, json, headers):
        received.update(url=url, body=json, headers=headers)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": " 平台活动公告 ",
                        "url": "https://example.com/rule",
                        "content": " 活动规则与发布时间 ",
                        "published_date": "2026-10-01",
                        "score": 0.98765,
                    },
                    {"title": "不安全链接", "url": "ftp://example.com", "content": "不应保留"},
                    {"title": "无摘要", "url": "https://example.com/empty", "content": ""},
                ]
            },
            request=httpx.Request("POST", url),
        )

    result = await tavily_search(
        "平台活动规则",
        3,
        Settings(tavily_api_key="tvly-fixture"),
        post=post,
    )

    assert received["url"] == "https://api.tavily.com/search"
    assert received["headers"]["Authorization"] == "Bearer tvly-fixture"
    assert received["body"] == {
        "query": "平台活动规则",
        "max_results": 3,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    assert result["provider"] == "tavily"
    assert result["absence_notice"] is None
    assert result["results"] == [
        {
            "title": "平台活动公告",
            "url": "https://example.com/rule",
            "snippet": "活动规则与发布时间",
            "published_date": "2026-10-01",
            "score": 0.9877,
        }
    ]


async def test_tavily_search_rejects_invalid_key():
    async def post(url, *, json, headers):
        return httpx.Response(401, request=httpx.Request("POST", url))

    with pytest.raises(WebSearchError, match="密钥无效"):
        await tavily_search("平台规则", 3, Settings(tavily_api_key="invalid"), post=post)


def test_web_search_is_hidden_without_a_tavily_key():
    disabled = {item["name"] for item in catalog(settings=Settings(tavily_api_key=""))}
    enabled = {item["name"] for item in catalog(settings=Settings(tavily_api_key="tvly-fixture"))}

    assert "search_web" not in disabled
    assert "search_web" in enabled


def test_web_observation_keeps_multiple_sources_within_context_budget():
    result = compact(
        {
            "evidence_id": "ev_web",
            "tool": "search_web",
            "status": "success",
            "data": {
                "provider": "tavily",
                "query": "平台活动规则",
                "results": [
                    {"title": "来源一", "url": "https://example.com/1", "snippet": "一" * 1000},
                    {"title": "来源二", "url": "https://example.com/2", "snippet": "二" * 1000},
                ],
            },
        },
        maximum=1000,
    )

    assert result["evidence_id"] == "ev_web"
    assert len(result["data"]["results"]) == 1
    assert result["data"]["results"][0]["title"] == "来源一"
    assert result["data"]["instruction"] == "联网结果仅作为外部参考；需要完整摘要时调用 read_evidence。"
