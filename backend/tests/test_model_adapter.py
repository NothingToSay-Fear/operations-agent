"""用真实 LangChain HTTP 适配器验证各阶段协议、错误修复提示和用量保留。"""

import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from app.agent.contracts import Action, Decision, Plan
from app.agent.model import ModelGateway, StructuredOutputError


VALUES = {
    "plan": {
        "summary": "查询目标",
        "criteria": [{"id": "c1", "description": "取得数据"}],
        "steps": [{"id": "s1", "objective": "检查数据", "depends_on": [], "done_when": "取得证据"}],
        "change_reason": "初始计划",
    },
    "execute": {
        "kind": "tool",
        "tool": "query_metrics",
        "arguments": {"group_by": "channel"},
        "tool_calls": [],
        "summary": "查询渠道",
        "evidence_ids": [],
    },
    "evaluate": {"kind": "continue", "reason": "仍有待完成步骤", "answer": "", "assessments": []},
}
SCHEMAS = {"plan": Plan, "execute": Action, "evaluate": Decision}


def completion(message, finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "id": "fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "fixture",
            "choices": [
                {"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", **message}}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        },
    )


def model_with_transport(client):
    return ChatOpenAI(
        model="fixture",
        api_key="fixture-only",
        base_url="https://fixture.invalid/v1",
        http_async_client=client,
        max_retries=0,
    )


@pytest.mark.parametrize("deepseek", [False, True])
@pytest.mark.parametrize("phase", ["plan", "execute", "evaluate"])
async def test_actual_langchain_structured_output_adapter(settings, deepseek, phase):
    if deepseek:
        settings = settings.model_copy(
            update={"llm_base_url": "https://api.deepseek.com", "llm_model": "deepseek-v4-flash"}
        )
    captured = []

    async def provider(request):
        payload = json.loads(request.content)
        captured.append(payload)
        context = json.loads(payload["messages"][1]["content"])
        assert context["current_phase"] == phase
        assert ("tools" in context) == (phase != "evaluate")
        if deepseek:
            assert payload["response_format"] == {"type": "json_object"}
            assert "tool_choice" not in payload and "tools" not in payload
            assert "thinking" not in payload
            assert SCHEMAS[phase].__name__ in payload["messages"][0]["content"]
            assert '"properties"' in payload["messages"][0]["content"]
            return completion({"content": json.dumps(VALUES[phase])})
        name = SCHEMAS[phase].__name__
        assert payload["tool_choice"]["function"]["name"] == name
        return completion(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "fixture",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(VALUES[phase])},
                    }
                ],
            },
            "tool_calls",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        gateway = ModelGateway(settings)
        gateway._model = model_with_transport(client)
        result, usage = await gateway.decide(
            phase, {"goal": "验证协议", "tools": [{"name": "query_metrics"}]}
        )
    assert isinstance(result, SCHEMAS[phase])
    assert result.model_dump() == VALUES[phase]
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 50
    assert len(captured) == 1


@pytest.mark.parametrize(
    "kind, value",
    [
        ("expand", {"queries": ["库存覆盖天数"]}),
        ("summary", {"summary": "只分析近七天", "decisions": [], "open_questions": [], "source_ids": []}),
        ("candidates", {"items": []}),
    ],
)
async def test_deepseek_auxiliary_json_output(settings, monkeypatch, kind, value):
    from app import auxiliary

    settings = settings.model_copy(
        update={"llm_model": "deepseek-v4-flash", "llm_base_url": "https://proxy.invalid/v1"}
    )

    async def provider(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "tool_choice" not in payload and "tools" not in payload
        return completion({"content": json.dumps(value)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        model = model_with_transport(client)
        monkeypatch.setattr(ModelGateway, "build", lambda self: model)
        parsed, usage = await auxiliary.call(settings, kind, {"text": "仅验证协议"})
    assert parsed == value
    assert usage["input_tokens"] == 100


@pytest.mark.parametrize(
    "message, finish, expected",
    [
        ({"content": "任务已经完成 sk-secret-fixture"}, "stop", "未取得可解析"),
        (
            {"content": json.dumps({"kind": "query_metrics", "reason": "sk-secret-fixture"})},
            "stop",
            "kind: literal_error",
        ),
        (
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "fixture",
                        "type": "function",
                        "function": {"name": "query_metrics", "arguments": "{}"},
                    }
                ],
            },
            "tool_calls",
            "不能直接返回业务工具",
        ),
        ({"content": json.dumps(VALUES["evaluate"])}, "length", "被截断"),
    ],
)
async def test_deepseek_invalid_decision_has_safe_repair_feedback_and_usage(
    settings, message, finish, expected
):
    settings = settings.model_copy(update={"llm_model": "deepseek-v4-flash"})

    async def provider(request):
        return completion(message, finish)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        gateway = ModelGateway(settings)
        gateway._model = model_with_transport(client)
        with pytest.raises(StructuredOutputError, match=expected) as error:
            await gateway.decide("evaluate", {"goal": "验证错误输出不绕过结构校验"})
    assert "sk-secret-fixture" not in str(error.value)
    assert error.value.usage["input_tokens"] == 100


async def test_deepseek_plan_ignores_only_unknown_fields(settings):
    settings = settings.model_copy(update={"llm_model": "deepseek-v4-flash"})
    value = json.loads(json.dumps(VALUES["plan"]))
    value["steps"][0]["status"] = "pending"
    value["steps"][0]["criterion_ids"] = ["c1"]

    async def provider(request):
        return completion({"content": json.dumps(value)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        gateway = ModelGateway(settings)
        gateway._model = model_with_transport(client)
        result, usage = await gateway.decide("plan", {"goal": "验证多余字段归一化"})

    assert result.model_dump() == VALUES["plan"]
    assert usage["input_tokens"] == 100


async def test_deepseek_plan_does_not_repair_missing_fields(settings):
    settings = settings.model_copy(update={"llm_model": "deepseek-v4-flash"})
    value = json.loads(json.dumps(VALUES["plan"]))
    value["steps"][0].pop("done_when")
    value["steps"][0]["status"] = "pending"

    async def provider(request):
        return completion({"content": json.dumps(value)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        gateway = ModelGateway(settings)
        gateway._model = model_with_transport(client)
        with pytest.raises(StructuredOutputError, match="missing"):
            await gateway.decide("plan", {"goal": "验证缺失字段继续失败"})
