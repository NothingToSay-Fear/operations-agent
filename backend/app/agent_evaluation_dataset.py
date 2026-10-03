"""版本化 Plan-and-Execute 评测集与回归基线的加载和校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentEvaluationCase:
    """一条可在隔离业务数据上执行的 Agent 端到端评测案例。"""

    id: str
    scenario: str
    category: str
    goal: str
    rubric: str
    expected: dict[str, Any]


@dataclass(frozen=True)
class AgentEvaluationDataset:
    """可复现的 Agent 评测版本输入与质量门槛。"""

    root: Path
    version: str
    default_repeat: int
    cases: tuple[AgentEvaluationCase, ...]
    quality_gates: dict[str, dict[str, float]]


def load_agent_evaluation_dataset(root: Path) -> AgentEvaluationDataset:
    """加载并严格校验 Agent 评测集，拒绝静默忽略错误标注。"""

    root = root.resolve()
    manifest = _read_object(root / "manifest.json")
    version = _required_text(manifest, "version", root / "manifest.json")
    default_repeat = manifest.get("default_repeat", 1)
    if not isinstance(default_repeat, int) or not 1 <= default_repeat <= 10:
        raise ValueError("default_repeat 必须是 1 至 10 的整数")
    payload = _read_object(root / "cases.json")
    rows = payload.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("cases.json 必须包含非空 cases 数组")
    cases: list[AgentEvaluationCase] = []
    identifiers: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"cases.json 第 {index} 个案例必须是对象")
        identifier = _required_text(row, "id", root / "cases.json")
        if identifier in identifiers:
            raise ValueError(f"评测案例 ID 重复：{identifier}")
        expected = row.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"案例 {identifier} 缺少 expected")
        _validate_expected(identifier, expected)
        identifiers.add(identifier)
        cases.append(
            AgentEvaluationCase(
                id=identifier,
                scenario=_required_text(row, "scenario", root / "cases.json"),
                category=_required_text(row, "category", root / "cases.json"),
                goal=_required_text(row, "goal", root / "cases.json"),
                rubric=_required_text(row, "rubric", root / "cases.json"),
                expected=expected,
            )
        )
    return AgentEvaluationDataset(
        root=root,
        version=version,
        default_repeat=default_repeat,
        cases=tuple(cases),
        quality_gates=_quality_gates(manifest.get("quality_gates")),
    )


def load_baseline(dataset: AgentEvaluationDataset) -> tuple[dict[str, float], float] | None:
    """读取人工确认后的 Agent 评测回归基线。"""

    path = dataset.root / "baseline.json"
    if not path.is_file():
        return None
    payload = _read_object(path)
    if payload.get("dataset_version") != dataset.version:
        raise ValueError("baseline.json 的 dataset_version 与评测集版本不一致")
    metrics = payload.get("metrics")
    tolerance = payload.get("max_regression", 0.03)
    if not isinstance(metrics, dict) or not all(isinstance(value, (int, float)) for value in metrics.values()):
        raise ValueError("baseline.json 的 metrics 必须是数值对象")
    if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 1:
        raise ValueError("baseline.json 的 max_regression 必须是 0 至 1 的数值")
    return {str(key): float(value) for key, value in metrics.items()}, float(tolerance)


def write_baseline(dataset: AgentEvaluationDataset, metrics: dict[str, float]) -> Path:
    """写入已通过门槛的自动评分指标，供后续回归比较。"""

    target = dataset.root / "baseline.json"
    target.write_text(
        json.dumps(
            {"dataset_version": dataset.version, "max_regression": 0.03, "metrics": metrics},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _validate_expected(identifier: str, expected: dict[str, Any]) -> None:
    statuses = expected.get("valid_statuses")
    if not isinstance(statuses, list) or not statuses or not all(
        item in {"completed", "partial", "blocked", "waiting_user"} for item in statuses
    ):
        raise ValueError(f"案例 {identifier} 的 valid_statuses 无效")
    if not isinstance(expected.get("min_evidence"), int) or expected["min_evidence"] < 0:
        raise ValueError(f"案例 {identifier} 的 min_evidence 无效")
    if not isinstance(expected.get("require_artifact"), bool):
        raise ValueError(f"案例 {identifier} 的 require_artifact 必须是布尔值")
    if not isinstance(expected.get("max_subtasks"), int) or expected["max_subtasks"] < 0:
        raise ValueError(f"案例 {identifier} 的 max_subtasks 无效")
    for key in ("forbidden_tools", "required_subtask_roles"):
        if not isinstance(expected.get(key), list) or not all(
            isinstance(item, str) and item for item in expected[key]
        ):
            raise ValueError(f"案例 {identifier} 的 {key} 必须是非空字符串数组")


def _quality_gates(value: Any) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict) or not value:
        raise ValueError("quality_gates 必须是非空对象")
    result: dict[str, dict[str, float]] = {}
    for name, gate in value.items():
        if not isinstance(name, str) or not isinstance(gate, dict):
            raise ValueError("quality_gates 格式无效")
        allowed = {key: item for key, item in gate.items() if key in {"minimum", "maximum"}}
        if len(allowed) != 1 or len(gate) != 1:
            raise ValueError(f"质量门槛 {name} 只能配置 minimum 或 maximum")
        boundary = next(iter(allowed.values()))
        if not isinstance(boundary, (int, float)):
            raise ValueError(f"质量门槛 {name} 必须是数值")
        result[name] = {next(iter(allowed)): float(boundary)}
    return result


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"缺少评测文件：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 文件格式错误：{path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 文件根节点必须是对象：{path}")
    return payload


def _required_text(payload: dict[str, Any], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path.name} 缺少有效字段：{key}")
    return value.strip()
