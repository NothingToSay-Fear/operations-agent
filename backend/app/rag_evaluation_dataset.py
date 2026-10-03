"""版本化 RAG 评测集与回归基线的加载和校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RagEvaluationDocument:
    """评测语料中的一个原始文件。"""

    id: str
    title: str
    path: Path


@dataclass(frozen=True)
class RagEvaluationCase:
    """一个问题及其人工标注的目标资料和文本锚点。"""

    id: str
    question: str
    expected_document_ids: tuple[str, ...]
    expected_anchors: tuple[str, ...]

    @property
    def expects_answer(self) -> bool:
        return bool(self.expected_document_ids)


@dataclass(frozen=True)
class RagEvaluationDataset:
    """可复现 RAG 质量评测所需的全部版本化输入。"""

    root: Path
    version: str
    top_k: int
    documents: tuple[RagEvaluationDocument, ...]
    cases: tuple[RagEvaluationCase, ...]
    quality_gates: dict[str, dict[str, float]]


def load_rag_evaluation_dataset(root: Path) -> RagEvaluationDataset:
    """加载并严格校验评测集，避免错误标注被静默带入质量报告。"""
    resolved_root = root.resolve()
    manifest_path = resolved_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"缺少评测集清单：{manifest_path}")
    manifest = _read_json(manifest_path)
    version = _required_text(manifest, "version", manifest_path)
    top_k = manifest.get("top_k", 5)
    if not isinstance(top_k, int) or top_k < 1 or top_k > 20:
        raise ValueError("manifest.json 的 top_k 必须为 1 到 20 的整数")
    documents = _load_documents(resolved_root, manifest.get("documents"))
    document_ids = {document.id for document in documents}
    cases = _load_cases(resolved_root / "cases.json", document_ids)
    gates = _load_quality_gates(manifest.get("quality_gates"))
    return RagEvaluationDataset(
        root=resolved_root,
        version=version,
        top_k=top_k,
        documents=tuple(documents),
        cases=tuple(cases),
        quality_gates=gates,
    )


def load_baseline(dataset: RagEvaluationDataset) -> tuple[dict[str, float], float] | None:
    """读取人工确认的基线；首次运行没有基线时允许继续。"""
    path = dataset.root / "baseline.json"
    if not path.is_file():
        return None
    payload = _read_json(path)
    if payload.get("dataset_version") != dataset.version:
        raise ValueError("baseline.json 的 dataset_version 与 manifest.json 不一致")
    metrics = payload.get("metrics")
    tolerance = payload.get("max_regression", 0.03)
    if not isinstance(metrics, dict) or not all(
        isinstance(value, (int, float)) for value in metrics.values()
    ):
        raise ValueError("baseline.json 的 metrics 必须是数值对象")
    if not isinstance(tolerance, (int, float)) or tolerance < 0 or tolerance > 1:
        raise ValueError("baseline.json 的 max_regression 必须为 0 到 1 的数值")
    return {str(name): float(value) for name, value in metrics.items()}, float(tolerance)


def write_baseline(
    dataset: RagEvaluationDataset, metrics: dict[str, float], max_regression: float = 0.03
) -> Path:
    """将人工核对后的本次指标固化为后续回归比较基线。"""
    target = dataset.root / "baseline.json"
    target.write_text(
        json.dumps(
            {
                "dataset_version": dataset.version,
                "max_regression": max_regression,
                "metrics": metrics,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _load_documents(root: Path, raw_documents: Any) -> list[RagEvaluationDocument]:
    if not isinstance(raw_documents, list) or not raw_documents:
        raise ValueError("manifest.json 的 documents 必须是非空数组")
    documents: list[RagEvaluationDocument] = []
    ids: set[str] = set()
    for index, item in enumerate(raw_documents, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"documents 第 {index} 项必须是对象")
        identifier = _required_text(item, "id", Path("manifest.json"))
        title = _required_text(item, "title", Path("manifest.json"))
        relative_path = _required_text(item, "path", Path("manifest.json"))
        source = (root / relative_path).resolve()
        if root not in source.parents or not source.is_file():
            raise ValueError(f"评测资料不存在或越出评测集目录：{relative_path}")
        if identifier in ids:
            raise ValueError(f"评测资料 ID 重复：{identifier}")
        ids.add(identifier)
        documents.append(RagEvaluationDocument(id=identifier, title=title, path=source))
    return documents


def _load_cases(path: Path, document_ids: set[str]) -> list[RagEvaluationCase]:
    if not path.is_file():
        raise ValueError(f"缺少评测问题集：{path}")
    payload = _read_json(path)
    rows = payload.get("cases")
    if not isinstance(rows, list):
        raise ValueError("cases.json 必须包含 cases 数组")
    cases: list[RagEvaluationCase] = []
    ids: set[str] = set()
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{path.name} 第 {index} 个案例必须是对象")
        identifier = _required_text(item, "id", path)
        question = _required_text(item, "question", path)
        expected_document_ids = _string_list(
            item.get("expected_document_ids", []), "expected_document_ids", path
        )
        expected_anchors = _string_list(item.get("expected_anchors", []), "expected_anchors", path)
        unknown = set(expected_document_ids) - document_ids
        if unknown:
            raise ValueError(f"{path.name} 第 {index} 个案例引用了未知资料：{', '.join(sorted(unknown))}")
        if bool(expected_document_ids) != bool(expected_anchors):
            raise ValueError(f"{path.name} 第 {index} 个案例的目标资料与文本锚点必须同时存在或同时为空")
        if identifier in ids:
            raise ValueError(f"评测问题 ID 重复：{identifier}")
        ids.add(identifier)
        cases.append(
            RagEvaluationCase(
                id=identifier,
                question=question,
                expected_document_ids=tuple(expected_document_ids),
                expected_anchors=tuple(expected_anchors),
            )
        )
    if not cases:
        raise ValueError("评测问题集不能为空")
    return cases


def _load_quality_gates(raw_gates: Any) -> dict[str, dict[str, float]]:
    if not isinstance(raw_gates, dict) or not raw_gates:
        raise ValueError("manifest.json 的 quality_gates 必须是非空对象")
    gates: dict[str, dict[str, float]] = {}
    for name, raw_gate in raw_gates.items():
        if not isinstance(name, str) or not isinstance(raw_gate, dict):
            raise ValueError("quality_gates 的键和值必须分别是字符串和对象")
        allowed = {key: value for key, value in raw_gate.items() if key in {"minimum", "maximum"}}
        if len(allowed) != 1 or len(raw_gate) != 1:
            raise ValueError(f"质量门槛 {name} 必须且只能配置 minimum 或 maximum")
        boundary = next(iter(allowed.values()))
        if not isinstance(boundary, (int, float)):
            raise ValueError(f"质量门槛 {name} 必须是数值")
        gates[name] = {next(iter(allowed)): float(boundary)}
    return gates


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 文件格式错误：{path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 文件根节点必须是对象：{path}")
    return payload


def _required_text(item: dict[str, Any], key: str, path: Path) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path.name} 缺少非空字符串字段：{key}")
    return value.strip()


def _string_list(value: Any, key: str, path: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{path.name} 的 {key} 必须是字符串数组")
    return [item.strip() for item in value]
