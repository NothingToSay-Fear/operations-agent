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


class Action(StrictModel):
    kind: Literal["tool", "step_done", "replan", "ask_user"]
    tool: str = Field(default="", max_length=80)
    arguments: dict = Field(default_factory=dict)
    summary: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


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
