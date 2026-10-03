"""版本化记忆评测集的加载与严格校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MemoryEvaluationCase:
    """一条记忆评测案例。"""

    id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class MemoryEvaluationDataset:
    """可复现记忆评测的版本化输入。"""

    root: Path
    version: str
    history_top_k: int
    cases: tuple[MemoryEvaluationCase, ...]
    quality_gates: dict[str, dict[str, float]]


def load_memory_evaluation_dataset(root: Path) -> MemoryEvaluationDataset:
    """加载并校验记忆评测集。"""

    root = root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"缺少评测集清单：{manifest_path}")
    manifest = _read_json(manifest_path)
    version = _text(manifest, "version", manifest_path)
    history_top_k = manifest.get("history_top_k", 4)
    if not isinstance(history_top_k, int) or not 1 <= history_top_k <= 10:
        raise ValueError("history_top_k 必须是 1 到 10 的整数")
    cases = _load_cases(root / "cases.json")
    return MemoryEvaluationDataset(
        root=root,
        version=version,
        history_top_k=history_top_k,
        cases=tuple(cases),
        quality_gates=_gates(manifest.get("quality_gates")),
    )


def load_baseline(dataset: MemoryEvaluationDataset) -> tuple[dict[str, float], float] | None:
    """读取人工确认后的回归基线。"""

    path = dataset.root / "baseline.json"
    if not path.is_file():
        return None
    payload = _read_json(path)
    if payload.get("dataset_version") != dataset.version:
        raise ValueError("baseline.json 的 dataset_version 与评测集版本不一致")
    metrics = payload.get("metrics")
    tolerance = payload.get("max_regression", 0.03)
    if not isinstance(metrics, dict) or not all(isinstance(value, (int, float)) for value in metrics.values()):
        raise ValueError("baseline.json 的 metrics 必须是数值对象")
    if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 1:
        raise ValueError("baseline.json 的 max_regression 必须是 0 到 1 的数值")
    return {str(key): float(value) for key, value in metrics.items()}, float(tolerance)


def write_baseline(dataset: MemoryEvaluationDataset, metrics: dict[str, float]) -> Path:
    """将本次已通过的硬指标写入回归基线。"""

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


def _load_cases(path: Path) -> list[MemoryEvaluationCase]:
    if not path.is_file():
        raise ValueError(f"缺少评测案例：{path}")
    payload = _read_json(path)
    rows = payload.get("cases")
    if not isinstance(rows, list):
        raise ValueError("cases.json 必须包含 cases 数组")
    cases: list[MemoryEvaluationCase] = []
    identifiers: set[str] = set()
    allowed = {"context", "history", "summary", "lifecycle"}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} 第 {index} 个案例必须是对象")
        identifier = _text(row, "id", path)
        kind = _text(row, "kind", path)
        if kind not in allowed:
            raise ValueError(f"{path.name} 第 {index} 个案例的 kind 无效：{kind}")
        if identifier in identifiers:
            raise ValueError(f"评测案例 ID 重复：{identifier}")
        if not isinstance(row.get("messages", []), list) or not row["messages"]:
            raise ValueError(f"{path.name} 第 {index} 个案例缺少 messages")
        if not isinstance(row.get("expected"), dict):
            raise ValueError(f"{path.name} 第 {index} 个案例缺少 expected")
        identifiers.add(identifier)
        cases.append(MemoryEvaluationCase(id=identifier, kind=kind, payload=row))
    if not cases:
        raise ValueError("评测案例不能为空")
    return cases


def _gates(value: Any) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict) or not value:
        raise ValueError("quality_gates 必须是非空对象")
    result: dict[str, dict[str, float]] = {}
    for name, gate in value.items():
        if not isinstance(name, str) or not isinstance(gate, dict):
            raise ValueError("quality_gates 格式无效")
        allowed = {key: item for key, item in gate.items() if key in {"minimum", "maximum"}}
        if len(allowed) != 1 or len(allowed) != len(gate):
            raise ValueError(f"质量门槛 {name} 只能配置 minimum 或 maximum")
        value = next(iter(allowed.values()))
        if not isinstance(value, (int, float)):
            raise ValueError(f"质量门槛 {name} 必须是数值")
        result[name] = {next(iter(allowed)): float(value)}
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name} 不是合法 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是对象")
    return value


def _text(payload: dict[str, Any], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path.name} 缺少有效字段：{key}")
    return value.strip()
