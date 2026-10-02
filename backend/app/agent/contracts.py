"""规划、执行、子任务与评估阶段的严格结构化协议。"""

from typing import Literal

from pydantic import Field, model_validator

from app.analytics import StrictModel


class Criterion(StrictModel):
    id: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=500)


class Step(StrictModel):
    id: str = Field(min_length=1, max_length=40)
    objective: str = Field(min_length=1, max_length=1000)
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    done_when: str = Field(min_length=1, max_length=1000)


class Plan(StrictModel):
    summary: str = Field(min_length=1, max_length=1000)
    criteria: list[Criterion] = Field(min_length=1, max_length=8)
    steps: list[Step] = Field(min_length=1, max_length=10)
    change_reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_dependencies(self):
        identifiers = [s.id for s in self.steps]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("步骤ID重复")
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("成功标准ID重复")
        seen = set()
        for step in self.steps:
            if any(dep not in seen for dep in step.depends_on):
                raise ValueError("依赖必须引用计划中排在前面的步骤，不能循环或缺失")
            seen.add(step.id)
        return self


class ToolCall(StrictModel):
    tool: str = Field(min_length=1, max_length=80)
    arguments: dict = Field(default_factory=dict)
    summary: str = Field(min_length=1, max_length=500)


class SubtaskSpec(StrictModel):
    """主 Agent 下发给临时专项执行器的受限任务单。"""

    id: str = Field(min_length=1, max_length=40)
    role: Literal["channel", "product_inventory", "knowledge", "general"]
    objective: str = Field(min_length=1, max_length=800)
    done_when: str = Field(min_length=1, max_length=800)
    allowed_tools: list[str] = Field(min_length=1, max_length=6)
    max_model_calls: int = Field(default=3, ge=1, le=4)
    max_tool_calls: int = Field(default=3, ge=1, le=4)


class Action(StrictModel):
    kind: Literal["tool", "tools", "delegate", "step_done", "replan", "ask_user"]
    tool: str = Field(default="", max_length=80)
    arguments: dict = Field(default_factory=dict)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=6)
    subtasks: list[SubtaskSpec] = Field(default_factory=list, max_length=3)
    summary: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_tools(self):
        if self.kind == "tool" and not self.tool:
            raise ValueError("单工具动作必须提供tool")
        if self.kind == "tools" and not self.tool_calls:
            raise ValueError("批量工具动作必须提供tool_calls")
        if self.kind != "tools" and self.tool_calls:
            raise ValueError("只有批量工具动作可以提供tool_calls")
        if self.kind == "delegate" and not self.subtasks:
            raise ValueError("委派动作必须提供子任务")
        if self.kind != "delegate" and self.subtasks:
            raise ValueError("只有委派动作可以提供子任务")
        if len({item.id for item in self.subtasks}) != len(self.subtasks):
            raise ValueError("子任务ID重复")
        return self


class SubtaskFinding(StrictModel):
    statement: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class SubtaskDecision(StrictModel):
    """子 Agent 的局部执行决策；它不能重规划总体任务或直接面向用户交付。"""

    kind: Literal["tool", "tools", "finish", "stop"]
    tool: str = Field(default="", max_length=80)
    arguments: dict = Field(default_factory=dict)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=4)
    summary: str = Field(min_length=1, max_length=1600)
    findings: list[SubtaskFinding] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_action(self):
        if self.kind == "tool" and not self.tool:
            raise ValueError("单工具动作必须提供tool")
        if self.kind == "tools" and not self.tool_calls:
            raise ValueError("批量工具动作必须提供tool_calls")
        if self.kind != "tools" and self.tool_calls:
            raise ValueError("只有批量工具动作可以提供tool_calls")
        if self.kind in {"finish", "stop"} and not (self.findings or self.limitations):
            raise ValueError("子任务结束时必须提供发现或限制")
        return self


class Assessment(StrictModel):
    criterion_id: str
    satisfied: bool
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(max_length=1000)


class Decision(StrictModel):
    kind: Literal["continue", "replan", "ask_user", "finish", "stop"]
    reason: str = Field(min_length=1, max_length=2000)
    answer: str = Field(default="", max_length=20000)
    assessments: list[Assessment] = Field(default_factory=list, max_length=8)


class ToolResult(StrictModel):
    status: Literal["success", "empty", "failed"]
    data: dict = Field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False
